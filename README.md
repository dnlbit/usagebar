# usagebar

Your Claude Code plan quota in the macOS menu bar, plus an MCP tool so agents can
check the quota and their own context size before starting big work. An agent can
decide to wait for a reset, split work, or stop before it hits the limit.

<img src="docs/agent.png" width="800" alt="Claude Code calling get_usage and advising whether to start a big refactor before the quota reset">

<img src="docs/menu.png" width="651" alt="usagebar menu: session and weekly quota percentages with reset countdowns">

Not affiliated with Anthropic. It reads what `claude -p /usage` prints and the
transcripts Claude Code keeps in `~/.claude/projects/`. Neither format is documented,
so a Claude Code release can break parsing until usagebar is updated.

## Install

### Homebrew

    brew install dnlbit/tap/usagebar
    brew services start usagebar
    claude mcp add usagebar -s user -- "$(brew --prefix)/opt/usagebar/bin/usagebar-mcp"

`brew services start` starts the menu bar app now and at login. Update with
`brew upgrade usagebar`: the in-app updater is off for Homebrew installs. Needs
`claude` on your PATH (`~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin`).

Uninstall:

    brew services stop usagebar
    brew uninstall usagebar
    claude mcp remove usagebar -s user

### Install script

    curl -fsSL https://usagebar.dnlbit.com/install.sh | sh

Installs into `~/.local/share/usagebar` (Python venv with `rumps`), starts at login,
and prints the `claude mcp add` command for the MCP tool. Needs macOS, `python3` 3.10+
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

The bar shows the current session quota (`▰▰▱▱▱ 48%`), then the number of open Claude
Code sessions and how many need you (`▰▰▱▱▱ 48% 3 ⚠1`). Hover for the reset time, click
for each quota with "in 1h 52m" after its reset time.

Plans without quota lines in `/usage` (enterprise) show the sessions only (`◉3 ⚠1`).
With no quotas and no sessions the bar shows `◉`.

### Sessions

The menu lists each open Claude Code session, newest change first:
`<state> <name>   <context>% context   <status>`.

- `⚠` the session needs you, for example a permission prompt.
- `●` busy: Claude is working or running a tool.
- `○` idle: the turn is finished and the session waits for your next message.
- `·` a status this version does not know; the raw value shows.

Click a session to bring its terminal to the front. In Terminal.app and iTerm2 its tab
is selected (macOS asks once for Automation permission). In other apps (Ghostty,
VS Code, Cursor, Warp…) the app only comes to the front.

Session data comes from `~/.claude/sessions/*.json` and the transcripts in
`~/.claude/projects`. Claude Code does not document these files, so a Claude Code
update can change or break this part.

Settings… holds:

- Idle interval (default 20 min): poll rate while usage is flat.
- Active interval (default 5 min): used after a poll sees the percentages change,
  until a poll sees no change.
- Auto start at login: writes `~/Library/LaunchAgents/com.dnlbit.usagebar.plist`
  (unchecking removes it). Takes effect at next login. Homebrew installs have no
  checkbox: use `brew services start usagebar` to start at login.
- Notify: session needs you (default on): a notification when a session starts to wait
  for you.
- Notify: session finished (default off): a notification when a session goes from busy
  to idle.

Settings are saved to `~/.config/usagebar.json`. Log: `/tmp/usagebar.log`.

## MCP tool

`mcp_server.py` (standard library only) gives agents one tool, `get_usage`, returning:

- `plan`: quota percentages and reset times, shared with the menu bar app through
  `~/.cache/usagebar/last.json`. If that is older than `max_age_minutes` (default 2;
  0 forces it), the tool runs `claude -p /usage` itself (about 2s), so it works
  without the app. `fetched_at` says how fresh it is.
- `context`: tokens in the calling Claude Code session's context, the model's max
  window (hard-coded table in `monitor.py`) and percent used.

Either part comes back as `{"error": ...}` on its own if it fails. Example (`text`,
the raw `/usage` output, shortened):

```json
{
 "plan": {
  "fetched_at": "2026-10-13T22:53:05-07:00",
  "error": null,
  "quotas": {
   "Current session": {"percent_used": 41, "resets_at": "2026-10-13T23:59:00-07:00"},
   "Current week (all models)": {"percent_used": 16, "resets_at": "2026-10-20T15:59:00-07:00"},
   "Current week (Fable)": {"percent_used": 0, "resets_at": "2026-10-20T16:00:00-07:00"}
  },
  "text": "You are currently using your subscription to power your Claude Code usage\n\nCurrent session: 41% used …"
 },
 "context": {
  "session_id": "a659f724-dd01-40c3-bcbe-6a3d33bd43a6",
  "model": "claude-opus-5-5",
  "context_tokens": 135210,
  "window_tokens": 1000000,
  "percent_used": 13.5
 }
}
```

## Let your agents budget themselves

Paste this into your project or user `CLAUDE.md` (after the MCP setup above):

```markdown
## Usage budget
- Before long or parallel work, and about every hour during it, call the
  `mcp__usagebar__get_usage` tool.
- If the session quota is above 80%, finish the current step, then stop or wait
  for the reset time it reports.
- If context is above 50%, compact or hand off to a fresh session.
```

## Privacy

Everything stays on your machine. The only network requests are the ones `claude`
makes itself and the daily update check to `api.github.com`.

## Development

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    ./run.sh
    python3 monitor.py --test && python3 mcp_server.py --test

A git checkout never offers updates. Release: bump `__version__` in `monitor.py`,
commit, then `gh release create vX.Y.Z --generate-notes`. Tags are never moved or re-pointed; fix a bad release with a new version.
