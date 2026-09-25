#!/usr/bin/env bash
# Deploy or update AIBox on a running lckfb-tspi Armbian installation.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app=/opt/eda-aibox
cache=/var/cache/eda-aibox
backup="$app/backups/$(date -u +%Y%m%dT%H%M%SZ)"

log() { printf '\n[AIBox] %s\n' "$*"; }
die() { printf '[AIBox] ERROR: %s\n' "$*" >&2; exit 1; }
fetch() {
    local url=$1 sha=$2 target=$3
    if [[ -f $target ]] && echo "$sha  $target" | sha256sum -c - >/dev/null 2>&1; then return; fi
    curl -fL --retry 5 --retry-all-errors -o "$target.part" "$url"
    echo "$sha  $target.part" | sha256sum -c -
    mv -f "$target.part" "$target"
}
model() {
    local name=$1 marker=$2 url=$3 sha=$4 archive="$cache/$1.tar.bz2" stage="$app/models/.install-$1"
    if [[ -s $app/models/$name/$marker ]]; then log "保留已有模型 $name"; return; fi
    log "下载模型 $name"
    fetch "$url" "$sha" "$archive"
    rm -rf -- "$stage"
    install -d "$stage"
    tar -xjf "$archive" -C "$stage"
    [[ -s $stage/$name/$marker ]] || die "模型缺少 $marker: $name"
    if [[ -e $app/models/$name ]]; then mv "$app/models/$name" "$backup/$name"; fi
    mv "$stage/$name" "$app/models/$name"
    rmdir "$stage"
}

[[ $(id -u) -eq 0 ]] || die "请运行 sudo bash install.sh"
[[ $(uname -m) == aarch64 ]] || die "只支持 ARM64 泰山派"
[[ -f /boot/armbianEnv.txt ]] || die "未找到 Armbian 启动配置"
grep -q 'rk3566-taishanpi-v10.dtb' /boot/armbianEnv.txt || die "设备树不是已验证的泰山派 RK3566 版本"
[[ -f /etc/os-release ]] && . /etc/os-release
[[ ${VERSION_CODENAME:-} == resolute ]] || die "需要当前已验证的 Ubuntu 26.04 resolute 基底"
[[ -f $repo/app/ai.py && -f $repo/requirements-arm64.txt ]] || die "请从完整仓库目录运行脚本"
command -v systemctl >/dev/null || die "需要 systemd"
install -d "$app/models" "$app/lib" "$cache" "$backup" /boot/overlay-user
for file in ai.py aht10_read.py setup_led_pwm.py; do
    [[ -f $app/$file ]] && cp -a "$app/$file" "$backup/$file"
done
for unit in ai.service ollama.service; do
    [[ -f /etc/systemd/system/$unit ]] && cp -a "/etc/systemd/system/$unit" "$backup/$unit"
done

log '安装系统依赖'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3-venv python3-pip alsa-utils gpiod i2c-tools fonts-noto-cjk ca-certificates curl libatomic1 libgomp1 device-tree-compiler zstd

if ! id yang >/dev/null 2>&1; then useradd -m -s /bin/bash yang; fi
if ! id ollama >/dev/null 2>&1; then useradd -r -m -d /usr/share/ollama -s /usr/sbin/nologin ollama; fi
usermod -aG audio,render,i2c yang
install -d -o ollama -g ollama /usr/share/ollama/.ollama/models
chown -R ollama:ollama /usr/share/ollama/.ollama

log '安装语音模型与 NPU 运行库'
model sherpa-onnx-rk3566-5-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17 model.rknn \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-rk3566-5-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 \
  d4108444665190ef4c1aff01a2ac47cb581cb25f7b64ea6a02065fad3734a2fa
model sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16 encoder-epoch-99-avg-1.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2 \
  2b7c63322b32e5e0f2526043a1103366119ca58dd615cd7105a37c01db9553d7
model vits-icefall-zh-aishell3 model.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-icefall-zh-aishell3.tar.bz2 \
  ab468db3a3308cdd861495e0db2f25d79418a0c00639f74944c7cdf5dd8c6ec1
