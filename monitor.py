#!/usr/bin/env python3
"""macOS menu bar monitor for Claude Code usage quota (`claude -p /usage`) and open sessions."""

import glob
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import AppKit
    import rumps
except ImportError:  # --test mode runs without the dependency
    AppKit = rumps = None

__version__ = "0.2.0"
REPO = "dnlbit/usagebar"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.expanduser("~/.config/usagebar.json")
LOG_PATH = "/tmp/usagebar.log"
STATE_PATH = os.path.expanduser("~/.cache/usagebar/last.json")  # read by mcp_server.py
LABEL = "com.dnlbit.usagebar"
PLIST_DST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
# cold: poll slowly while usage is flat; warm: poll faster after a change
DEFAULTS = {"cold_minutes": 20, "warm_minutes": 5, "notify_waiting": True, "notify_finished": False}
SEGMENTS = 5
FILLED, EMPTY = "▰", "▱"
NEUTRAL = "◉"
# Live sessions: undocumented Claude Code files, one per running session (<pid>.json)
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
PROJECTS = os.path.expanduser("~/.claude/projects")
# Model max context windows (Anthropic docs). Claude Code auto-compacts earlier (500k for Opus 5.5).
WINDOWS = {
    model: 1_000_000 for model in (
        "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
        "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
        "claude-haiku-5-5",
    )
}
# Seen on Claude Code 2.1.299 (2026-10-15): running a tool -> "busy"; permission prompt ->
# "waiting" with waitingFor "permission prompt"; turn finished, waiting for the user -> "idle".
STATE_ICONS = {"waiting": "⚠", "busy": "●", "idle": "○"}

LINE_RE = re.compile(
    r"^(Current session|Current week \([^)]+\)): (\d+)% used(?: · resets (.+?) \((.+?)\))?$",
    re.M,
)


def noop(_):
    """Menu items with no callback render greyed out; this keeps them readable."""


def log(message):
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def fetch():
    """Run the usage command. Returns (text, error); one of them is None."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "/usage"],
            stdin=subprocess.DEVNULL,  # under mcp_server.py stdin is the JSON-RPC pipe; claude would read it
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.path.expanduser("~"),
        )
    except FileNotFoundError:
        return None, "claude not found in PATH"
    except subprocess.TimeoutExpired:
        return None, "claude -p /usage timed out after 120s"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    log("exit=%d %r" % (proc.returncode, output[:200]))
    if proc.returncode != 0:
        return None, "claude exited %d\n%s" % (proc.returncode, output)
    return output, None


def save_state(text, error):
    """Write the last poll for mcp_server.py. Replaced atomically so a reader never sees half a file."""
    state = {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "error": error,
        "quotas": {
            label: {"percent_used": pct, "resets_at": reset.isoformat() if reset else None}
            for label, (pct, reset) in (parse(text) if text else {}).items()
        },
        "text": text,
    }
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH + ".tmp", "w") as fh:
            json.dump(state, fh, indent=1)
        os.replace(STATE_PATH + ".tmp", STATE_PATH)
    except OSError as exc:
        log("state: %s" % exc)


def parse_reset(when, tzname):
    """'Sep 17 at 1:40pm' + tz name -> aware datetime, or None."""
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        return None
    now = datetime.now(tz)
    for year in (now.year, now.year + 1):  # December output read in January
        for fmt in ("%Y %b %d at %I:%M%p", "%Y %b %d at %I%p"):
            try:
                dt = datetime.strptime("%d %s" % (year, when), fmt).replace(tzinfo=tz)
            except ValueError:
                continue
            if dt >= now - timedelta(days=1):
                return dt
            break
    return None


def parse(text):
    """Map line label -> (percent, reset datetime) for the two quota lines.
    An unused quota (0%) prints no reset clause, so its reset is None."""
    out = {}
    for label, pct, when, tzname in LINE_RE.findall(text):
        out[label] = (int(pct), parse_reset(when, tzname) if when else None)
    return out


def menu_lines(text):
    """Lines up to the last quota line. The usage breakdown after it is wider than a
    laptop screen; unrecognised output comes back whole so nothing is hidden."""
    lines = text.splitlines()
    quota = [i for i, line in enumerate(lines) if LINE_RE.match(line)]
    return lines[:quota[-1] + 1] if quota else lines


def usage_key(text):
    """Percentages only, so reset-time jitter (3:59pm vs 4pm) is not a change."""
    return tuple(pct for _, (pct, _) in sorted(parse(text).items())) if text else None


def humanize(dt, now=None):
    if dt is None:
        return "?"
    now = now or datetime.now(dt.tzinfo)
    seconds = (dt - now).total_seconds()
    if seconds <= 0:
        return "now"
    hours, minutes = divmod(int(seconds // 60), 60)
    days, hours = divmod(hours, 24)
    if days:
        return "in %dd %dh" % (days, hours)
    return "in %dh %dm" % (hours, minutes) if hours else "in %dm" % minutes


def bar(pct):
    filled = min(SEGMENTS, max(0, round(pct * SEGMENTS / 100)))
    return "%s%s %d%%" % (FILLED * filled, EMPTY * (SEGMENTS - filled), pct)


def minutes(value, fallback):
    try:
        value = int(str(value).strip())
    except ValueError:
        return fallback
    return value if 1 <= value <= 1440 else fallback


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as fh:
            saved = json.load(fh)
        for key, default in DEFAULTS.items():
            value = saved.get(key, default)
            if isinstance(default, bool):
                cfg[key] = value if isinstance(value, bool) else default
            else:
                cfg[key] = minutes(value, default)
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, owned by another user
        return True
    return True


def read_sessions(directory=SESSIONS_DIR, alive=pid_alive, previous=None):
    """Live interactive sessions, newest status change first. Files of crashed sessions stay
    behind: a dead pid drops them. previous (pid -> session from the last poll) stands in for
    a file caught mid-write, so the session does not vanish and come back as new.
    shortcut: a reused pid shows a stale file; compare procStart if that bites."""
    out = []
    for path in glob.glob(os.path.join(directory, "*.json")):
        try:
            with open(path) as fh:
                session = json.load(fh)
            session["pid"] = pid = int(session["pid"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):  # mid-write or not a session file
            name = os.path.basename(path)[:-len(".json")]
            session = (previous or {}).get(int(name)) if name.isdigit() else None
            if session is None:
                continue
            pid = session["pid"]
        if session.get("kind", "interactive") != "interactive":
            continue
        if pid > 0 and alive(pid):
            out.append(session)
    return sorted(out, key=lambda s: s.get("statusUpdatedAt") or 0, reverse=True)


def session_state(session):
    """(icon, text) for a session's status; unknown values show raw so new states do not break the menu."""
    status = session.get("status") or "?"
    if status == "waiting":
        return STATE_ICONS[status], session.get("waitingFor") or status
    return STATE_ICONS.get(status, "·"), status


