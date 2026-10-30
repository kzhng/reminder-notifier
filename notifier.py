"""
Reminder Notifier
=================
Sends a Discord webhook reminder shortly before each scheduled event: one-off
dates, daily, weekly (including every N weeks), monthly (a date, the last day,
the last Friday, ...) and yearly. Works on Linux and Windows.

Files (all next to this script)
-------------------------------
  reminders.toml        your schedule (safe to commit, hot-reloaded when saved)
  reminders.example.toml  every option, with comments
  secrets.json          webhook URL, Discord user ID, heartbeat URL
                        (gitignored, hot-reloaded)
  state.json            which reminders were already sent, and when the script
                        last ran, so a restart never repeats a reminder and
                        downtime can be reported (written by the script, gitignored)
  reminder_schedule.py  the scheduling and validation rules

Secrets can also come from environment variables, which win over secrets.json:
  DISCORD_WEBHOOK_URL, DISCORD_USER_ID, HEARTBEAT_URL

Usage
-----
  python notifier.py                    run the notifier
  python notifier.py --check [--days N] validate everything, show the next run of each reminder
  python notifier.py --agenda [--days N] list every run in the next N days, in date order
  python notifier.py --test             send a test message to Discord (and ping the heartbeat URL)
  python notifier.py --install-startup  (Windows) run automatically at login
                                        (includes an automatic 60s network-ready delay)
  python notifier.py --install-startup --hidden
                                        same, but with no console window
  python notifier.py --startup-status   (Windows) is run-at-login enabled?
  python notifier.py --uninstall-startup
                                        (Windows) remove the run-at-login entry

The startup options register or remove an entry and then exit; they do not
start the notifier. On Linux use the systemd service (reminder-notifier.service).
"""

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import math
import os
import re
import sys
import time
from dataclasses import dataclass

import requests

try:
  import tomllib
except ImportError:  # Python < 3.11
  sys.exit("Python 3.11 or newer is required (it reads the TOML schedule file).")

from reminder_schedule import (
    Problem,
    describe,
    describe_notices,
    occurrences_around,
    parse_schedule,
    upcoming,
)

# -------------------------------------------------------------------
# PATHS - always next to the script, whatever the working directory is.
# (Windows "Run at login" and systemd start with a different directory.)
# -------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "reminders.toml")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "notifier.log")

STATE_KEEP = dt.timedelta(hours=1)  # forget a sent reminder this long after its run started
LAST_CHECK_SAVE = dt.timedelta(seconds=60)  # how often "last ran at" is written to state.json
MIN_GAP_SECONDS = 180  # silence longer than this (and 3 check intervals) counts as downtime
MISSED_NOTICE = -1  # stands in for "notice" in state.json for a "missed while offline" message

# A Discord webhook looks like https://discord.com/api/webhooks/<number>/<token>
# (an optional ?thread_id=... is allowed for posting into a thread)
WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+(?:\?[\w=&.-]*)?$"
)
HEARTBEAT_RE = re.compile(r"^https?://\S+$")

DEFAULT_TEMPLATE = """\
# Your reminder schedule. See reminders.example.toml for every option.
timezone = "Australia/Melbourne"
default_notice_minutes = 15

[[reminders]]
name = "Example weekly reminder"
type = "weekly"
day = "Thursday"
time = "20:00"
"""


def utcnow():
  return dt.datetime.now(dt.timezone.utc)


