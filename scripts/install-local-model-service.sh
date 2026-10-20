#!/usr/bin/env bash
set -euo pipefail

MODEL_PATH=${1:-$HOME/.local/share/llama.cpp/models/Qwen3.5-4B-Q6_K.gguf}
REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CONFIG_DIR=$HOME/.config/llama-server
SYSTEMD_DIR=$HOME/.config/systemd/user
ENV_FILE=$CONFIG_DIR/env

for command in openssl systemctl; do
  if ! command -v "$command" >/dev/null; then
    echo "Required command is missing: $command" >&2
    exit 1
  fi
done

if [[ ! -x $HOME/.local/bin/llama-server ]]; then
  echo "llama-server is missing at $HOME/.local/bin/llama-server" >&2
  echo "Build and link llama.cpp first; see README.md Local model setup." >&2
  exit 1
fi

if [[ ! -r $MODEL_PATH ]]; then
  echo "Model is not readable: $MODEL_PATH" >&2
  echo "Download and verify Qwen3.5-4B-Q6_K.gguf first; see README.md." >&2
  exit 1
fi

mkdir -p "$CONFIG_DIR" "$SYSTEMD_DIR"
if [[ ! -f $ENV_FILE ]]; then
  umask 077
  API_KEY=$(openssl rand -hex 32)
  {
    printf 'LLAMA_API_KEY=%s\n' "$API_KEY"
    printf 'LLAMA_MODEL_PATH=%s\n' "$MODEL_PATH"
    printf 'LOCAL_LLM_BASE_URL=http://127.0.0.1:9322/v1\n'
    printf 'LOCAL_LLM_MODEL=qwen3.5-4b-local\n'
    printf 'LOCAL_LLM_IDLE_WARNING_SECONDS=1800\n'
    printf 'LOCAL_LLM_IDLE_GRACE_SECONDS=180\n'
    printf 'LOCAL_LLM_WEB_PROXY_PORT=9321\n'
  } >"$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  echo "Preserving existing configuration: $ENV_FILE"
  if grep -qx 'LOCAL_LLM_BASE_URL=http://127.0.0.1:9321/v1' "$ENV_FILE"; then
    sed -i \
      's|^LOCAL_LLM_BASE_URL=http://127.0.0.1:9321/v1$|LOCAL_LLM_BASE_URL=http://127.0.0.1:9322/v1|' \
      "$ENV_FILE"
  fi
  if ! grep -q '^LOCAL_LLM_WEB_PROXY_PORT=' "$ENV_FILE"; then
    printf 'LOCAL_LLM_WEB_PROXY_PORT=9321\n' >>"$ENV_FILE"
  fi
fi

cp "$REPO_ROOT/systemd/llama-server.service" "$SYSTEMD_DIR/"
cp "$REPO_ROOT/systemd/claude-slack-bot.service" "$SYSTEMD_DIR/"
systemctl --user daemon-reload
systemctl --user enable llama-server.service
systemctl --user restart llama-server.service
systemctl --user restart claude-slack-bot.service

echo "Local model service installed. Open Matrix and send: /local Hello"
