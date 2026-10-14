#!/usr/bin/env python3
"""macOS menu bar monitor for Claude Code usage quota (`claude -p /usage`)."""

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
DEFAULTS = {"cold_minutes": 20, "warm_minutes": 5}
SEGMENTS = 5
FILLED, EMPTY = "▰", "▱"

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
        for key in DEFAULTS:
            cfg[key] = minutes(saved.get(key, cfg[key]), cfg[key])
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh)


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
    view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (280, 92)))

    def field(y, label, value):
        text = AppKit.NSTextField.labelWithString_(label)
        text.setFrame_(((0, y), (200, 22)))
        view.addSubview_(text)
        box = AppKit.NSTextField.alloc().initWithFrame_(((210, y), (60, 22)))
        box.setStringValue_(str(value))
        view.addSubview_(box)
        return box

    cold = field(66, "Idle interval (minutes):", cfg["cold_minutes"])
    warm = field(36, "Active interval (minutes):", cfg["warm_minutes"])
    if homebrew_install():
        login = None
        hint = AppKit.NSTextField.labelWithString_("Start at login: brew services start usagebar")
        hint.setSelectable_(True)  # so the command can be copied
        hint.setFrame_(((0, 2), (280, 22)))
        view.addSubview_(hint)
    else:
        login = AppKit.NSButton.checkboxWithTitle_target_action_("Auto start at login", None, None)
        login.setFrame_(((0, 2), (280, 22)))
        login.setState_(1 if autostart else 0)
        view.addSubview_(login)
    alert.setAccessoryView_(view)
    alert.window().setInitialFirstResponder_(cold)
    if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
        return None
    new = {
        "cold_minutes": minutes(cold.stringValue(), cfg["cold_minutes"]),
        "warm_minutes": minutes(warm.stringValue(), cfg["warm_minutes"]),
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
        self.status_item = rumps.MenuItem("", callback=noop)
        self.settings_item = rumps.MenuItem("Settings\u2026", callback=self.open_settings)
        self.update_tag = None  # newer release found by _update_checker
        self.update_failed = False
        self.updating = False
        threading.Thread(target=self._worker, daemon=True).start()
        if updates_enabled():
            threading.Thread(target=self._update_checker, daemon=True).start()
        rumps.Timer(self._tick, 1).start()

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
        if result is None:
            return
        if dirty:
            self._rebuild(*result)
        for item, base, reset in self.live:
            item.title = "%s  ·  %s" % (base, humanize(reset))
        self._refresh_tooltip()

    def _rebuild(self, text, error):
        self.live = []
        items = []
        if error:
            self.title = "⚠︎ Claude"
            self.session = None
            items = [rumps.MenuItem(line, callback=noop) for line in error.splitlines()]
        else:
            stats = parse(text)
            self.session = stats.get("Current session")
            if not self.session:  # unrecognised output: show it, do not hide it
                log("parse failed on %r" % text[:300])
                self.title = "\u26a0\ufe0e Claude"
            else:
                self.title = bar(self.session[0])
            seen = set()
            for line in menu_lines(text):
                title = line.rstrip()
                if not title:
                    items.append(None)  # blank line -> separator
                    continue
                while title in seen:  # rumps keys menu items by title
                    title += " "
                seen.add(title)
                item = rumps.MenuItem(title, callback=noop)
                match = LINE_RE.match(line)
                if match and stats[match.group(1)][1]:
                    self.live.append((item, title, stats[match.group(1)][1]))
                items.append(item)
        self._refresh_status()
        items += [None, self.status_item]
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

    # install kind: Homebrew runs us through an opt symlink into the Cellar
    import tempfile
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