def setup_logging():
  fmt = logging.Formatter(
      "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
  )
  root = logging.getLogger()
  root.setLevel(logging.INFO)

  file_handler = logging.handlers.RotatingFileHandler(
      LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
  )
  file_handler.setFormatter(fmt)
  root.addHandler(file_handler)

  # pythonw.exe has no stdout; systemd/journald and terminals do.
  if sys.stdout is not None:
    try:
      sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # old Windows consoles
    except (AttributeError, ValueError):
      pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

  def log_uncaught(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
      sys.__excepthook__(exc_type, exc, tb)
      return
    logging.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

  sys.excepthook = log_uncaught


# -------------------------------------------------------------------
# RUN-AT-LOGIN (Windows)
# Registers this script under the current user's "Run" registry key, which
# Windows reads at every login. winreg is imported inside the functions
# because it only exists on Windows (a top-level import would crash the
# script on Linux).
# -------------------------------------------------------------------
# Think of the registry as a big dictionary of folders and labelled values:
#   STARTUP_REG_PATH : the "folder" Windows reads at login for programs to start
#                      (a location inside the registry, NOT a folder on disk)
#   STARTUP_APP_NAME : the label of our entry in that folder (just a name; it is
#                      NOT the script or exe file name)
#   the value stored under that label is the command to run, which
#   build_startup_command() builds from the real Python and script paths.
STARTUP_APP_NAME = "ReminderNotifier"
STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _require_windows():
  if sys.platform != "win32":
    sys.exit(
        "Run-at-login registration only works on Windows. "
        "On Linux, use the systemd service (reminder-notifier.service)."
    )


def build_startup_command(hidden=False):
  """The exact command line Windows will run at login."""
  exe = sys.executable
  if hidden:
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
      exe = pythonw  # same interpreter, but no console window
    else:
      print("Note: pythonw.exe not found next to python.exe; using a visible console.")
  return f'"{exe}" "{os.path.abspath(__file__)}" --startup'


def install_startup(hidden=False):
  _require_windows()
  import winreg

  command = build_startup_command(hidden)
  with winreg.OpenKey(
      winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE
  ) as key:
    winreg.SetValueEx(key, STARTUP_APP_NAME, 0, winreg.REG_SZ, command)
  print(f"Installed. Will run at login:\n  {command}")


def uninstall_startup():
  _require_windows()
  import winreg

  with winreg.OpenKey(
      winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE
  ) as key:
    try:
      winreg.DeleteValue(key, STARTUP_APP_NAME)
      print("Removed from startup.")
    except FileNotFoundError:
      print("It wasn't registered for startup.")


def startup_status():
  _require_windows()
  import winreg

  with winreg.OpenKey(
      winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_READ
  ) as key:
    try:
      value, _ = winreg.QueryValueEx(key, STARTUP_APP_NAME)
      print(f"Enabled. Runs at login:\n  {value}")
    except FileNotFoundError:
      print("Not registered for startup.")


# -------------------------------------------------------------------
# File loading
# -------------------------------------------------------------------
def load_toml(path):
  with open(path, "rb") as f:
    # utf-8-sig tolerates the BOM that some Windows editors add
    return tomllib.loads(f.read().decode("utf-8-sig"))


def load_json(path):
  with open(path, "r", encoding="utf-8-sig") as f:
    return json.load(f)


class WatchedFile:
  """A config file that is re-read only when it changes on disk.

  If someone saves a broken edit, the error is logged once and the last good
  version keeps being used, so a typo never silences your reminders.
  """

  def __init__(self, path, label, loader):
    self.path = path
    self.label = label
    self.loader = loader
    self.mtime = None
    self.data = None
    self.changed = False  # True for one refresh() after a new version loads

  def refresh(self):
    self.changed = False
    try:
      mtime = os.path.getmtime(self.path)
    except OSError:
      return self.data  # file missing: keep whatever we had
    if mtime == self.mtime:
      return self.data
    self.mtime = mtime  # remember even on failure -> one error per edit
    try:
      data = self.loader(self.path)
      if not isinstance(data, dict):
        raise ValueError("the top level must be a table/object")
    except (OSError, ValueError) as e:  # TOMLDecodeError and JSONDecodeError are ValueErrors
      kept = "Keeping the previous version." if self.data else "Nothing loaded."
      logging.error(f"Could not read {self.label}: {e}. {kept}")
      return self.data
    self.data = data
    self.changed = True
    logging.info(f"Loaded {self.label}.")
    return self.data


def ensure_default_config():
  if os.path.exists(CONFIG_FILE):
    return
  logging.warning(f"'{CONFIG_FILE}' not found. Creating a starter template...")
  try:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
      f.write(DEFAULT_TEMPLATE)
  except OSError as e:
    logging.error(f"Failed to create the starter reminders.toml: {e}")


# -------------------------------------------------------------------
# Secrets
# -------------------------------------------------------------------
@dataclass(frozen=True)
class Secrets:
  """The checked secrets. A value that failed its check is blank, with a Problem explaining why."""

  webhook: str = ""
  user_id: str = ""
  heartbeat_url: str = ""
  problems: tuple = ()


def resolve_secrets(raw):
  """Read and check the secrets. Environment variables win over secrets.json.

  The problem messages never contain the URLs themselves (they are secrets).
  """

  def pick(env_name, key):
    return str(os.environ.get(env_name) or raw.get(key) or "").strip()

  webhook = pick("DISCORD_WEBHOOK_URL", "webhook_url")
  user_id = pick("DISCORD_USER_ID", "discord_user_id")
  heartbeat = pick("HEARTBEAT_URL", "heartbeat_url")
  problems = []

  if not webhook:
    problems.append(Problem(
        "error", "secrets:",
        "no Discord webhook is set. Put \"webhook_url\" in secrets.json (or set DISCORD_WEBHOOK_URL)."
        " Reminders can't be sent until then.",
    ))
  elif not WEBHOOK_RE.match(webhook):
    problems.append(Problem(
        "error", "secrets:",
        "webhook_url doesn't look like a Discord webhook URL. It should look like"
        " https://discord.com/api/webhooks/<number>/<token>. Reminders can't be sent until it's fixed.",
    ))
    webhook = ""
  if user_id and not user_id.isdigit():
    problems.append(Problem(
        "warning", "secrets:",
        "discord_user_id should be a number (in Discord: Developer Mode, right-click your name, Copy"
        " User ID). Pings are off until it's fixed.",
    ))
    user_id = ""
  if heartbeat and not HEARTBEAT_RE.match(heartbeat):
    problems.append(Problem(
        "warning", "secrets:",
        "heartbeat_url should start with http:// or https://. Heartbeats are off until it's fixed.",
    ))
    heartbeat = ""
  return Secrets(webhook, user_id, heartbeat, tuple(problems))


def log_problems(problems):
  for problem in problems:
    if problem.level == "error":
      logging.error(f"{problem}")
    else:
      logging.warning(f"{problem}")


# -------------------------------------------------------------------
# Saved state: what was already sent, and when the script last ran
# -------------------------------------------------------------------
def load_state(path):
  """Read state.json. Returns (sent, last_check).

  sent is {(name, run start in UTC, notice): run start in UTC}; last_check is
  when the script last ran (aware UTC datetime) or None. Anything unreadable is
  skipped, never fatal: the worst case is that one reminder is sent again.
  """
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
    items = data.get("sent", [])
  except FileNotFoundError:
    return {}, None
  except (OSError, ValueError, AttributeError) as e:
    logging.warning(f"Could not read state.json ({e}); starting with nothing marked as sent.")
    return {}, None

  def parse_time(text):
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:
      when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc)

  sent = {}
  for item in items if isinstance(items, list) else []:
    try:
      start = parse_time(item["start"])
      sent[(str(item["name"]), start, int(item["notice"]))] = start
    except (KeyError, TypeError, ValueError):
      logging.warning(f"Skipping an unreadable entry in state.json: {item!r}")

  last_check = None
  try:
    if data.get("last_check"):
      last_check = parse_time(data["last_check"])
  except (TypeError, ValueError):
    logging.warning("Ignoring an unreadable 'last_check' in state.json.")
  return sent, last_check


