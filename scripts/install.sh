#!/usr/bin/env bash
# Install JARVIS WorkOS for the current user (no root, no system changes).
#   scripts/install.sh            launcher in ~/.local/bin + app-menu entry
#   scripts/install.sh --service  also a systemd user service for the core (starts at login)
#   scripts/install.sh --uninstall remove everything this script added (user data is kept)
#   JARVIS_NO_AGENTS=1 scripts/install.sh   skip OpenClaw and Hermes (scripts/install-agents.sh, ~0.9 GB on disk)
# Waybar module and Hyprland key bindings are opt-in: see config/omarchy/README.md.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$HOME/.local/bin/jarvis"
APP="$HOME/.local/share/applications/jarvis-workos.desktop"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/jarvis-workos.svg"
UNIT="$HOME/.config/systemd/user/jarvis-workos.service"

if [[ ${1:-} == --uninstall ]]; then
  systemctl --user disable --now jarvis-workos.service 2>/dev/null || true
  rm -f "$UNIT" "$APP" "$ICON"
  [[ -L $BIN ]] && rm -f "$BIN"
  [[ -L $HOME/.local/bin/jarvis-agent-hook ]] && rm -f "$HOME/.local/bin/jarvis-agent-hook"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Удалено. Данные остаются в ~/.local/share/jarvis и ~/.config/jarvis."
  exit 0
fi

[[ -x "$ROOT/.venv/bin/python" ]] || (cd "$ROOT" && uv sync --frozen)
mkdir -p "$(dirname "$BIN")" "$(dirname "$APP")" "$(dirname "$ICON")"
if [[ -e $BIN && ! -L $BIN ]]; then echo "$BIN уже существует и не является ссылкой — не трогаю"; else ln -sf "$ROOT/bin/jarvis" "$BIN"; fi
# The relay coding agents' hooks run (jarvis/hub/coding.py): registered only when the person presses «Подключить» in
# «Возможности → На компьютере»; a stable path so the hooks survive a moved checkout.
ln -sf "$ROOT/bin/jarvis-agent-hook" "$HOME/.local/bin/jarvis-agent-hook"
cp "$ROOT/web/icon.svg" "$ICON"
cat > "$APP" <<DESK
[Desktop Entry]
Type=Application
Name=JARVIS
GenericName=Ассистент
Comment=JARVIS WorkOS — голосовой ассистент
Exec=$BIN
Icon=jarvis-workos
Terminal=false
Categories=Utility;Office;
StartupWMClass=jarvis-workos
DESK

# Voice of JARVIS: Silero (Russian, local, no quota) in its own virtualenv, run as a separate process
# (jarvis/voice/silero_worker.py). Silero models: non-commercial licence. ~0.9 GB with CPU-only PyTorch.
SILERO="$HOME/.local/share/jarvis/silero"
if [[ ! -x "$SILERO/.venv/bin/python" ]]; then
  mkdir -p "$SILERO/models" && (cd "$SILERO" && uv venv -q --python 3.12 .venv \
    && uv pip install -q --python .venv/bin/python --index-url https://download.pytorch.org/whl/cpu "torch==2.7.1" \
    && uv pip install -q --python .venv/bin/python "numpy<2.3" "num2words==0.5.14")
fi
[[ -s "$SILERO/models/v5_2_ru.pt" ]] || curl -fsSL -o "$SILERO/models/v5_2_ru.pt" "https://models.silero.ai/models/tts/ru/v5_2_ru.pt"
# German and Ukrainian voices (same worker, loaded on their first phrase).
[[ -s "$SILERO/models/v3_de.pt" ]] || curl -fsSL -o "$SILERO/models/v3_de.pt" "https://models.silero.ai/models/tts/de/v3_de.pt"
[[ -s "$SILERO/models/v4_ua.pt" ]] || curl -fsSL -o "$SILERO/models/v4_ua.pt" "https://models.silero.ai/models/tts/ua/v4_ua.pt"

# The main voice: Supertonic 3 (Supertone; model OpenRAIL-M, code MIT) — one voice for every language JARVIS speaks,
# ~99M parameters, ONNX on the CPU, own virtualenv, separate process (jarvis/voice/supertonic_worker.py). ~390 MB.
SUPERTONIC="$HOME/.local/share/jarvis/supertonic"
if [[ ! -x "$SUPERTONIC/.venv/bin/python" ]]; then
  mkdir -p "$SUPERTONIC" && (cd "$SUPERTONIC" && uv venv -q --python 3.12 .venv \
    && uv pip install -q --python .venv/bin/python "supertonic==1.3.1" "num2words==0.5.14")
fi
if [[ ! -s "$SUPERTONIC/models/onnx/vector_estimator.onnx" ]]; then
  SUPERTONIC_CACHE_DIR="$SUPERTONIC/models" "$SUPERTONIC/.venv/bin/python" -c \
    "from supertonic import TTS; TTS(model='supertonic-3', auto_download=True)"
fi

# English (British/American), Spanish, French, Italian, Portuguese voices: Kokoro-82M (Apache-2.0), int8 ONNX, own
# virtualenv, separate process (jarvis/voice/kokoro_worker.py). ~120 MB of models.
KOKORO="$HOME/.local/share/jarvis/kokoro"
if [[ ! -x "$KOKORO/.venv/bin/python" ]]; then
  mkdir -p "$KOKORO/models" && (cd "$KOKORO" && uv venv -q --python 3.12 .venv \
    && uv pip install -q --python .venv/bin/python "kokoro-onnx==0.4.7")
fi
for f in kokoro-v1.0.int8.onnx voices-v1.0.bin; do
  [[ -s "$KOKORO/models/$f" ]] || curl -fsSL -o "$KOKORO/models/$f" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f"
