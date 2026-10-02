#!/usr/bin/env bash
# Installs Phone Mic Router for the current user:
#   * a Python virtual environment in ./.venv with the dependencies
#   * a launcher in ~/.local/bin/phone-mic-router
#   * a desktop menu entry
# Nothing is installed system-wide and nothing is installed on any phone.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

need() { command -v "$1" >/dev/null 2>&1; }

echo "== Phone Mic Router installer =="
need python3 || { echo "python3 is required"; exit 1; }
python3 - <<'PY' || { echo "Python 3.10 or newer is required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

for tool in pw-cat pw-loopback pw-dump pactl; do
  need "$tool" || echo "warning: '$tool' not found. Install your distribution's PipeWire tools (see README)."
done
need adb || echo "note: 'adb' not found. USB microphones need it (android-tools / adb package). Wi-Fi works without it."

if [ ! -d "$VENV" ]; then
  # Reuse a distribution-provided PySide6/numpy when available (saves ~500 MB download).
  if python3 -c "import PySide6, numpy" 2>/dev/null; then
    echo "Using system PySide6/numpy (venv with --system-site-packages)"
    python3 -m venv --system-site-packages "$VENV"
  else
    python3 -m venv "$VENV"
  fi
fi
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r "$ROOT/requirements.txt"

mkdir -p "$BIN" "$APPS"
cat > "$BIN/phone-mic-router" <<SH
#!/usr/bin/env bash
cd "$ROOT" && exec "$VENV/bin/python" -m app "\$@"
SH
chmod +x "$BIN/phone-mic-router"

sed "s|@LAUNCHER@|$BIN/phone-mic-router|; s|@ICON@|$ROOT/app/web/icon.svg|" \
  "$ROOT/packaging/phone-mic-router.desktop" > "$APPS/phone-mic-router.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo
echo "Installed. Start it from your application menu or run:  phone-mic-router"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "(add $BIN to your PATH to run it from a terminal)";; esac
echo "Optional headless service: see packaging/phone-mic-router.service"
