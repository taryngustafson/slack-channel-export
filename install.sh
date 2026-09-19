#!/bin/sh
# Install slack-channel-export: set up its Python environment, then put a
# `slack-export` command on your PATH so it runs from any folder.
#
#   sh install.sh              install
#   sh install.sh --to DIR     install the command into DIR instead
#   sh install.sh --uninstall  remove the command (your exports are kept)
#   sh install.sh --help
#
# Safe to run again: it repeats only the steps that are not already done.
# Nothing here needs sudo unless you choose a folder that does.
set -u

COMMAND_NAME=slack-export
MARKER="# added by slack-channel-export install.sh"

# This script may be run from any folder, and through a symlink, so find the
# project folder from the script's own real path rather than the current folder.
SELF=$0
while [ -L "$SELF" ]; do
    LINK=$(readlink "$SELF")
    case $LINK in
        /*) SELF=$LINK ;;
         *) SELF=$(dirname "$SELF")/$LINK ;;
    esac
done
DIR=$(cd "$(dirname "$SELF")" && pwd)

TARGET=""
ACTION=install

while [ $# -gt 0 ]; do
    case $1 in
        --to) shift; [ $# -gt 0 ] || { echo "FATAL: --to needs a folder" >&2; exit 1; }
              TARGET=$1 ;;
        --uninstall) ACTION=uninstall ;;
        -h|--help)
            sed -n '2,10p' "$SELF" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "FATAL: unknown option '$1'. Try --help." >&2; exit 1 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }

# ── uninstall ────────────────────────────────────────────────────────────────
if [ "$ACTION" = uninstall ]; then
    removed=no
    for folder in ${TARGET:-} "$HOME/.local/bin" /usr/local/bin "$HOME/bin"; do
        [ -n "$folder" ] || continue
        link=$folder/$COMMAND_NAME
        # Only ever remove a link that points back at THIS project's wrapper.
        # Compared as text, so a link left dangling by a moved folder still goes.
        if [ -L "$link" ] && [ "$(readlink "$link")" = "$DIR/$COMMAND_NAME" ]; then
            rm -f "$link" && say "removed $link" && removed=yes
        fi
    done
    [ "$removed" = no ] && say "no $COMMAND_NAME command belonging to $DIR was found"
    if [ -f "$HOME/.zshrc" ] && grep -q "$MARKER" "$HOME/.zshrc"; then
        # Remove the PATH line this script added, and the marker above it.
        tmp=$(mktemp) && grep -v "$MARKER" "$HOME/.zshrc" > "$tmp" && mv "$tmp" "$HOME/.zshrc"
        say "removed the PATH line from ~/.zshrc"
    fi
    say ""
    say "Your exports and this folder are untouched."
    say "To remove the Python environment too:  rm -rf \"$DIR/.venv\""
    exit 0
fi

# ── 1. python ────────────────────────────────────────────────────────────────
say "1. Checking for Python"
if ! command -v python3 >/dev/null 2>&1; then
    say "   FATAL: python3 was not found."
    say "   On macOS, running 'python3' once in a terminal offers to install it"
    say "   as part of Apple's command line developer tools. Do that, then re-run."
    exit 1
fi
say "   found $(command -v python3) ($(python3 --version 2>&1))"

# ── 2. the project's own Python environment ──────────────────────────────────
say "2. Setting up the Python environment in $DIR/.venv"
if [ -x "$DIR/.venv/bin/python" ]; then
    say "   already there"
else
    python3 -m venv "$DIR/.venv" || { say "   FATAL: could not create $DIR/.venv"; exit 1; }
    say "   created"
fi
"$DIR/.venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r "$DIR/requirements.txt" || {
        say "   FATAL: could not install the dependency (needs an internet connection)."
        exit 1; }
say "   dependency installed: $(sed -n 1p "$DIR/requirements.txt")"

# ── 3. where the command goes ────────────────────────────────────────────────
say "3. Installing the '$COMMAND_NAME' command"
if [ -z "$TARGET" ]; then
    # /usr/local/bin is on every Mac's PATH already, so prefer it when it can be
    # written without sudo. Otherwise use ~/.local/bin, which needs no permission.
    if [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
        TARGET=/usr/local/bin
    else
        TARGET=$HOME/.local/bin
    fi
fi
mkdir -p "$TARGET" 2>/dev/null || true
if [ ! -d "$TARGET" ] || [ ! -w "$TARGET" ]; then
    say "   FATAL: cannot write to $TARGET."
    say "   Either re-run with a folder you own:   sh install.sh --to \"\$HOME/.local/bin\""
    say "   or, for a system-wide install:         sudo sh install.sh --to /usr/local/bin"
    exit 1
fi
ln -sf "$DIR/$COMMAND_NAME" "$TARGET/$COMMAND_NAME" || {
    say "   FATAL: could not create $TARGET/$COMMAND_NAME"; exit 1; }
say "   $TARGET/$COMMAND_NAME -> $DIR/$COMMAND_NAME"

# ── 4. make sure that folder is on PATH ──────────────────────────────────────
case ":$PATH:" in
    *":$TARGET:"*) ON_PATH=yes ;;
    *)             ON_PATH=no ;;
esac

if [ "$ON_PATH" = no ]; then
    say "4. Adding $TARGET to your PATH"
    if [ -w "$HOME/.zshrc" ] || [ ! -e "$HOME/.zshrc" ]; then
        printf '\nexport PATH="%s:$PATH"  %s\n' "$TARGET" "$MARKER" >> "$HOME/.zshrc"
        say "   added one line to ~/.zshrc (remove it, or run --uninstall, to undo)"
        say "   OPEN A NEW TERMINAL TAB for it to take effect."
    else
        say "   Add this line to your shell's startup file yourself:"
        say "       export PATH=\"$TARGET:\$PATH\""
    fi
    say "   (Using bash rather than zsh? Add the same line to ~/.bash_profile.)"
else
    say "4. $TARGET is already on your PATH"
fi

# ── done ─────────────────────────────────────────────────────────────────────
say ""
say "Installed."
say ""
say "  Next: store your Slack token, then check it. See SLACK-SETUP.md."
say "  Then: $COMMAND_NAME C0123456789      (from any folder)"
say ""
say "  Keep this folder - the command is a link to it, not a copy."
say "  To undo:  sh \"$DIR/install.sh\" --uninstall"