def save_state(path, sent, last_check):
  """Write state.json atomically, so a crash can never leave it half-written."""
  data = {
      "version": 2,
      "last_check": last_check.isoformat() if last_check else None,
      "sent": [
          {"name": name, "start": start.isoformat(), "notice": notice}
          for (name, start, notice) in sorted(sent, key=lambda k: (k[1], k[0], k[2]))
      ],
  }
  tmp = path + ".tmp"
  try:
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2)
    os.replace(tmp, path)
  except OSError as e:
    logging.error(f"Could not save state.json: {e}")


# -------------------------------------------------------------------
# Discord and the heartbeat
# -------------------------------------------------------------------
def post_to_discord(webhook_url, content, user_id=None):
  """Send a message. Returns True on success. Never logs the webhook URL."""
  payload = {"content": content}
  if user_id:
    payload["allowed_mentions"] = {"users": [user_id]}
  else:
    payload["allowed_mentions"] = {"parse": []}
  try:
    resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()
    return True
  except requests.exceptions.RequestException as e:
    # str(e) often contains the full webhook URL (which is a secret),
    # so only log the error type and HTTP status.
    status = getattr(getattr(e, "response", None), "status_code", None)
    extra = f" (HTTP {status})" if status else ""
    logging.error(f"Discord webhook failed: {type(e).__name__}{extra}")
    return False