def session_events(previous, sessions):
    """(kind, session) for status changes since the last poll. previous maps pid -> session;
    sessions not in it are new (or the app just started) and never notify. A busy spell
    under 2 s (e.g. /context) is not a finished turn."""
    events = []
    for session in sessions:
        if session["pid"] not in previous:
            continue
        before = previous[session["pid"]]
        old, new = before.get("status"), session.get("status")
        if new == "waiting" and old != "waiting":
            events.append(("notify_waiting", session))
        elif old == "busy" and new == "idle":
            busy_ms = (session.get("statusUpdatedAt") or 0) - (before.get("statusUpdatedAt") or 0)
            if busy_ms >= 2000:
                events.append(("notify_finished", session))
    return events


def notify(title, subtitle, message):
    """Post a notification. rumps needs a bundle id on the Python it runs under; without
    one, osascript posts it instead (as Script Editor). Text goes as argv, never into the script."""
    try:
        rumps.notification(title, subtitle, message)
        return
    except Exception as exc:
        log("notify via rumps: %s" % exc)
    try:
        proc = subprocess.run(["osascript", "-e", NOTIFY_SCRIPT, title, subtitle, message],
                              capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:  # runs on the main thread: never hang the menu
        log("notify via osascript: timed out")
        return
    if proc.returncode:
        log("notify via osascript: %s" % proc.stderr.strip())


NOTIFY_SCRIPT = """on run argv
    display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)
end run"""


def session_name(session):
    return session.get("name") or os.path.basename(session.get("cwd") or "") or str(session["pid"])


def transcript_path(session_id):
    """Path of a session's transcript, or None. session_id must already be validated."""
    hits = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
    return hits[0] if hits else None


def last_usage(path):
    """(usage, model) of the last main-thread reply in a transcript, or None.
    Reads from the end, growing the window up to the last 4 MB."""
    size = os.path.getsize(path)
    chunk = 256 * 1024
    while True:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - chunk))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        if chunk < size:
            lines = lines[1:]  # cut mid-line by the seek
        for line in reversed(lines):
            if '"assistant"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:  # last line may still be mid-write
                continue
            msg = entry.get("message") or {}
            if (entry.get("type") == "assistant" and not entry.get("isSidechain")
                    and msg.get("usage") and msg.get("model") != "<synthetic>"):
                return msg["usage"], msg.get("model")
        if chunk >= size or chunk >= TAIL_MAX:
            return None
        chunk *= 4


TAIL_MAX = 4 * 1024 * 1024  # runs on the main thread: never read whole multi-MB transcripts


