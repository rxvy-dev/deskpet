#!/usr/bin/env bash
# Sets up a local LLM so you can talk to your deskpets (and they can talk to each other).
#   ./setup-llm.sh                 interactive
#   ./setup-llm.sh --model NAME    pick the model without asking
# Installs Ollama (GPU build for your card), starts it, downloads an abliterated model,
# and points deskpet's config at it. Everything runs locally - nothing is sent anywhere.
set -euo pipefail

MODEL=""
if [[ "${1:-}" == "--model" && -n "${2:-}" ]]; then MODEL="$2"; fi

say() { printf '\033[1;32m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# 1. ollama, with the right GPU backend
if ! command -v ollama >/dev/null; then
    pkg=ollama
    if lspci 2>/dev/null | grep -qiE 'vga.*nvidia|3d.*nvidia'; then
        pkg=ollama-cuda
    elif lspci 2>/dev/null | grep -qiE 'vga.*(amd|ati)|display.*(amd|ati)'; then
        pkg=ollama-rocm
    fi
    say "installing $pkg"
    sudo pacman -S --needed "$pkg"
else
    say "ollama is already installed ($(ollama --version 2>/dev/null | head -1))"
fi

# 2. start it (and on boot)
if ! systemctl is-active --quiet ollama; then
    say "starting the ollama service"
    sudo systemctl enable --now ollama
fi
for _ in $(seq 1 30); do
    curl -fs http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
    sleep 1
done
curl -fs http://127.0.0.1:11434/api/version >/dev/null || { warn "ollama didn't come up - check: journalctl -u ollama"; exit 1; }

# 3. pick a model
if [[ -z "$MODEL" ]]; then
    echo
    echo "Which abliterated model? (they run on your GPU; ollama unloads it 5 min after the last chat)"
    echo "  1) huihui_ai/qwen3.5-abliterated:9b   6.6 GB  - best replies, good for 12-16 GB GPUs   [default]"
    echo "  2) huihui_ai/qwen3.5-abliterated:4B   3.3 GB  - lighter, nicer if you game while pets chat"
    echo "  3) huihui_ai/gemma3-abliterated:1b    0.8 GB  - tiny and fast, sillier"
    echo "  4) something else (any ollama model name)"
    read -rp "> " pick
    case "${pick:-1}" in
        2) MODEL="huihui_ai/qwen3.5-abliterated:4B" ;;
        3) MODEL="huihui_ai/gemma3-abliterated:1b" ;;
        4) read -rp "model name: " MODEL ;;
        *) MODEL="huihui_ai/qwen3.5-abliterated:9b" ;;
    esac
fi

say "downloading $MODEL (this is the big part)"
ollama pull "$MODEL"

# 4. point deskpet at it
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/deskpet/config.json"
mkdir -p "$(dirname "$CFG")"
python3 - "$CFG" "$MODEL" <<'EOF'
import json, os, sys
path, model = sys.argv[1], sys.argv[2]
cfg = {}
if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except ValueError:
        pass
llm = cfg.setdefault("llm", {})
llm.update({"enabled": True, "backend": "ollama", "url": "http://127.0.0.1:11434", "model": model})
cfg.setdefault("chatter", {"enabled": True, "min_gap": 45, "max_gap": 150, "turns": 4})
json.dump(cfg, open(path, "w"), indent=2)
print("updated", path)
EOF

# 5. say hi
DESKPET="$(command -v deskpet || echo "$HOME/.local/bin/deskpet")"
if [[ -x "$DESKPET" ]]; then
    say "testing (the first reply takes a few seconds while the model loads)..."
    "$DESKPET" --ask tux "hi! introduce yourself in one sentence" || warn "the test failed - see the error above"
fi
echo
say "done. Restart deskpet, then double-click a pet (or right-click > talk...) to chat."
