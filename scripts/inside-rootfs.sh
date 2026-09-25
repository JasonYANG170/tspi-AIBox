#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export LD_LIBRARY_PATH=/opt/eda-aibox/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
apt-get update
apt-get install -y --no-install-recommends python3-venv python3-pip alsa-utils gpiod i2c-tools fonts-noto-cjk ca-certificates curl libatomic1 libgomp1

if ! id yang >/dev/null 2>&1; then useradd -m -s /bin/bash yang; fi
if ! id ollama >/dev/null 2>&1; then useradd -r -m -d /usr/share/ollama -s /usr/sbin/nologin ollama; fi
usermod -aG audio,render,i2c yang
install -d -o ollama -g ollama /usr/share/ollama/.ollama/models
chown -R ollama:ollama /usr/share/ollama/.ollama
chown -R yang:yang /opt/eda-aibox

python3 -m venv /opt/eda-aibox/venv
/opt/eda-aibox/venv/bin/python -m pip install --no-cache-dir -r /opt/eda-aibox/requirements-arm64.txt
cd /opt/eda-aibox
for test in smoke_ui.py smoke_interrupt.py smoke_led.py smoke_weather_vad.py smoke_wake_display.py; do
    /opt/eda-aibox/venv/bin/python "$test"
done

export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_MODELS=/usr/share/ollama/.ollama/models
runuser -u ollama -- env OLLAMA_HOST="$OLLAMA_HOST" OLLAMA_MODELS="$OLLAMA_MODELS" /usr/bin/ollama serve >/tmp/ollama-build.log 2>&1 &
server_pid=$!
cleanup() { kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; }
trap cleanup EXIT
for attempt in $(seq 1 15); do
    if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    sleep 1
done
if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null; then
    cat /tmp/ollama-build.log >&2
    exit 1
fi
runuser -u ollama -- env OLLAMA_HOST="$OLLAMA_HOST" OLLAMA_MODELS="$OLLAMA_MODELS" /usr/bin/ollama pull qwen3:0.6b
runuser -u ollama -- env OLLAMA_HOST="$OLLAMA_HOST" OLLAMA_MODELS="$OLLAMA_MODELS" /usr/bin/ollama pull qwen3:1.7b-q4_K_M
/usr/bin/ollama show qwen3:0.6b >/dev/null
/usr/bin/ollama show qwen3:1.7b-q4_K_M >/dev/null
rm -f /usr/share/ollama/.ollama/id_ed25519 /usr/share/ollama/.ollama/id_ed25519.pub
echo 'Both Ollama models installed'