def context_tokens(usage):
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return sum(usage.get(k) or 0 for k in keys)


def context_label(path):
    """'7% context', '117k context' for an unknown model, or '' before the first reply."""
    found = last_usage(path)
    if not found:
        return ""
    tokens, window = context_tokens(found[0]), WINDOWS.get(found[1])
    return "%d%% context" % round(tokens * 100 / window) if window else "%dk context" % round(tokens / 1000)


def session_row(session, context):
    icon, state = session_state(session)
    return "   ".join(part for part in ("%s %s" % (icon, session_name(session)), context, state) if part)


def no_quota_plan(text):
    """True for /usage output of a plan without limits (enterprise): the known header or
    breakdown and no quota lines. Other output without quota lines (expired login, a new
    format) is not this and still shows a warning."""
    # Pro/Max output has the same header: any "% used" means quotas in a format we do not parse
    return bool(text) and not parse(text) and "% used" not in text and (
        "You are currently using your subscription" in text or "What's contributing to your" in text)


def quota_title(text, error):
    """Quota part of the title: the session bar, a warning on a failed fetch or unknown
    output, or None for a plan without quotas or before the first fetch."""
    session = parse(text).get("Current session") if text else None
    if session:
        return bar(session[0])
    if error or (text and not no_quota_plan(text)):
        return "\u26a0\ufe0e Claude"
    return None


def build_title(quota, sessions):
    """'▰▰▱▱▱ 48% 3 ⚠1', '◉3 ⚠1' without quotas, '◉' with neither."""
    if not sessions:
        return quota or NEUTRAL
    waiting = sum(s.get("status") == "waiting" for s in sessions)
    count = ("%s %d" % (quota, len(sessions))) if quota else "%s%d" % (NEUTRAL, len(sessions))
    return count + (" ⚠%d" % waiting if waiting else "")