def ping_heartbeat(url):
  """Ping the heartbeat URL. Returns (ok, detail); detail never contains the URL."""
  try:
    resp = requests.get(url, timeout=10)
  except requests.exceptions.RequestException as e:
    return False, type(e).__name__
  if resp.status_code >= 400:
    return False, f"HTTP {resp.status_code}"
  return True, "ok"


def format_remaining(minutes):
  """90 -> '1 hour 30 minutes', 1500 -> '1 day 1 hour'. At most two units."""

  def unit(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"

  days, rest = divmod(minutes, 1440)
  hours, mins = divmod(rest, 60)
  if days:
    return unit(days, "day") + (f" {unit(hours, 'hour')}" if hours else "")
  if hours:
    return unit(hours, "hour") + (f" {unit(mins, 'minute')}" if mins else "")
  return unit(mins, "minute")


def build_message(reminder, when_text, minutes_left, user_id=None):
  mention = f"<@{user_id}> " if user_id else ""
  timing = f"in {format_remaining(minutes_left)}" if minutes_left > 0 else "starting now"
  text = f"{mention}🔔 **Reminder:** **{reminder.name}** is scheduled for **{when_text}** ({timing})."
  if reminder.note:
    text += "\n" + "\n".join(f"> {line}" for line in reminder.note.splitlines())
  return text


def build_missed_message(items, now, local_now, user_id=None):
  """One message for everything missed while offline. items: [(reminder, start_local, start_utc)]."""
  mention = f"<@{user_id}> " if user_id else ""

  def describe_one(reminder, start_local, start):
    ago = format_remaining(max(1, int((now - start).total_seconds() // 60)))
    return f"**{reminder.name}** started at **{when_text(start_local, local_now)}** ({ago} ago)"

  if len(items) == 1:
    reminder, start_local, start = items[0]
    text = f"{mention}⚠️ **Missed reminder:** {describe_one(reminder, start_local, start)}, while the notifier was offline."
    if reminder.note:
      text += "\n" + "\n".join(f"> {line}" for line in reminder.note.splitlines())
    return text

  shown = items[:10]
  lines = [f"• {describe_one(*item)}" for item in shown]
  if len(items) > len(shown):
    lines.append(f"• …and {len(items) - len(shown)} more")
  return f"{mention}⚠️ **Missed {len(items)} reminders** while the notifier was offline:\n" + "\n".join(lines)


def when_text(start_local, now_local):
  """'20:00 AEDT' if it's today, else 'Thu 05 Nov 20:00 AEDT'."""
  if start_local.date() == now_local.date():
    return start_local.strftime("%H:%M %Z").strip()
  return start_local.strftime("%a %d %b %H:%M %Z").strip()


def due_notice(notices, start, now, interval_seconds):
  """Which notice (minutes before) should be sent right now for a run at 'start'?

  If several have passed (say the notifier was down), only the most recent one
  counts, so you never get a stale "in 60 minutes" reminder 5 minutes before.
  A notice of 0 ("at start") stays valid for one check interval after the start.
  """
  best = None
  for n in notices:  # largest first, so the last match is the smallest notice
    trigger = start - dt.timedelta(minutes=n)
    end = start if n > 0 else start + dt.timedelta(seconds=interval_seconds)
    if trigger <= now < end:
      best = n
  return best


# -------------------------------------------------------------------
# Core loop
# -------------------------------------------------------------------
class Notifier:

  def __init__(self):
    self.config_file = WatchedFile(CONFIG_FILE, "reminders.toml", load_toml)
    self.secrets_file = WatchedFile(SECRETS_FILE, "secrets.json", load_json)
    self.schedule = None
    self.ahead_days = 2

    # (name, run start UTC, notice) -> run start UTC; and when we last ran
    self.sent, self.last_check = load_state(STATE_FILE)
    if self.sent:
      logging.info(f"Loaded state.json: {len(self.sent)} reminder(s) already sent.")
    self.last_check_saved = self.last_check
    self.gap_from = None  # start of a detected downtime, until its missed runs are reported

    self.last_secret_problems = None
    self.last_heartbeat = None
    self.heartbeat_failing = False

  def apply_config(self, raw):
    """Validate and adopt a new schedule. Returns False if it was rejected."""
    schedule, problems = parse_schedule(raw)
    log_problems(problems)
    if schedule is None:
      logging.error("Ignoring this version of reminders.toml (see the problems above).")
      return False

    skipped = sum(1 for p in problems if p.level == "error")
    if schedule.check_interval < schedule.requested_interval:
      logging.info(
          f"check_interval_seconds lowered {schedule.requested_interval} ->"
          f" {schedule.check_interval} so short reminders can't be skipped."
      )
    self.schedule = schedule
    # How many days ahead a run can be and still have a reminder due today.
    self.ahead_days = math.ceil(schedule.max_notice_minutes() / 1440) + 1
    logging.info(
        f"Schedule ready: {len(schedule.active())} active reminder(s)"
        f"{f', {skipped} skipped because of errors' if skipped else ''},"
        f" timezone {schedule.tz_name}, checking every {schedule.check_interval}s."
    )
    return True

  def interval(self):
    return self.schedule.check_interval if self.schedule else 30

  def save(self):
    save_state(STATE_FILE, self.sent, self.last_check)

  def report_secret_problems(self, secrets):
    """Log secret problems once, when they appear or change (not on every check)."""
    if secrets.problems == self.last_secret_problems:
      return
    log_problems(secrets.problems)
    if not secrets.problems and self.last_secret_problems:
      logging.info("The secrets look fine now.")
    self.last_secret_problems = secrets.problems

  def is_missed(self, reminder, start, now):
    """Did this run start while the notifier was down, with nothing sent for it?"""
    missed_window = dt.timedelta(minutes=self.schedule.missed_notice_minutes)
    if not dt.timedelta(0) <= now - start <= missed_window:
      return False
    if any(key[0] == reminder.name and key[1] == start for key in self.sent):
      return False  # something was already sent for this run
    # The latest reminder was due during the downtime. (If it was due earlier,
    # the notifier was running and the cause is something else.)
    latest_trigger = start - dt.timedelta(minutes=min(reminder.notices))
    return latest_trigger > self.gap_from

  def send_missed(self, missed, secrets, now, local_now):
    """Tell the channel about runs that started while we were offline. Returns True if sent."""
    missed.sort(key=lambda item: item[2])
    logging.warning("Missed while offline: " + ", ".join(r.name for r, _, _ in missed))
    text = build_missed_message(missed, now, local_now, secrets.user_id)
    if not post_to_discord(secrets.webhook, text, secrets.user_id):
      return False
    for reminder, _, start in missed:
      self.sent[(reminder.name, start, MISSED_NOTICE)] = start
    self.save()
    return True

  def maybe_heartbeat(self, now, secrets):
    """Ping the heartbeat URL, but only while the notifier can actually send reminders."""
    if not secrets.heartbeat_url or not secrets.webhook:
      return
    every = dt.timedelta(minutes=self.schedule.heartbeat_minutes)
    if self.last_heartbeat is not None and now - self.last_heartbeat < every:
      return
    ok, detail = ping_heartbeat(secrets.heartbeat_url)
    if ok:
      if self.heartbeat_failing:
        logging.info("Heartbeat is working again.")
        self.heartbeat_failing = False
      self.last_heartbeat = now
    elif not self.heartbeat_failing:
      logging.warning(f"Heartbeat ping failed ({detail}); will keep trying.")
      self.heartbeat_failing = True

  def tick(self):
    raw = self.config_file.refresh()
    if raw is None:
      return
    secrets_raw = self.secrets_file.refresh() or {}
    if self.config_file.changed:
      self.apply_config(raw)
    if self.schedule is None:
      return

    secrets = resolve_secrets(secrets_raw)
    self.report_secret_problems(secrets)
    now = utcnow()
    local_now = now.astimezone(self.schedule.tz)  # tz None -> the system timezone
    interval = self.schedule.check_interval

    # Forget sent reminders a while after their run started.
    keep = max(STATE_KEEP, dt.timedelta(minutes=self.schedule.missed_notice_minutes))
    kept = {k: v for k, v in self.sent.items() if v > now - keep}
    if len(kept) != len(self.sent):
      self.sent = kept
      self.save()

    # Was the notifier off (or the computer asleep) since the last check?
    if (
        self.gap_from is None
        and self.last_check is not None
        and now - self.last_check > dt.timedelta(seconds=max(3 * interval, MIN_GAP_SECONDS))
    ):
      self.gap_from = self.last_check
      logging.warning(f"No checks were made between {self.last_check:%Y-%m-%d %H:%M} and now (UTC).")
    look_for_missed = (
        self.gap_from is not None and self.schedule.missed_notice_minutes > 0 and bool(secrets.webhook)
    )

    missed = []
    for reminder in self.schedule.active():
      for start_local in occurrences_around(
          reminder, local_now.tzinfo, local_now, 1, self.ahead_days
      ):
        start = start_local.astimezone(dt.timezone.utc)
        notice = due_notice(reminder.notices, start, now, interval)
        if notice is None:
          if look_for_missed and self.is_missed(reminder, start, now):
            missed.append((reminder, start_local, start))
          continue
        key = (reminder.name, start, notice)
        if key in self.sent:
          continue
        if not secrets.webhook:
          continue  # the problem was already logged once

        minutes_left = max(0, math.ceil((start - now).total_seconds() / 60))
        message = build_message(
            reminder, when_text(start_local, local_now), minutes_left, secrets.user_id
        )
        logging.info(f"Reminder due: {reminder.name} at {start_local:%Y-%m-%d %H:%M %Z}")
        # Only mark as sent on success, so a network blip is retried next cycle.
        if post_to_discord(secrets.webhook, message, secrets.user_id):
          self.sent[key] = start
          self.save()  # saved at once, so a crash can't repeat it
          logging.info(f"Sent Discord reminder for {reminder.name}")

    if self.gap_from is not None:
      if not missed or self.send_missed(missed, secrets, now, local_now):
        self.gap_from = None  # reported (or nothing to report); otherwise try again next check

    self.last_check = now
    if self.last_check_saved is None or now - self.last_check_saved >= LAST_CHECK_SAVE:
      self.save()
      self.last_check_saved = now
    self.maybe_heartbeat(now, secrets)

  def run(self):
    while True:
      try:
        self.tick()
      except Exception:  # never let the daemon die
        logging.exception("Unexpected error in check cycle; will retry")
      time.sleep(self.interval())


def main(startup=False):
  if startup:
    logging.info("Launched at Windows login. Waiting 60s for the network...")
    time.sleep(60)
  logging.info(f"Reminder Notifier started (launched via startup: {startup}).")
  ensure_default_config()
  try:
    Notifier().run()
  except KeyboardInterrupt:
    logging.info("Stopped.")


# -------------------------------------------------------------------
# Helper commands
# -------------------------------------------------------------------
def _load_for_report():
  """Load and validate the schedule for --check / --agenda. Returns (schedule, problems) or None."""
  ensure_default_config()
  try:
    raw = load_toml(CONFIG_FILE)
  except (OSError, ValueError) as e:
    print(f"Could not read reminders.toml: {e}")
    return None
  schedule, problems = parse_schedule(raw)
  return schedule, problems


def _print_problems(problems):
  errors = [p for p in problems if p.level == "error"]
  warnings = [p for p in problems if p.level == "warning"]
  if errors:
    print("\nErrors (these reminders are skipped):")
    for p in errors:
      print(f"  ✗ {p}")
  if warnings:
    print("\nWarnings:")
    for p in warnings:
      print(f"  ! {p}")
  return len(errors)


def _read_secrets_for_report():
  """Read secrets.json for --check / --test, without the "Loaded ..." log line in the output."""
  try:
    return resolve_secrets(load_json(SECRETS_FILE))
  except FileNotFoundError:
    return resolve_secrets({})
  except (OSError, ValueError) as e:
    print(f"Could not read secrets.json: {e}")
    return resolve_secrets({})


def run_check(days):
  """Validate everything, then show when each reminder next runs (soonest first)."""
  loaded = _load_for_report()
  if loaded is None:
    return 1
  schedule, problems = loaded
  error_count = _print_problems(problems)
  if schedule is None:
    return 1

  now = utcnow()
  local_now = now.astimezone(schedule.tz)
  print(f"\nNow: {local_now:%a %Y-%m-%d %H:%M %Z}  (UTC {now:%H:%M})   Looking {days} days ahead")

  rows = []
  for r in schedule.reminders:
    if not r.enabled:
      continue
    runs = upcoming(r, local_now.tzinfo, now, days)
    rows.append((r, runs[0] if runs else None))
  # Soonest first; reminders with no run in the window go last.
  rows.sort(key=lambda row: (
      row[1] is None,
      row[1].astimezone(dt.timezone.utc) if row[1] else now,
      row[0].name.lower(),
  ))

  print()
  for r, start in rows:
    info = f"{describe(r)}; reminders {describe_notices(r.notices)}"
    if start is None:
      print(f"- {r.name}: no run in the next {days} days  [{info}]")
    else:
      print(f"- {start:%a %Y-%m-%d %H:%M %Z}  {r.name}  [{info}]")
  for r in schedule.reminders:
    if not r.enabled:
      print(f"- {r.name}: disabled")

  secrets = _read_secrets_for_report()
  print("\nSecrets:")
  print(f"  Webhook:   {'configured' if secrets.webhook else 'NOT USABLE'}")
  print(f"  Ping you:  {'yes (discord_user_id set)' if secrets.user_id else 'no'}")
  print(f"  Heartbeat: {f'every {schedule.heartbeat_minutes} min' if secrets.heartbeat_url else 'off'}")
  for problem in secrets.problems:
    print(f"  {'✗' if problem.level == 'error' else '!'} {problem}")
    error_count += problem.level == "error"
  missed = schedule.missed_notice_minutes
  print(f"  Missed-run notices: {'off' if missed == 0 else f'runs that started up to {missed} min ago'}")

  print(f"\n{len(schedule.reminders)} reminder(s) loaded, {error_count} problem(s) that need fixing.")
  return 1 if error_count else 0


def run_agenda(days):
  """List every run in the next N days, in date order."""
  loaded = _load_for_report()
  if loaded is None:
    return 1
  schedule, problems = loaded
  error_count = _print_problems(problems)
  if schedule is None:
    return 1

  now = utcnow()
  local_now = now.astimezone(schedule.tz)
  entries = []
  for r in schedule.active():
    for start in upcoming(r, local_now.tzinfo, now, days):
      entries.append((start, r))
  entries.sort(key=lambda e: (e[0].astimezone(dt.timezone.utc), e[1].name.lower()))

  print(f"\nNow: {local_now:%a %Y-%m-%d %H:%M %Z}. Every run in the next {days} days:\n")
  if not entries:
    print("  (nothing scheduled)")
  for start, r in entries:
    print(f"{start:%a %Y-%m-%d %H:%M %Z}  {r.name}  (reminders {describe_notices(r.notices)})")
  print(f"\n{len(entries)} run(s).")
  return 1 if error_count else 0


def run_test():
  """Send a test message, and ping the heartbeat URL if there is one."""
  secrets = _read_secrets_for_report()
  for problem in secrets.problems:
    print(f"{'✗' if problem.level == 'error' else '!'} {problem}")
  if not secrets.webhook:
    return 1
  mention = f"<@{secrets.user_id}> " if secrets.user_id else ""
  ok = post_to_discord(secrets.webhook, f"{mention}✅ Reminder Notifier test message.", secrets.user_id)
  print("Discord message:", "sent" if ok else "FAILED - check notifier.log")
  if secrets.heartbeat_url:
    hb_ok, detail = ping_heartbeat(secrets.heartbeat_url)
    print("Heartbeat ping:", "sent" if hb_ok else f"FAILED ({detail})")
    ok = ok and hb_ok
  return 0 if ok else 1


# -------------------------------------------------------------------
# CLI entrypoint
# -------------------------------------------------------------------
def parse_args():
  parser = argparse.ArgumentParser(description="Reminder Notifier")
  group = parser.add_mutually_exclusive_group()
  group.add_argument("--install-startup", action="store_true",
                     help="(Windows) start this script automatically at login, then exit")
  group.add_argument("--uninstall-startup", action="store_true",
                     help="(Windows) remove the run-at-login entry, then exit")
  group.add_argument("--startup-status", action="store_true",
                     help="(Windows) show whether run-at-login is enabled, then exit")
  group.add_argument("--check", action="store_true",
                     help="validate everything and show the next run of each reminder, then exit")
  group.add_argument("--agenda", action="store_true",
                     help="list every run in the next N days in date order, then exit")
  group.add_argument("--test", action="store_true",
                     help="send a test message (and a heartbeat ping), then exit")
  parser.add_argument("--days", type=int, default=None, metavar="N",
                      help="with --check (default 62) or --agenda (default 30): how many days ahead to look")
  parser.add_argument("--startup", action="store_true",
                      help="indicates the script was launched on Windows login")
  parser.add_argument("--hidden", action="store_true",
                      help="with --install-startup: run without a console window (uses pythonw.exe)")
  args = parser.parse_args()
  if args.hidden and not args.install_startup:
    parser.error("--hidden only makes sense together with --install-startup")
  if args.days is not None:
    if not (args.check or args.agenda):
      parser.error("--days only makes sense together with --check or --agenda")
    if not 1 <= args.days <= 732:
      parser.error("--days must be between 1 and 732")
  return args


if __name__ == "__main__":
  args = parse_args()
  setup_logging()
  if args.install_startup:
    install_startup(args.hidden)
  elif args.uninstall_startup:
    uninstall_startup()
  elif args.startup_status:
    startup_status()
  elif args.check:
    sys.exit(run_check(args.days or 62))
  elif args.agenda:
    sys.exit(run_agenda(args.days or 30))
  elif args.test:
    sys.exit(run_test())
  else:
    main(startup=args.startup)
