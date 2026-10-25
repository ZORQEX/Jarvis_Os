#!/usr/bin/env bash
# OpenClaw and Hermes Agent for JARVIS (from morph-rq/jarvis-os, docs/architecture/ADR-008-agents.md).
# User-level only: no root, nothing system-wide. Every package is pinned (config/agents/):
#   Node 24.22.1      official tarball, sha256-checked (OpenClaw needs Node >=24.22.1 <25)
#   OpenClaw          npm ci --ignore-scripts from package-lock.json (integrity hashes)
#   Hermes Agent      its own venv, pip --require-hashes --only-binary from hermes.lock (Python 3.13 through uv)
# Each version goes into its own folder; `current` moves only after the new one runs, the old one stays for
# going back. Their own data stays where they keep it (~/.openclaw, ~/.hermes). JARVIS starts them as its
# children on loopback with a token it keeps in its secrets (jarvis/agents_backends.py).
#   scripts/install-agents.sh            install (or check) both
#   scripts/install-agents.sh --remove   remove the installed copies (their data in ~/.openclaw, ~/.hermes stays)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AG="${JARVIS_AGENTS_DIR:-$HOME/.local/share/jarvis/agents}"
LOCKS="$ROOT/config/agents"
step() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

if [[ ${1:-} == --remove ]]; then
  rm -rf "$AG/node" "$AG/openclaw" "$AG/hermes"
  echo "Удалено: $AG/{node,openclaw,hermes}. Данные агентов (~/.openclaw, ~/.hermes) не тронуты."
  exit 0
fi

# https only, size ceiling, used only when the sha256 matches (also when the file is already there).
fetch_verified() {
  local url="$1" sha="$2" dest="$3" max="$4" size
  if [[ -f $dest ]] && echo "$sha  $dest" | sha256sum -c --quiet - 2>/dev/null; then return 0; fi
  curl -sSL --fail --proto '=https' --tlsv1.2 --max-time 600 --max-filesize "$max" "$url" | head -c "$((max + 1))" > "$dest.tmp" || true
  size=$(stat -c %s "$dest.tmp" 2>/dev/null || echo 0)
  if (( size > max )); then rm -f "$dest.tmp"; warn "$(basename "$dest") больше допустимого — не установлено"; return 1; fi
  if ! echo "$sha  $dest.tmp" | sha256sum -c --quiet -; then rm -f "$dest.tmp"; warn "контрольная сумма $(basename "$dest") не совпала — не установлено"; return 1; fi
  mv "$dest.tmp" "$dest"
}

mkdir -p "$AG/node" "$AG/openclaw" "$AG/hermes"

# ---------------------------------------------------------------- OpenClaw on its own Node 24
node_v=24.22.1
case "$(uname -m)" in
  x86_64) node_arch=x64; node_sha=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6 ;;
  aarch64) node_arch=arm64; node_sha=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e ;;
  *) node_arch="" ;;
esac
oc_v=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dependencies"]["openclaw"])' "$LOCKS/openclaw/package.json")
step "OpenClaw $oc_v (npm lock, install scripts off) on Node $node_v"
if [[ -z $node_arch ]]; then
  warn "нет сборки Node для $(uname -m); OpenClaw пропущен"
elif fetch_verified "https://nodejs.org/dist/v$node_v/node-v$node_v-linux-$node_arch.tar.xz" "$node_sha" \
       "$AG/node/node-v$node_v.tar.xz" $((96 << 20)); then
  if [[ ! -x $AG/node/$node_v/bin/node ]]; then
    mkdir -p "$AG/node/$node_v"
    tar -xJf "$AG/node/node-v$node_v.tar.xz" -C "$AG/node/$node_v" --strip-components=1
  fi
  ln -sfn "$node_v" "$AG/node/current"
  if [[ ! -x $AG/openclaw/$oc_v/node_modules/.bin/openclaw ]]; then
    rm -rf "$AG/openclaw/$oc_v.tmp" && mkdir -p "$AG/openclaw/$oc_v.tmp"
    cp "$LOCKS/openclaw/package.json" "$LOCKS/openclaw/package-lock.json" "$AG/openclaw/$oc_v.tmp/"
    if (cd "$AG/openclaw/$oc_v.tmp" && PATH="$AG/node/current/bin:$PATH" npm ci --ignore-scripts --omit=dev \
          --no-audit --no-fund --loglevel=error); then
      rm -rf "$AG/openclaw/$oc_v" && mv "$AG/openclaw/$oc_v.tmp" "$AG/openclaw/$oc_v"
    else
      rm -rf "$AG/openclaw/$oc_v.tmp"
      warn "OpenClaw не установился (npm ci); JARVIS работает и без него"
    fi
  fi
  if PATH="$AG/node/current/bin:$PATH" "$AG/openclaw/$oc_v/node_modules/.bin/openclaw" --version >/dev/null 2>&1; then
    ln -sfn "$oc_v" "$AG/openclaw/current"
    echo "   $(PATH="$AG/node/current/bin:$PATH" "$AG/openclaw/current/node_modules/.bin/openclaw" --version 2>/dev/null | head -1)"
  fi
fi

# ---------------------------------------------------------------- Hermes Agent (hash-locked venv)
hermes_v=$(grep -oE '^hermes-agent(\[[a-z,]+\])?==[^ ]+' "$LOCKS/hermes.lock" | cut -d= -f3)
step "Hermes Agent $hermes_v (venv from hermes.lock, hashes required)"
hdir="$AG/hermes/$hermes_v"
if [[ ! -x $hdir/bin/hermes ]]; then
  rm -rf "$hdir"     # built in its final folder (a venv records its path); `current` moves once it is complete
  if uv venv -q --python 3.13 "$hdir" && uv pip install -q --python "$hdir/bin/python" pip \
     && "$hdir/bin/python" -m pip install --disable-pip-version-check -q --require-hashes --only-binary :all: \
          --no-deps -r "$LOCKS/hermes.lock"; then
    :
  else
    rm -rf "$hdir"
    warn "Hermes не установился (нужен uv и Python 3.13); JARVIS работает и без него"
  fi
fi
if [[ -x $hdir/bin/hermes ]]; then
  ln -sfn "$hermes_v" "$AG/hermes/current"
  echo "   $("$hdir/bin/hermes" --version 2>/dev/null | head -1)"
fi
echo "Готово. JARVIS запустит агентов сам (Настройки → Возможности)."