def focus_session(pid):
    """Bring the session's terminal to the front: its tab in Terminal.app or iTerm2,
    else just the app that owns it. Runs osascript, so call it off the main thread."""
    try:
        tty = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        app, parent = None, pid
        while parent > 1 and app is None:
            parent = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(parent)],
                                        capture_output=True, text=True).stdout.strip() or 0)
            found = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(parent)
            if found and found.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular:
                app = found
    except (OSError, ValueError) as exc:
        log("focus %d: %s" % (pid, exc))
        return
    if app is None:
        log("focus %d: no terminal app found" % pid)
        return
    bundle = app.bundleIdentifier() or ""
    # the tab script for Terminal/iTerm2, else activate the app: osascript does it from a
    # background app, where macOS 14+ ignores NSRunningApplication.activate
    for script, arg in ((FOCUS_SCRIPTS.get(bundle), "/dev/" + tty), (ACTIVATE_SCRIPT, bundle)):
        if not script or arg in ("/dev/", "/dev/??"):
            continue
        try:
            proc = subprocess.run(["osascript", "-e", script, arg], capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:  # Automation permission dialog left open
            log("focus %d: osascript timed out" % pid)
            return
        if proc.returncode == 0:
            return
        log("focus %d: %s" % (pid, proc.stderr.strip()))


ACTIVATE_SCRIPT = """on run argv
    tell application id (item 1 of argv) to activate
end run"""


FOCUS_SCRIPTS = {
    "com.apple.Terminal": """on run argv
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is item 1 of argv then
                    set selected of t to true
                    set index of w to 1
                    activate
                    return
                end if
            end repeat
        end repeat
    end tell
    error "tty not found"
end run""",
    "com.googlecode.iterm2": """on run argv
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if tty of s is item 1 of argv then
                        select w
                        tell t to select
                        tell s to select
                        activate
                        return
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    error "tty not found"
end run""",
}


def autostart_enabled():
    return os.path.exists(PLIST_DST)


def set_autostart(enabled):
    """launchd picks the plist up at next login; not loaded now, which would
    start a second copy (RunAtLoad)."""
    if enabled:
        os.makedirs(os.path.dirname(PLIST_DST), exist_ok=True)
        # launchd starts with a bare PATH; these are where claude usually lives
        path = [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        with open(PLIST_DST, "wb") as fh:
            plistlib.dump({
                "Label": LABEL,
                "ProgramArguments": [os.path.join(HERE, "run.sh")],
                "EnvironmentVariables": {"PATH": ":".join(path)},
                "RunAtLoad": True,
                "KeepAlive": False,
                "StandardOutPath": LOG_PATH,
                "StandardErrorPath": LOG_PATH,
            }, fh)
    elif os.path.exists(PLIST_DST):
        os.remove(PLIST_DST)


def version_tuple(tag):
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def homebrew_install(path=__file__):
    # realpath: Homebrew runs us through /opt/homebrew/opt/<name> symlinks, which hide /Cellar/
    return "/Cellar/" in os.path.realpath(path)


def updates_enabled(path=__file__):
    """Only installs made by install.sh update themselves: a git checkout is a
    dev copy, and Homebrew installs update with `brew upgrade`."""
    here = os.path.dirname(os.path.abspath(path))
    return not os.path.exists(os.path.join(here, ".git")) and not homebrew_install(path)


def latest_release():
    """Tag of the newest GitHub release if it is newer than this copy, else None."""
    url = "https://api.github.com/repos/%s/releases/latest" % REPO
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            tag = json.load(resp)["tag_name"]
    except Exception as exc:  # offline, rate limited, no release yet
        log("update check: %s" % exc)
        return None
    return tag if version_tuple(tag) > version_tuple(__version__) else None


def install_update():
    """Rerun the installer in update mode (files and deps only). True on success."""
    url = "https://raw.githubusercontent.com/%s/main/install.sh" % REPO
    proc = subprocess.run(["/bin/sh", "-c", 'curl -fsSL "$0" | sh -s -- --update', url],
                          capture_output=True, text=True, timeout=600)
    log("update exit=%d %s" % (proc.returncode, (proc.stdout + proc.stderr)[-500:]))
    return proc.returncode == 0


def settings_dialog(cfg, autostart):
    """Modal settings form. Returns (cfg, autostart) or None on cancel.
    autostart is None for a Homebrew install: `brew services` owns login start."""
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Claude Usage Monitor Settings")
    alert.setInformativeText_("Polls every idle interval; after usage changes, "
                              "polls every active interval until it goes flat again.")
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")
    view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (280, 144)))

    def field(y, label, value):
        text = AppKit.NSTextField.labelWithString_(label)
        text.setFrame_(((0, y), (200, 22)))
        view.addSubview_(text)
        box = AppKit.NSTextField.alloc().initWithFrame_(((210, y), (60, 22)))
        box.setStringValue_(str(value))
        view.addSubview_(box)
        return box

    def checkbox(y, label, on):
        box = AppKit.NSButton.checkboxWithTitle_target_action_(label, None, None)
        box.setFrame_(((0, y), (280, 22)))
        box.setState_(1 if on else 0)
        view.addSubview_(box)
        return box

    cold = field(118, "Idle interval (minutes):", cfg["cold_minutes"])
    warm = field(88, "Active interval (minutes):", cfg["warm_minutes"])
    waiting = checkbox(58, "Notify: session needs you", cfg["notify_waiting"])
    finished = checkbox(32, "Notify: session finished", cfg["notify_finished"])
    if homebrew_install():
        login = None
        hint = AppKit.NSTextField.labelWithString_("Start at login: brew services start usagebar")
        hint.setSelectable_(True)  # so the command can be copied
        hint.setFrame_(((0, 2), (280, 22)))
        view.addSubview_(hint)
    else:
        login = checkbox(2, "Auto start at login", autostart)
    alert.setAccessoryView_(view)
    alert.window().setInitialFirstResponder_(cold)
    if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
        return None
    new = {
        "cold_minutes": minutes(cold.stringValue(), cfg["cold_minutes"]),
        "warm_minutes": minutes(warm.stringValue(), cfg["warm_minutes"]),
        "notify_waiting": waiting.state() == 1,
        "notify_finished": finished.state() == 1,
    }
    return new, None if login is None else login.state() == 1


