#!/usr/bin/env bash
# Opt-in Omarchy integration: Waybar indicator + Hyprland key bindings (Super+H, Super+Shift+H).
#   scripts/omarchy-integrate.sh apply    back up configs, add marked blocks, reload bar/compositor
#   scripts/omarchy-integrate.sh remove   remove only the marked blocks
# Paths can be overridden for testing: HYPR_CONF, WAYBAR_CONF, WAYBAR_CSS, NO_RELOAD=1.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HYPR_CONF=${HYPR_CONF:-$HOME/.config/hypr/hyprland.conf}
WAYBAR_CONF=${WAYBAR_CONF:-$HOME/.config/waybar/config.jsonc}
WAYBAR_CSS=${WAYBAR_CSS:-$HOME/.config/waybar/style.css}
MARK="jarvis-workos"
cmd=${1:-}

backup() {
  local dir="$HOME/.local/share/jarvis/backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$dir"
  for f in "$HYPR_CONF" "$WAYBAR_CONF" "$WAYBAR_CSS"; do [[ -f $f ]] && cp -p "$f" "$dir/"; done
  echo "Резервные копии: $dir"
}

python_edit() {
  python3 - "$1" "$WAYBAR_CONF" "$WAYBAR_CSS" "$ROOT" "$MARK" <<'PY'
import json, re, sys
mode, conf, css, root, mark = sys.argv[1:]
text = open(conf, encoding="utf-8").read()
block = open(f"{root}/config/omarchy/waybar-jarvis.jsonc", encoding="utf-8").read()
block = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("//"))
def strip_comments(s):
    """Remove // and /* */ comments outside strings, then trailing commas (JSONC -> JSON)."""
    out, i, n, in_str = [], 0, len(s), False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(s[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True; out.append(c)
        elif s.startswith("//", i):
            while i < n and s[i] != "\n":
                i += 1
            continue
        elif s.startswith("/*", i):
            i = s.index("*/", i) + 2; continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))
if mode == "apply" and mark not in text:
    text = text.replace('"modules-right": [', f'"modules-right": [\n    "custom/jarvis", // {mark}', 1)
    first = text.index("{") + 1
    text = text[:first] + f"\n  // {mark}:begin\n  " + block.strip().replace("\n", "\n  ") + f",\n  // {mark}:end" + text[first:]
elif mode == "remove":
    text = re.sub(rf'\n\s*"custom/jarvis", // {mark}', "", text)
    text = re.sub(rf'\n\s*// {mark}:begin.*?// {mark}:end', "", text, flags=re.S)
json.loads(strip_comments(text))          # refuse to write an invalid config
open(conf, "w", encoding="utf-8").write(text)
style = open(css, encoding="utf-8").read() if __import__("os").path.exists(css) else ""
add = open(f"{root}/config/omarchy/waybar-jarvis.css", encoding="utf-8").read()
if mode == "apply" and mark not in style:
    style += f"\n/* {mark}:begin */\n{add}/* {mark}:end */\n"
elif mode == "remove":
    style = re.sub(rf'\n?/\* {mark}:begin \*/.*?/\* {mark}:end \*/\n?', "\n", style, flags=re.S)
open(css, "w", encoding="utf-8").write(style)
PY
}

case $cmd in
  apply)
    command -v jarvis >/dev/null || { echo "Сначала запустите scripts/install.sh"; exit 1; }
    backup
    python_edit apply            # validates the result before writing; aborts on error
    grep -q "$MARK" "$HYPR_CONF" || printf '\nsource = %s/config/omarchy/hypr-jarvis.conf # %s\n' "$ROOT" "$MARK" >> "$HYPR_CONF"
    if [[ -z ${NO_RELOAD:-} ]]; then hyprctl reload >/dev/null; omarchy-restart-waybar >/dev/null 2>&1 || pkill -SIGUSR2 waybar || true; fi
    echo "Готово: Super+H — открыть JARVIS, Super+Shift+H — экстренная остановка, индикатор в верхней панели.";;
  remove)
    sed -i "/# $MARK\$/d" "$HYPR_CONF"
    python_edit remove
    if [[ -z ${NO_RELOAD:-} ]]; then hyprctl reload >/dev/null; omarchy-restart-waybar >/dev/null 2>&1 || pkill -SIGUSR2 waybar || true; fi
    echo "Интеграция удалена.";;
  *) sed -n '2,6p' "$0"; exit 2;;
esac
