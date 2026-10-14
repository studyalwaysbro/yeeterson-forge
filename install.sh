#!/bin/sh
# Yeeterson Forge installer. It is short on purpose: read it before you run it.
#
#   curl -fsSL https://github.com/studyalwaysbro/yeeterson-forge/releases/latest/download/install.sh | sh
#   ... | sh -s -- github:studyalwaysbro/godslayer      also start managing these addons
#
# What it does, all inside your home folder and without sudo:
#   1. downloads forge.py and SHA256SUMS from the newest GitHub release and checks one against the other
#   2. puts forge.py in ~/.local/share/yeeterson-forge and links it as ~/.local/bin/yforge
#   3. runs "yforge init" to find the game, adds the addons you named, and turns the update timer on
# Undo: yforge timer off; rm -r ~/.local/share/yeeterson-forge ~/.local/bin/yforge ~/.config/yeeterson-forge
set -eu

REPO="studyalwaysbro/yeeterson-forge"
BASE="${YFORGE_BASE_URL:-https://github.com/$REPO/releases/latest/download}"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/yeeterson-forge"
BIN="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
die() { say "yforge install: $*" >&2; exit 1; }

get() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --tlsv1.2 "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --https-only "$1" -O "$2"
    else
        die "need curl or wget"
    fi
}

main() {
    [ "$(id -u)" -ne 0 ] || die "run this as yourself, not as root or with sudo"
    command -v python3 >/dev/null 2>&1 || die "python3 is missing. Install it with your package manager and run this again."
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is needed"

    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    say "Downloading Yeeterson Forge..."
    get "$BASE/forge.py" "$tmp/forge.py" || die "could not download forge.py"
    get "$BASE/SHA256SUMS" "$tmp/SHA256SUMS" || die "could not download SHA256SUMS"
    python3 - "$tmp" <<'PY' || die "forge.py does not match its published checksum. Nothing was installed."
import hashlib, sys
from pathlib import Path
tmp = Path(sys.argv[1])
want = {name.lstrip("*"): digest for digest, name in (line.split() for line in (tmp / "SHA256SUMS").read_text().splitlines() if line.strip())}
sys.exit(hashlib.sha256((tmp / "forge.py").read_bytes()).hexdigest() != want.get("forge.py"))
PY

    mkdir -p "$DEST" "$BIN"
    cp "$tmp/forge.py" "$DEST/forge.py.new"
    chmod 755 "$DEST/forge.py.new"
    mv -f "$DEST/forge.py.new" "$DEST/forge.py"
    ln -sf "$DEST/forge.py" "$BIN/yforge"
    say "Installed Yeeterson Forge $("$BIN/yforge" --version) to $DEST"

    config="${YFORGE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/yeeterson-forge}/forge.json"
    if [ ! -f "$config" ]; then
        "$BIN/yforge" init || {
            say ""
            say "The forge is installed but does not know where the game is yet."
            say "Finish with the yforge init line above, then:  yforge timer on"
            path_note
            exit 0
        }
    fi
    for addon in "$@"; do
        "$BIN/yforge" add "$addon" || say "Could not add $addon yet. Try again later with: yforge add $addon"
    done
    "$BIN/yforge" timer on || say "No background timer on this system. Run 'yforge update' when you want updates."
    say ""
    say "Done. See what it manages with: yforge status"
    path_note
}

path_note() {
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) say "Note: $BIN is not on your PATH. Open a new terminal, or run it as $BIN/yforge" ;;
    esac
}

main "$@"