model vits-piper-zh_CN-xiao_ya-medium zh_CN-xiao_ya-medium.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-zh_CN-xiao_ya-medium.tar.bz2 \
  9396a3dffbb95b037acaa18094500f58d0db9a7c4f2689554e2539717cf0db65
fetch https://raw.githubusercontent.com/airockchip/rknn-toolkit2/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so \
  d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8 "$app/lib/librknnrt.so"

log '准备 Python 环境并运行应用测试'
if [[ ! -x $app/venv/bin/python ]]; then python3 -m venv "$app/venv"; fi
"$app/venv/bin/python" -m pip install --no-cache-dir -r "$repo/requirements-arm64.txt"
export LD_LIBRARY_PATH="$app/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
(cd "$repo/app" && for test in smoke_ui.py smoke_interrupt.py smoke_led.py smoke_weather_vad.py smoke_wake_display.py; do "$app/venv/bin/python" "$test"; done)

log '准备 Ollama 和 Qwen3 模型'
if [[ ! -x /usr/bin/ollama ]] || ! /usr/bin/ollama --version 2>/dev/null | grep -q '0.34.4'; then
    fetch https://github.com/ollama/ollama/releases/download/v0.34.4/ollama-linux-arm64.tar.zst \
      96f50a1192133028cf4e010d8c333f8af14b1505db6be7b2034c11487e7fd7e6 "$cache/ollama-linux-arm64.tar.zst"
    systemctl stop ollama.service 2>/dev/null || true
    tar --zstd -xf "$cache/ollama-linux-arm64.tar.zst" -C /usr --exclude='lib/ollama/cuda*'
fi
install -m 644 "$repo/app/ollama.service" /etc/systemd/system/ollama.service
systemctl daemon-reload
systemctl enable --now ollama.service
for attempt in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
    sleep 1
done
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null || die 'Ollama 未就绪；请查看 journalctl -u ollama'
for tag in qwen3:0.6b qwen3:1.7b-q4_K_M; do
    if ! /usr/bin/ollama show "$tag" >/dev/null 2>&1; then
        runuser -u ollama -- env OLLAMA_HOST=127.0.0.1:11434 /usr/bin/ollama pull "$tag"
    fi
    /usr/bin/ollama show "$tag" >/dev/null || die "模型不可用：$tag"
done

log '更新服务与 LED 覆盖层，保留现有设置'
for file in ai.py aht10_read.py setup_led_pwm.py smoke_ui.py smoke_interrupt.py smoke_led.py smoke_weather_vad.py smoke_wake_display.py; do
    install -m 644 "$repo/app/$file" "$app/$file"
done
install -m 644 "$repo/app/90-eda-aibox-gpio.rules" /etc/udev/rules.d/90-eda-aibox-gpio.rules
install -m 644 "$repo/app/ai.service" /etc/systemd/system/ai.service
install -m 644 "$repo/requirements-arm64.txt" "$app/requirements-arm64.txt"
[[ -e $app/asr_backend ]] || printf 'sensevoice\n' > "$app/asr_backend"
[[ -e $app/tts_backend ]] || printf 'xiao_ya\n' > "$app/tts_backend"
dtc -@ -I dts -O dtb -o /boot/overlay-user/eda-led-pwm0.dtbo "$repo/app/eda-led-pwm0.dts"
overlay_value=$(sed -n 's/^user_overlays=//p' /boot/armbianEnv.txt | head -n 1)
if [[ " $overlay_value " != *" eda-led-pwm0 "* ]]; then
    if grep -q '^user_overlays=' /boot/armbianEnv.txt; then
        sed -i '/^user_overlays=/ s/$/ eda-led-pwm0/; s/^user_overlays= /user_overlays=/' /boot/armbianEnv.txt
    else
        printf '\nuser_overlays=eda-led-pwm0\n' >> /boot/armbianEnv.txt
    fi
fi
chown -R yang:yang "$app"
udevadm control --reload-rules
systemctl daemon-reload
systemctl enable ai.service
systemctl restart ai.service
if ! systemctl is-active --quiet ai.service; then
    journalctl -u ai.service -n 30 --no-pager >&2 || true
    die 'AI 服务未能启动；若外设尚未连接，请接好后运行 systemctl restart ai'
fi
log '部署完成：ai 与 ollama 服务运行中。当前设置文件和已有模型已保留。'
