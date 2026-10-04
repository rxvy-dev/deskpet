#!/usr/bin/env bash
# deskpet installer (per-user, no root needed except for the pacman deps)
#   ./install.sh            install / update
#   ./install.sh uninstall  remove the app (your own pets in ~/.local/share/deskpet/pets stay)
set -euo pipefail

APP="${XDG_DATA_HOME:-$HOME/.local/share}/deskpet/app"
BIN="$HOME/.local/bin"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "uninstall" ]]; then
    rm -rf "$APP" "$BIN/deskpet" "${XDG_DATA_HOME:-$HOME/.local/share}/applications/deskpet.desktop"
    echo "deskpet removed. (custom pets kept in ${XDG_DATA_HOME:-$HOME/.local/share}/deskpet/pets)"
    exit 0
fi

missing=()
python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null || missing+=(python-gobject gtk3)
python3 -c 'import cairo' 2>/dev/null || missing+=(python-cairo)
python3 -c 'import gi; gi.require_version("GtkLayerShell", "0.1"); from gi.repository import GtkLayerShell' 2>/dev/null || missing+=(gtk-layer-shell)
if ((${#missing[@]})); then
    echo "installing dependencies: ${missing[*]}"
    sudo pacman -S --needed "${missing[@]}"
fi

mkdir -p "$APP" "$BIN" "${XDG_DATA_HOME:-$HOME/.local/share}/deskpet/pets"
rm -rf "$APP/pets" "$APP/props"
cp "$SRC/deskpet.py" "$APP/deskpet.py"
cp -r "$SRC/pets" "$APP/pets"
cp -r "$SRC/props" "$APP/props"
chmod +x "$APP/deskpet.py"
ln -sf "$APP/deskpet.py" "$BIN/deskpet"

# app launcher entry (full paths, so it works even if ~/.local/bin isn't in PATH)
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
sed -e "s|@BIN@|$BIN/deskpet|g" -e "s|@ICON@|$APP/pets/tux/idle_0.png|g" "$SRC/deskpet.desktop" > "$APPS/deskpet.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "launcher: $APPS/deskpet.desktop"

echo "installed: $BIN/deskpet"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: $BIN isn't in your PATH - add it, or run $BIN/deskpet";; esac
echo
echo "try:  deskpet                      (Tux)"
echo "      deskpet --pet tux,cat,crab,floppy"
echo "      deskpet --new mypet           (make your own)"
echo
echo "to talk to them: ./setup-llm.sh   (installs ollama + an abliterated model)"
