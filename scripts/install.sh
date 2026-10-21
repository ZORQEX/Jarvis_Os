#!/usr/bin/env bash
# Install JARVIS WorkOS for the current user (no root, no system changes).
#   scripts/install.sh            launcher in ~/.local/bin + app-menu entry
#   scripts/install.sh --service  also a systemd user service for the core (starts at login)
#   scripts/install.sh --uninstall remove everything this script added (user data is kept)
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
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Удалено. Данные остаются в ~/.local/share/jarvis и ~/.config/jarvis."
  exit 0
fi

[[ -x "$ROOT/.venv/bin/python" ]] || (cd "$ROOT" && uv sync --frozen)
mkdir -p "$(dirname "$BIN")" "$(dirname "$APP")" "$(dirname "$ICON")"
if [[ -e $BIN && ! -L $BIN ]]; then echo "$BIN уже существует и не является ссылкой — не трогаю"; else ln -sf "$ROOT/bin/jarvis" "$BIN"; fi
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

# Speech recognition on this computer: GigaAM v3 (Sber, MIT), int8 build (~230 MB, ~520 MB RAM), own virtualenv.
STT="$HOME/.local/share/jarvis/stt"
if [[ ! -x "$STT/.venv/bin/python" ]]; then
  mkdir -p "$STT" && (cd "$STT" && uv venv -q --python 3.12 .venv \
    && uv pip install -q --python .venv/bin/python "onnx-asr[cpu,hub]==0.12.0")
fi
HF_HOME="$STT/hf" "$STT/.venv/bin/python" -c "import onnx_asr; onnx_asr.load_model('gigaam-v3-e2e-rnnt', quantization='int8')"

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