done

# End of a phrase by the voice: Smart Turn v3.2 (Pipecat / Daily, BSD-2-Clause), int8 ONNX, ~8 MB, run by the speech
# worker (jarvis/voice/turn.py).
TURN_DIR="$HOME/.local/share/jarvis/stt/turn"
mkdir -p "$TURN_DIR"
[[ -s "$TURN_DIR/smart-turn-v3.2-cpu.onnx" ]] || curl -fsSL -o "$TURN_DIR/smart-turn-v3.2-cpu.onnx" \
  "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/smart-turn-v3.2-cpu.onnx"

# Speech recognition on this computer: GigaAM v3 (Sber, MIT), int8 build (~230 MB, ~520 MB RAM), own virtualenv.
STT="$HOME/.local/share/jarvis/stt"
if [[ ! -x "$STT/.venv/bin/python" ]]; then
  mkdir -p "$STT" && (cd "$STT" && uv venv -q --python 3.12 .venv \
    && uv pip install -q --python .venv/bin/python "onnx-asr[cpu,hub]==0.12.0")
fi
HF_HOME="$STT/hf" "$STT/.venv/bin/python" -c "import onnx_asr; onnx_asr.load_model('gigaam-v3-e2e-rnnt', quantization='int8')"
# Other languages: Parakeet TDT 0.6B v3 (NVIDIA, CC-BY-4.0, 25 European languages), int8 (~670 MB). It re-hears only
# phrases that are not Russian; a Russian word list (FrequencyWords, CC-BY-SA-4.0) tells them apart.
HF_HOME="$STT/hf" "$STT/.venv/bin/python" -c "import onnx_asr; onnx_asr.load_model('nemo-parakeet-tdt-0.6b-v3', quantization='int8')"
[[ -s "$STT/ru_50k.txt" ]] || curl -fsSL -o "$STT/ru_50k.txt" "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/ru/ru_50k.txt"

# «Заказы»: the fashion-orders app (fashion/, git subtree of morph-rq/alina-fashion-ai), own virtualenv; JARVIS starts
# it (jarvis/fashion.py), data lives in ~/.local/share/jarvis/fashion. Its assistant uses Claude Code (the user's
# subscription). Customer PDFs need a Cyrillic font (Noto / Liberation / DejaVu); old .xls files need LibreOffice.
FASHION="$HOME/.local/share/jarvis/fashion"
mkdir -p "$FASHION" && (cd "$FASHION" && { [[ -x .venv/bin/python ]] || uv venv -q --python 3.12 .venv; } \
  && uv pip install -q --python .venv/bin/python -r "$ROOT/fashion/requirements.txt")

# Fast web search (jarvis/research.py, the research engine of morph-rq/jarvis-os): a local SearXNG — pinned git checkout
# (the commit jarvis-os tested) + its own venv. JARVIS starts it with the core on 127.0.0.1:8796, loopback only.
SEARX="$HOME/.local/share/jarvis/searxng"
SEARX_COMMIT=81371861f67226091d1c4df2e3908a11d3b54eb9
if [[ ! -d "$SEARX/src/searx" ]]; then
  mkdir -p "$SEARX" && git clone -q --filter=blob:none https://github.com/searxng/searxng.git "$SEARX/src" \
    && git -C "$SEARX/src" -c advice.detachedHead=false checkout -q "$SEARX_COMMIT"
fi
[[ -x "$SEARX/venv/bin/granian" ]] || (uv venv -q --python 3.12 "$SEARX/venv" && uv pip install -q \
  --python "$SEARX/venv/bin/python" -r "$SEARX/src/requirements.txt" -r "$SEARX/src/requirements-server.txt")

# Browser control for Claude Code tasks the user allows (Microsoft Playwright MCP, pinned; uses /usr/bin/chromium).
command -v npx >/dev/null && npx -y @playwright/mcp@0.0.83 --help >/dev/null 2>&1 || true

# OpenClaw and Hermes Agent (from morph-rq/jarvis-os; «Возможности», ADR-008): pinned Node 24 + OpenClaw (npm lock) and
# a hash-locked Hermes venv under ~/.local/share/jarvis/agents. Both think with Claude through the person's own sign-in.
if [[ -z ${JARVIS_NO_AGENTS:-} ]]; then
  "$ROOT/scripts/install-agents.sh" || echo "OpenClaw/Hermes не установились — JARVIS работает и без них"
fi

# Emergency fallback voice only (used if Silero cannot start): Piper (GPL-3.0, separate process), voice "denis" (CC0).
PIPER="$HOME/.local/share/jarvis/piper"
if [[ ! -x "$PIPER/.venv/bin/python" ]]; then
  mkdir -p "$PIPER/voices" && (cd "$PIPER" && uv venv -q --python 3.12 .venv && uv pip install -q --python .venv/bin/python "piper-tts==1.8.0")
fi
for f in ru_RU-denis-medium.onnx ru_RU-denis-medium.onnx.json; do
  [[ -s "$PIPER/voices/$f" ]] || curl -fsSL -o "$PIPER/voices/$f" "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/denis/medium/$f"
done

if [[ ${1:-} == --service ]]; then
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<UNITEOF
[Unit]
Description=JARVIS WorkOS core (local, loopback only)
After=graphical-session.target
PartOf=graphical-session.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
ExecStart=$ROOT/.venv/bin/python -m jarvis serve
WorkingDirectory=$ROOT
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=graphical-session.target
UNITEOF
  systemctl --user daemon-reload
  systemctl --user enable --now jarvis-workos.service
  echo "Служба jarvis-workos включена."
fi
echo "Готово. Запуск: jarvis (или JARVIS в меню приложений)."