class Monitor(rumps.App if rumps else object):
    def __init__(self):
        super().__init__("Claude …", quit_button=None)
        self.cfg = load_config()
        self.warm = False
        self.result = None  # (text, error) from the worker thread
        self.dirty = False
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.live = []  # (MenuItem, base title, reset datetime)
        self.session = None
        self.sessions = []  # (session, context label) from the last poll
        self.session_items = []  # their menu items, retitled in place when only text changes
        self.known = None  # pid -> session at the last poll; None until the first one
        self.contexts = {}  # sessionId -> (transcript path, mtime, context label)
        self.quota = None  # quota part of the title
        self.logged_text = None  # last /usage text logged as having no quota lines
        self.status_item = rumps.MenuItem("", callback=noop)
        self.settings_item = rumps.MenuItem("Settings\u2026", callback=self.open_settings)
        self.update_tag = None  # newer release found by _update_checker
        self.update_failed = False
        self.updating = False
        threading.Thread(target=self._worker, daemon=True).start()
        if updates_enabled():
            threading.Thread(target=self._update_checker, daemon=True).start()
        rumps.Timer(self._tick, 1).start()
        rumps.Timer(self._poll_sessions, 2).start()

    # background --------------------------------------------------------
    def interval(self):
        return self.cfg["warm_minutes" if self.warm else "cold_minutes"]

    def _worker(self):
        last = None
        while True:
            text, error = fetch()
            save_state(text, error)
            key = usage_key(text)
            # ponytail: one flat poll drops back to cold; add a cooldown count if it flaps
            self.warm = bool(last and key and key != last)
            last = key or last
            with self.lock:
                self.result = (text, error)
                self.dirty = True
            self.wake.wait(self.interval() * 60)
            self.wake.clear()

    def _update_checker(self):
        while True:
            tag = latest_release()
            if tag:
                with self.lock:
                    self.update_tag, self.dirty = tag, True
            threading.Event().wait(24 * 3600)

    def _run_update(self):
        if install_update():
            os.execv("/bin/sh", ["sh", os.path.join(HERE, "run.sh")])  # restart into the new code
        with self.lock:
            self.update_failed, self.updating, self.dirty = True, False, True

    # main thread -------------------------------------------------------
    def _tick(self, _):
        with self.lock:
            result, dirty = self.result, self.dirty
            self.dirty = False
            failed, self.update_failed = self.update_failed, False
        if failed:
            rumps.alert("Update failed", "Details are in %s" % LOG_PATH)
        if dirty:
            self._rebuild(*(result or (None, None)))
        for item, base, reset in self.live:
            item.title = "%s  ·  %s" % (base, humanize(reset))
        self._refresh_tooltip()

    def _poll_sessions(self, _):
        """Every 2 s: re-read the session files; rebuild the menu only when a row changed."""
        sessions = read_sessions(previous=self.known)
        # first poll: sessions already waiting at app start do not notify
        events = session_events(self.known, sessions) if self.known is not None else []
        self.known = {s["pid"]: s for s in sessions}  # before notifying: a failure must not repeat it
        for kind, session in events:
            if self.cfg[kind]:
                state = session_state(session)[1]
                notify("Claude Code", session_name(session),
                       "Needs you: %s" % state if kind == "notify_waiting" else "Finished")
        rows = [(s, self._context(s)) for s in sessions]
        live = {str(s.get("sessionId")) for s in sessions}
        self.contexts = {sid: v for sid, v in self.contexts.items() if sid in live}
        titles = [session_row(s, ctx) for s, ctx in rows]
        if titles == [session_row(s, ctx) for s, ctx in self.sessions]:
            return
        same_pids = [s["pid"] for s, _ in rows] == [s["pid"] for s, _ in self.sessions]
        self.sessions = rows
        if same_pids and len(self.session_items) == len(rows):
            # same rows, new text: retitle in place so an open menu does not flicker
            for item, title in zip(self.session_items, titles):
                item.title = title
            self.title = build_title(self.quota, sessions)
        else:
            with self.lock:
                self.dirty = True

    def _context(self, session):
        """Context label, recomputed only when the transcript's mtime changed."""
        sid = str(session.get("sessionId") or "")
        if not re.fullmatch(r"[\w-]+", sid):  # keeps the glob inside PROJECTS
            return ""
        path, mtime, label = self.contexts.get(sid, (None, None, ""))
        try:
            path = path or transcript_path(sid)
            if path and os.path.getmtime(path) != mtime:
                # nothing found in the tail (long tool output): keep the last value
                mtime, label = os.path.getmtime(path), context_label(path) or label
        except OSError as exc:  # transcript moved or deleted
            log("context %s: %s" % (sid, exc))
            path = None
        self.contexts[sid] = (path, mtime, label)
        return label

    def _focus(self, pid):
        threading.Thread(target=focus_session, args=(pid,), daemon=True).start()

    def _rebuild(self, text, error):
        self.live = []
        items = []
        seen = set()

        def add(title, callback=noop):
            while title in seen:  # rumps keys menu items by title
                title += " "
            seen.add(title)
            item = rumps.MenuItem(title, callback=callback)
            items.append(item)
            return item

        if self.sessions:
            add("Sessions")
            self.session_items = [add(session_row(session, context), lambda _, pid=session["pid"]: self._focus(pid))
                                  for session, context in self.sessions]
            items.append(None)
        else:
            self.session_items = []
        stats = parse(text) if text else {}
        self.session = stats.get("Current session")
        self.quota = quota_title(text, error)
        self.title = build_title(self.quota, [s for s, _ in self.sessions])
        if text and not self.session and text != self.logged_text:  # once per new text, not per rebuild
            self.logged_text = text
            log("no session quota line in %r" % text[:300])
        if error:
            for line in error.splitlines():
                add(line)
        elif no_quota_plan(text):  # e.g. an enterprise plan: nothing to show, not an error
            pass
        elif text:
            for line in menu_lines(text):
                title = line.rstrip()
                if not title:
                    items.append(None)  # blank line -> separator
                    continue
                item = add(title)
                match = LINE_RE.match(line)
                if match and stats[match.group(1)][1]:
                    self.live.append((item, item.title, stats[match.group(1)][1]))
        if items and items[-1] is None:
            items.pop()
        self._refresh_status()
        items += ([None] if items else []) + [self.status_item]
        if self.updating:
            items.append(rumps.MenuItem("Updating\u2026"))  # no callback: greyed out, cannot start twice
        elif self.update_tag:
            items.append(rumps.MenuItem("Update to %s" % self.update_tag, callback=self.update_now))
        items += [
            rumps.MenuItem("Refresh now", callback=self.refresh_now),
            self.settings_item,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        self.menu.clear()
        self.menu.update(items)

    def _refresh_status(self):
        self.status_item.title = "Updating every %d min (%s)" % (
            self.interval(), "active" if self.warm else "idle")

    def _refresh_tooltip(self):
        session = getattr(self, "session", None)
        if not session:
            tip = "Claude usage unavailable"
        elif session[1]:
            tip = "%d%% used · resets %s" % (session[0], humanize(session[1]))
        else:
            tip = "%d%% used" % session[0]
        try:  # rumps internal, not public API
            self._nsapp.nsstatusitem.button().setToolTip_(tip)
        except Exception:
            pass

    # callbacks ---------------------------------------------------------
    def refresh_now(self, _):
        self.wake.set()

    def update_now(self, _):
        with self.lock:
            self.updating, self.dirty = True, True
        threading.Thread(target=self._run_update, daemon=True).start()

    def open_settings(self, _):
        # accessory apps have no dock icon; without this the window opens behind
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        result = settings_dialog(self.cfg, autostart_enabled())
        if result is None:
            return
        self.cfg, autostart = result
        save_config(self.cfg)
        try:
            if autostart is not None:  # None: Homebrew install, brew services owns it
                set_autostart(autostart)
        except OSError as exc:
            log("autostart: %s" % exc)
            rumps.alert("Could not change auto start", str(exc))
        self._refresh_status()
        self.wake.set()  # refetch now and restart the wait with the new interval


SAMPLE = """You are currently using your subscription to power your Claude Code usage

Current session: 48% used · resets Sep 17 at 1:40pm (America/Los_Angeles)
Current week (all models): 10% used · resets Sep 22 at 4pm (America/Los_Angeles)

Last 24h · 160 requests · 3 sessions
  65% of your usage was at >150k context
"""

# enterprise plan (owner's work machine, 2026-10-15): no quota lines at all
ENTERPRISE = """You are currently using your subscription to power your Claude Code usage

What's contributing to your usage
Last 24h · 212 requests · 4 sessions
  58% of your usage was at >150k context
"""


def test():
    stats = parse(SAMPLE)
    session_pct, session_reset = stats["Current session"]
    week_pct, week_reset = stats["Current week (all models)"]
    assert session_pct == 48, session_pct
    assert week_pct == 10, week_pct
    assert (session_reset.month, session_reset.day, session_reset.hour, session_reset.minute) == (9, 17, 13, 40), session_reset
    assert (week_reset.month, week_reset.day, week_reset.hour) == (9, 22, 16), week_reset
    assert str(session_reset.tzinfo) == "America/Los_Angeles"

    assert bar(48) == FILLED * 2 + EMPTY * 3 + " 48%", bar(48)
    assert bar(0) == EMPTY * 5 + " 0%"
    assert bar(100) == FILLED * 5 + " 100%"
    assert bar(1) == EMPTY * 5 + " 1%"

    # change detection ignores reset-time jitter, sees percentage moves
    a = usage_key("Current session: 5% used · resets Sep 29 at 3:59pm (America/Los_Angeles)")
    b = usage_key("Current session: 5% used · resets Sep 29 at 4pm (America/Los_Angeles)")
    c = usage_key("Current session: 6% used · resets Sep 29 at 4pm (America/Los_Angeles)")
    assert a == b and b != c, (a, b, c)
    assert usage_key(None) is None

    assert minutes(" 7 ", 20) == 7
    assert minutes("0", 20) == 20 and minutes("x", 20) == 20 and minutes("1441", 5) == 5

    menu = menu_lines(SAMPLE)
    assert menu[-1].startswith("Current week (all models)") and not any("Last 24h" in l for l in menu), menu
    assert menu_lines("odd output\nmore") == ["odd output", "more"]

    # any "Current week (<model>)" line is a quota
    fable = parse("Current week (Fable): 0% used\nCurrent week (all models): 15% used")
    assert fable == {"Current week (Fable)": (0, None), "Current week (all models)": (15, None)}, fable

    assert version_tuple("v0.10.0") > version_tuple("0.9.1") and version_tuple("v1.2.3") == (1, 2, 3)

    # fresh window: 0% line has no reset clause
    fresh = parse("Current session: 0% used\nCurrent week (all models): 87% used · resets Sep 29 at 4pm (America/Los_Angeles)")
    assert fresh["Current session"] == (0, None), fresh
    assert fresh["Current week (all models)"][0] == 87

    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 9, 17, 11, 48, tzinfo=tz)
    assert humanize(session_reset.replace(year=2026), now) == "in 1h 52m"
    assert humanize(now + timedelta(minutes=7), now) == "in 7m"
    assert humanize(now - timedelta(minutes=1), now) == "now"
    assert humanize(now + timedelta(days=5, hours=4, minutes=48), now) == "in 5d 4h"
    assert humanize(None) == "?"

    # year rollover: a December reset parsed while it is already January
    jan = datetime.now(tz).replace(month=1, day=2)
    dec = parse_reset("Dec 31 at 4pm", "America/Los_Angeles")
    assert dec is not None and dec.month == 12
    assert dec > jan - timedelta(days=370)

    assert parse_reset("not a date", "America/Los_Angeles") is None
    assert parse_reset("Sep 17 at 1:40pm", "Not/AZone") is None
    assert parse("nothing here") == {}

    # title: quota bar, session counts, neither
    sessions = [{"pid": 1, "status": "waiting"}, {"pid": 2, "status": "busy"}, {"pid": 3, "status": "idle"}]
    assert build_title(quota_title(SAMPLE, None), sessions) == bar(48) + " 3 ⚠1"
    assert build_title(quota_title(SAMPLE, None), []) == bar(48)
    assert build_title(None, sessions[1:]) == "◉2"
    assert build_title(None, []) == "◉"
    assert build_title(quota_title(None, "claude exited 1"), []) == "⚠︎ Claude"
    # enterprise /usage: no quota lines is not an error
    assert parse(ENTERPRISE) == {} and quota_title(ENTERPRISE, None) is None
    assert build_title(quota_title(ENTERPRISE, None), sessions) == "◉3 ⚠1"
    assert "⚠︎" not in build_title(quota_title(ENTERPRISE, None), [])
    assert no_quota_plan(ENTERPRISE) and not no_quota_plan(SAMPLE) and not no_quota_plan(None)
    # other output without quota lines (expired login, new format) still warns and shows whole
    expired = "Invalid API key · Please run /login"
    assert not no_quota_plan(expired) and quota_title(expired, None) == "⚠︎ Claude"
    assert menu_lines(expired) == [expired]
    assert quota_title(None, None) is None  # before the first fetch
    # Max-like output whose quota line changed format: same header, so it must still warn
    changed = "You are currently using your subscription to power your Claude Code usage\n\nSession usage: 48% used (resets 1:40pm)\n"
    assert not no_quota_plan(changed) and quota_title(changed, None) == "\u26a0\ufe0e Claude"

    # session states: waitingFor text, unknown status shown raw
    assert session_state({"status": "waiting", "waitingFor": "permission prompt"}) == ("⚠", "permission prompt")
    assert session_state({"status": "waiting"}) == ("⚠", "waiting")
    assert session_state({"status": "blocked"}) == ("·", "blocked")
    assert session_row({"pid": 9, "cwd": "/a/proj", "status": "idle"}, "7% context") == "○ proj   7% context   idle"
    assert session_row({"pid": 9, "name": "x-1", "status": "busy"}, "") == "● x-1   busy"

    # notifications only on changes seen after the first poll
    before = {1: {"status": "busy"}, 2: {"status": "busy", "statusUpdatedAt": 1000},
              3: {"status": "waiting"}, 5: {"status": "busy", "statusUpdatedAt": 1000}}
    now = [{"pid": 1, "status": "waiting"}, {"pid": 2, "status": "idle", "statusUpdatedAt": 9000},
           {"pid": 3, "status": "waiting"}, {"pid": 4, "status": "waiting"},
           {"pid": 5, "status": "idle", "statusUpdatedAt": 1700}]  # 0.7 s busy blip (/context): no "Finished"
    assert [(k, s["pid"]) for k, s in session_events(before, now)] == [("notify_waiting", 1), ("notify_finished", 2)]
    assert session_events({}, now) == []

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        def write(name, data):
            with open(os.path.join(tmp, name), "w") as fh:
                fh.write(data if isinstance(data, str) else json.dumps(data))
        write("1.json", {"pid": 1, "status": "idle", "statusUpdatedAt": 5})
        write("2.json", {"pid": 2, "status": "blocked", "statusUpdatedAt": 9})
        write("3.json", {"pid": 3, "status": "busy"})  # crashed: pid dead
        write("4.json", '{"pid": 4, "sta')  # mid-write
        write("4.key", "x")
        got = read_sessions(tmp, alive=lambda pid: pid != 3)
        assert [s["pid"] for s in got] == [2, 1], got
        write("5.json", {"pid": 2 ** 22 + 12345, "status": "idle"})  # real check: no such pid
        write("6.json", {"pid": os.getpid(), "status": "idle"})
        assert sorted(s["pid"] for s in read_sessions(tmp) if s["pid"] > 4) == [os.getpid()]
        # a file caught mid-write keeps the last good copy, so the pid does not come back as "new"
        for name in ("5.json", "6.json"):
            os.remove(os.path.join(tmp, name))
        write("7.json", {"pid": "7", "kind": "interactive", "status": "busy"})
        write("8.json", {"pid": 8, "kind": "sdk-cli", "status": "busy"})  # not an interactive session
        first = read_sessions(tmp, alive=lambda pid: True)
        assert sorted(s["pid"] for s in first) == [1, 2, 3, 7], first  # 7: pid stored back as int; 8: skipped
        write("7.json", '{"pid": 7, "kind": "interac')
        again = read_sessions(tmp, alive=lambda pid: True, previous={s["pid"]: s for s in first})
        assert [s for s in again if s["pid"] == 7] == [{"pid": 7, "kind": "interactive", "status": "busy"}], again
        assert 7 not in [s["pid"] for s in read_sessions(tmp, alive=lambda pid: pid != 7, previous={7: first[0]})]
        os.remove(os.path.join(tmp, "7.json"))  # file gone: dropped even with a previous copy
        assert 7 not in [s["pid"] for s in read_sessions(tmp, alive=lambda pid: True, previous={s["pid"]: s for s in first})]

        # context from the transcript tail; a reply far back is still found
        reply = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
            "input_tokens": 3, "cache_read_input_tokens": 69_997}}}
        side = {"type": "assistant", "isSidechain": True, "message": {"model": "x", "usage": {"input_tokens": 9}}}
        write("t.jsonl", json.dumps(reply) + "\n" + ('{"type": "user", "pad": "%s"}\n' % ("x" * 1000)) * 2000
              + json.dumps(side) + '\n{"type": "assistant", "trunc')
        assert context_label(os.path.join(tmp, "t.jsonl")) == "7% context"
        reply["message"]["model"] = "unknown"
        write("u.jsonl", json.dumps(reply))
        assert context_label(os.path.join(tmp, "u.jsonl")) == "70k context"
        write("e.jsonl", "")
        assert context_label(os.path.join(tmp, "e.jsonl")) == ""
        # the tail read stops at TAIL_MAX: a reply further back is not found (the app keeps the last value)
        write("far.jsonl", json.dumps(reply) + "\n" + ('{"type": "user", "pad": "%s"}\n' % ("x" * 1000)) * 5000)
        assert context_label(os.path.join(tmp, "far.jsonl")) == ""

    # notification toggles are kept as booleans next to the intervals
    global CONFIG_PATH
    saved_path = CONFIG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        CONFIG_PATH = os.path.join(tmp, "c.json")
        assert load_config() == DEFAULTS
        changed = dict(DEFAULTS, notify_waiting=False, notify_finished=True, cold_minutes=9)
        save_config(changed)
        assert load_config() == changed
        save_config(dict(DEFAULTS, notify_waiting="no", notify_finished=1))  # not bools: defaults
        assert load_config() == DEFAULTS
    CONFIG_PATH = saved_path

    # install kind: Homebrew runs us through an opt symlink into the Cellar
    with tempfile.TemporaryDirectory() as tmp:
        libexec = os.path.join(tmp, "Cellar", "usagebar", "0.0.0", "libexec")
        os.makedirs(libexec)
        open(os.path.join(libexec, "monitor.py"), "w").close()
        os.makedirs(os.path.join(tmp, "opt"))
        os.symlink(libexec, os.path.join(tmp, "opt", "usagebar"))
        via_opt = os.path.join(tmp, "opt", "usagebar", "monitor.py")
        assert "/Cellar/" not in os.path.abspath(via_opt)
        assert homebrew_install(via_opt) and not updates_enabled(via_opt)
        plain = os.path.join(tmp, "plain", "monitor.py")
        os.makedirs(os.path.dirname(plain))
        open(plain, "w").close()
        assert not homebrew_install(plain) and updates_enabled(plain)
        os.makedirs(os.path.join(tmp, "plain", ".git"))
        assert not updates_enabled(plain)
    print("ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
    elif "--autostart" in sys.argv:  # used by install.sh
        set_autostart(True)
    elif rumps is None:
        sys.exit("rumps is not installed: python3 -m venv .venv && .venv/bin/pip install rumps")
    else:
        # menu bar only: no Dock icon (must be set before NSApplication starts)
        AppKit.NSBundle.mainBundle().infoDictionary()["LSUIElement"] = "1"
        Monitor().run()
