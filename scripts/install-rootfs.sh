#!/usr/bin/env bash
set -euo pipefail
root=${1:?rootfs mount required}
repo=${2:?repository path required}
work=${3:?work path required}

fetch() {
    local url=$1 sha=$2 file=$3
    if [[ ! -f $file ]]; then
        curl -fL --retry 5 --retry-all-errors -o "$file.part" "$url"
        mv "$file.part" "$file"
    fi
    echo "$sha  $file" | sha256sum -c -
}

install_model() {
    local name=$1 url=$2 sha=$3
    local archive="$work/$name.tar.bz2"
    fetch "$url" "$sha" "$archive"
    tar -xjf "$archive" -C "$root/opt/eda-aibox/models"
    test -d "$root/opt/eda-aibox/models/$name"
}

install -d "$root/opt/eda-aibox/models" "$root/opt/eda-aibox/lib" "$root/etc/udev/rules.d" "$root/etc/systemd/system" "$root/boot/overlay-user"
install -m 644 "$repo/app/90-eda-aibox-gpio.rules" "$root/etc/udev/rules.d/90-eda-aibox-gpio.rules"
install -m 644 "$repo/app/ai.service" "$root/etc/systemd/system/ai.service"
install -m 644 "$repo/app/ollama.service" "$root/etc/systemd/system/ollama.service"
install -m 644 "$repo/requirements-arm64.txt" "$root/opt/eda-aibox/requirements-arm64.txt"
for file in ai.py aht10_read.py setup_led_pwm.py smoke_ui.py smoke_interrupt.py smoke_led.py smoke_weather_vad.py smoke_wake_display.py; do
    install -m 644 "$repo/app/$file" "$root/opt/eda-aibox/$file"
done
printf 'sensevoice\n' > "$root/opt/eda-aibox/asr_backend"
printf 'xiao_ya\n' > "$root/opt/eda-aibox/tts_backend"
dtc -@ -I dts -O dtb -o "$root/boot/overlay-user/eda-led-pwm0.dtbo" "$repo/app/eda-led-pwm0.dts"
if grep -q '^user_overlays=' "$root/boot/armbianEnv.txt"; then
    sed -i '/^user_overlays=/ s/^user_overlays=.*/user_overlays=eda-led-pwm0/' "$root/boot/armbianEnv.txt"
else
    printf '\nuser_overlays=eda-led-pwm0\n' >> "$root/boot/armbianEnv.txt"
fi

install_model sherpa-onnx-rk3566-5-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17 \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-rk3566-5-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 \
    d4108444665190ef4c1aff01a2ac47cb581cb25f7b64ea6a02065fad3734a2fa
install_model sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16 \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2 \
    2b7c63322b32e5e0f2526043a1103366119ca58dd615cd7105a37c01db9553d7
install_model vits-icefall-zh-aishell3 \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-icefall-zh-aishell3.tar.bz2 \
    ab468db3a3308cdd861495e0db2f25d79418a0c00639f74944c7cdf5dd8c6ec1
install_model vits-piper-zh_CN-xiao_ya-medium \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-zh_CN-xiao_ya-medium.tar.bz2 \
    9396a3dffbb95b037acaa18094500f58d0db9a7c4f2689554e2539717cf0db65

fetch https://raw.githubusercontent.com/airockchip/rknn-toolkit2/v2.3.2/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so \
    d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8 \
    "$root/opt/eda-aibox/lib/librknnrt.so"
ollama_archive="$work/ollama-linux-arm64.tar.zst"
fetch https://github.com/ollama/ollama/releases/download/v0.34.4/ollama-linux-arm64.tar.zst \
    96f50a1192133028cf4e010d8c333f8af14b1505db6be7b2034c11487e7fd7e6 "$ollama_archive"
tar --zstd -xf "$ollama_archive" -C "$root/usr" --exclude='lib/ollama/cuda*'
test -x "$root/usr/bin/ollama"

install -m 755 "$repo/scripts/inside-rootfs.sh" "$root/tmp/inside-rootfs.sh"
chroot "$root" /bin/bash /tmp/inside-rootfs.sh
rm -f "$root/tmp/inside-rootfs.sh"
systemctl --root="$root" enable ollama.service ai.service
rm -rf "$root/var/lib/apt/lists/"* "$root/root/.cache/pip" "$root/opt/eda-aibox/venv/.cache"
echo 'Rootfs installation complete'
