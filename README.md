# usagebar

Your Claude Code plan quota in the macOS menu bar, plus an MCP tool so agents can
check the quota and their own context size before starting big work.

Not affiliated with Anthropic. It reads what `claude -p /usage` prints and the
transcripts Claude Code keeps in `~/.claude/projects/`. Neither format is documented,
so a Claude Code release can break parsing until usagebar is updated.

## Install

    curl -fsSL https://usagebar.dnlbit.com/install.sh | sh

Installs into `~/.local/share/usagebar` (Python venv with `rumps`), starts at login,
and prints the `claude mcp add` command for the MCP tool. Needs macOS, `python3` 3.9+
and `claude` on your PATH.

Updates: the app checks GitHub Releases once a day and shows "Update to vX.Y.Z" in
its menu. Click it to install and restart. Nothing updates without that click.
Rerunning the install command also updates.

Uninstall:

    launchctl bootout gui/$(id -u)/com.dnlbit.usagebar
    rm -rf ~/.local/share/usagebar ~/Library/LaunchAgents/com.dnlbit.usagebar.plist \
      ~/.config/usagebar.json ~/.cache/usagebar
    claude mcp remove usagebar -s user

## Menu bar

The bar shows the current session quota (`▰▰▱▱▱ 48%`). Hover for the reset time, click
for the full `/usage` output, with "in 1h 52m" after each reset time.

Settings… holds:

- Idle interval (default 20 min): poll rate while usage is flat.
- Active interval (default 5 min): used after a poll sees the percentages change,
  until a poll sees no change.
- Auto start at login: writes `~/Library/LaunchAgents/com.dnlbit.usagebar.plist`
  (unchecking removes it). Takes effect at next login.

Intervals are saved to `~/.config/usagebar.json`. Log: `/tmp/usagebar.log`.

## MCP tool

`mcp_server.py` (standard library only) gives agents one tool, `get_usage`, returning:

- `plan`: the last poll, read from `~/.cache/usagebar/last.json` (written by the menu
  bar app, so the app must be running; `fetched_at` says how fresh it is).
- `context`: tokens in the calling Claude Code session's context, the model's max
  window (hard-coded table in `mcp_server.py`) and percent used.

Either part comes back as `{"error": ...}` on its own if it fails.

## Privacy

Everything stays on your machine. The only network requests are the ones `claude`
makes itself and the daily update check to `api.github.com`.

## Development

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    ./run.sh
    python3 monitor.py --test && python3 mcp_server.py --test

A git checkout never offers updates. Release: bump `__version__` in `monitor.py`,
commit, then `gh release create vX.Y.Z --generate-notes`.
