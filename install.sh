#!/bin/sh
# usagebar installer:
#   curl -fsSL https://usagebar.dnlbit.com/install.sh | sh
# Rerun it to update. --update (used by the in-app updater, which restarts itself)
# only replaces files and dependencies: it leaves the LaunchAgent and running app alone.
set -eu

REPO=dnlbit/usagebar
LABEL=com.dnlbit.usagebar
DIR="${USAGEBAR_DIR:-$HOME/.local/share/usagebar}"

fail() { echo "usagebar: $*" >&2; exit 1; }

[ "$(uname)" = Darwin ] || fail "macOS only"
PY=$(command -v python3) || fail "python3 not found (xcode-select --install, or brew install python)"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "python3 3.10 or newer needed"

TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" |
  sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
[ -n "$TAG" ] || fail "could not find the latest release of $REPO"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz" | tar -xz -C "$tmp" --strip-components 1

mkdir -p "$DIR"
cp "$tmp/monitor.py" "$tmp/mcp_server.py" "$tmp/run.sh" "$tmp/requirements.txt" "$DIR/"
[ -x "$DIR/.venv/bin/python" ] || "$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$DIR/requirements.txt"
"$DIR/.venv/bin/python" "$DIR/monitor.py" --test >/dev/null || fail "self-check failed"

if [ "${1:-}" = --update ]; then
  echo "usagebar updated to $TAG"
  exit 0
fi

"$DIR/.venv/bin/python" "$DIR/monitor.py" --autostart
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true  # stop an older copy
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist"

echo "usagebar $TAG installed in $DIR and running in the menu bar."
echo "Give Claude Code agents the get_usage tool:"
echo "  claude mcp add usagebar -s user -- \"$DIR/.venv/bin/python\" \"$DIR/mcp_server.py\""
