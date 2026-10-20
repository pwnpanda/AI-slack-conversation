# Local llama.cpp model and Matrix chat

This setup runs Qwen3.5-4B locally behind llama.cpp and exposes it through the existing Matrix
bot. The same GGUF model runs on the Radeon/Vulkan Linux host and an NVIDIA/CUDA Windows host;
only the llama.cpp binary differs.

## Pinned components

| Component | Pinned value |
|---|---|
| llama.cpp | commit `7879491cf4c1cfb25acbd78fae34a9e7a8e3a062` |
| Model | `unsloth/Qwen3.5-4B-GGUF`, `Qwen3.5-4B-Q6_K.gguf` |
| Model size | 3,525,956,768 bytes |
| SHA-256 | `fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66` |
| Web/API proxy | `127.0.0.1:9321` |
| llama.cpp backend | `127.0.0.1:9322` |
| Context | 4,096 tokens, one concurrent slot |

Q6_K was selected because its 3.53 GB weights fit inside the coworker's 8 GB RTX 4070 Laptop
VRAM with room for the KV cache. It also runs fully offloaded on the Radeon 890M. Both systems
use the exact same model file.

## Linux build

Install build dependencies on Debian 13:

```bash
pkexec apt-get install -y build-essential cmake curl git glslc libvulkan-dev openssl spirv-headers
```

Build the pinned Vulkan version:

```bash
mkdir -p ~/.local/src
git clone https://github.com/ggml-org/llama.cpp.git ~/.local/src/llama.cpp
git -C ~/.local/src/llama.cpp checkout 7879491cf4c1cfb25acbd78fae34a9e7a8e3a062
cmake -S ~/.local/src/llama.cpp -B ~/.local/src/llama.cpp/build \
  -DGGML_VULKAN=ON -DGGML_CCACHE=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build ~/.local/src/llama.cpp/build --config Release -j "$(nproc)"
mkdir -p ~/.local/bin
ln -sfn ~/.local/src/llama.cpp/build/bin/llama-server ~/.local/bin/llama-server
ln -sfn ~/.local/src/llama.cpp/build/bin/llama-cli ~/.local/bin/llama-cli
llama-cli --list-devices
```

For an NVIDIA Linux host with the CUDA toolkit installed, replace `-DGGML_VULKAN=ON` with
`-DGGML_CUDA=ON`.

## Windows CUDA build

Install Git, CMake, Visual Studio 2022 Build Tools with the C++ workload, and the NVIDIA CUDA
Toolkit. In a Developer PowerShell:

```powershell
git clone https://github.com/ggml-org/llama.cpp.git "$HOME\src\llama.cpp"
git -C "$HOME\src\llama.cpp" checkout 7879491cf4c1cfb25acbd78fae34a9e7a8e3a062
cmake -S "$HOME\src\llama.cpp" -B "$HOME\src\llama.cpp\build" -DGGML_CUDA=ON
cmake --build "$HOME\src\llama.cpp\build" --config Release -j
& "$HOME\src\llama.cpp\build\bin\Release\llama-cli.exe" --list-devices
```

The expected device is the RTX 4070 Laptop GPU. Native Windows has access to the full 32 GB
system RAM; WSL is not required.

## Download and verify the shared model

Linux:

```bash
mkdir -p ~/.local/share/llama.cpp/models
curl -fL --retry 3 \
  -o ~/.local/share/llama.cpp/models/Qwen3.5-4B-Q6_K.gguf \
  https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q6_K.gguf
printf '%s  %s\n' \
  fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66 \
  "$HOME/.local/share/llama.cpp/models/Qwen3.5-4B-Q6_K.gguf" | sha256sum --check
```

Windows PowerShell:

```powershell
$model = "$HOME\models\Qwen3.5-4B-Q6_K.gguf"
New-Item -ItemType Directory -Force (Split-Path $model)
Invoke-WebRequest `
  https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q6_K.gguf `
  -OutFile $model
(Get-FileHash -Algorithm SHA256 $model).Hash
```

The printed Windows hash must equal the pinned SHA-256 above.

## Install the Linux services

From this repository:

```bash
scripts/install-local-model-service.sh \
  "$HOME/.local/share/llama.cpp/models/Qwen3.5-4B-Q6_K.gguf"
```

The installer creates a random API key in `~/.config/llama-server/env` with mode `0600`, installs
both user units, starts llama.cpp, and restarts the Matrix bot. The API key is passed through an
environment variable rather than exposed in the process command line.

Verify:

```bash
systemctl --user status llama-server claude-slack-bot
source ~/.config/llama-server/env
curl -fsS -H "Authorization: Bearer $LLAMA_API_KEY" http://127.0.0.1:9321/health
journalctl --user -u llama-server -u claude-slack-bot --since '5 minutes ago' --no-pager
```

Open `http://127.0.0.1:9321` for llama.cpp's built-in web interface and enter the value of
`LLAMA_API_KEY` when prompted. Port 9321 is an authenticated proxy owned by the Matrix bot;
llama.cpp itself listens on the private backend port 9322. This separation lets the bot observe
browser response streams without exposing the model on another network interface.

## Matrix usage and history

Send a top-level Matrix message:

```text
/local What is the capital of Norway?
```

The bot creates a thread and streams the response by editing one Matrix message. Reply inside that
thread to continue the same conversation. Full conversation history is stored as readable JSON in
`~/.local/state/claude-slack-bot/local-chats/`. Only the most recent 12,000 characters are sent to
the 4,096-token model context; older messages remain on disk.

Messages submitted through the llama.cpp web interface are also mirrored. Each browser
conversation maps to a Matrix thread containing its prompts and streamed responses. Its header is
`🟢 Local: <title>  ·  llama.cpp Web UI`, matching the existing agent-thread convention. The
header begins as `New Chat` and is edited to the first non-empty line of the first user message.
The header and model responses are posted by `AI-bot`; prompts are posted inside the thread under
the configured human Matrix identity. The conversation history sent by the browser is imported
into that thread's JSON history, allowing subsequent Matrix replies to continue with the same
context. The mapping uses llama.cpp's
`X-Conversation-Id` header and survives service restarts.

Ordinary API clients remain private by default. To deliberately mirror an API chat-completion
request, send the header `X-Matrix-Mirror: true` through port 9321. The proxy removes this internal
header before forwarding the request to llama.cpp.

After 30 minutes without a local-model request, the bot warns in the most recently active local
thread. There is then a three-minute grace period. Reply exactly:

```text
DO NOT CLEAR CACHE
```

The bot performs a real inference keepalive and resets the 30-minute timer. Without the veto,
llama.cpp sleeps at 33 minutes and releases the model and KV cache. It automatically reloads on the
next prompt. Sleeping never deletes conversation history.

Both listeners use localhost and llama.cpp requires its generated API key. Do not bind either
listener to `0.0.0.0` without adding a firewall and TLS reverse proxy.

## Updating

Updates are deliberate because both computers should use compatible binaries and the same model.
Choose and test a new llama.cpp commit on both backends, update the pinned commit in this document,
rebuild, then restart:

```bash
systemctl --user restart llama-server claude-slack-bot
```
