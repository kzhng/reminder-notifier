"""Reminder schedule: parsing, validation and recurrence rules.

Pure Python (no network, no Discord), so every rule here can be tested on its
own. Everything about *when* a reminder happens lives in this file.

A schedule file (reminders.toml) looks like this:

    timezone = "Australia/Melbourne"
    default_notice_minutes = 15

    [[reminders]]
    name = "Hard Lotus"
    type = "weekly"
    day = "Thursday"
    time = "20:00"

Every reminder needs a `name`, a `type` and a `time`. The type decides which
other fields are allowed (see FIELD_TYPES and the README).
"""

import calendar
import datetime as dt
import difflib
import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TYPES = ("once", "daily", "weekly", "monthly", "yearly")
TOP_LEVEL_KEYS = (
    "timezone",
    "default_notice_minutes",
    "check_interval_seconds",
    "missed_notice_minutes",
    "heartbeat_minutes",
    "reminders",
)

# Fields every reminder may have, whatever its type.
COMMON_FIELDS = ("name", "type", "time", "notice_minutes", "skip", "note", "enabled")

# Type-specific fields: field -> the types that use it. This drives the
# "'date' doesn't belong on a 'weekly' reminder" style of error.
FIELD_TYPES = {
    "date": ("once",),
    "every_days": ("daily",),
    "every_weeks": ("weekly",),
    "every_months": ("monthly",),
    "starts": ("daily", "weekly", "monthly"),
    "day": ("weekly", "monthly"),
    "days": ("weekly",),
    "on": ("monthly", "yearly"),
    "nth": ("monthly",),
    "month": ("yearly",),
    "if_weekend": ("monthly", "yearly"),
}

DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
ON_WORDS = ("last", "last_weekday", "first_weekday")
NTH_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}

MAX_NOTICE_MINUTES = 30 * 24 * 60  # 30 days
DEFAULT_NOTICES = (15,)
DEFAULT_INTERVAL = 60
MIN_INTERVAL = 5
DEFAULT_MISSED_MINUTES = 60  # tell me about runs I missed while offline, up to this long after the start
DEFAULT_HEARTBEAT_MINUTES = 5


@dataclass(frozen=True)
class Problem:
  """Something wrong (or suspicious) in the schedule file."""

  level: str  # "error": the reminder/config is skipped.  "warning": loaded anyway.
  where: str  # e.g. "reminder 'Hard Lotus'"
  text: str

  def __str__(self):
    return f"{self.where} {self.text}"


@dataclass(frozen=True)
class Reminder:
  name: str
  type: str
  time: dt.time
  notices: Tuple[int, ...]  # minutes before, largest first; 0 = at the start time
  skip: Tuple[Tuple[dt.date, dt.date], ...] = ()  # inclusive date ranges
  note: str = ""
  enabled: bool = True
  starts: Optional[dt.date] = None  # earliest date, and the anchor for every_N
  date: Optional[dt.date] = None  # once
  every: int = 1  # every_days / every_weeks / every_months
  weekdays: Tuple[int, ...] = ()  # weekly: the days; monthly "nth": the one day (0=Mon)
  on: object = None  # monthly/yearly: day number, or "last" / "last_weekday" / "first_weekday"
  nth: Optional[int] = None  # monthly: 1-4, or -1 for last
  month: Optional[int] = None  # yearly: 1-12
  if_weekend: str = "none"  # "none", "before" (to Friday) or "after" (to Monday)


@dataclass
class Schedule:
  tz: Optional[ZoneInfo]  # None = the machine's own timezone
  tz_name: str
  default_notices: Tuple[int, ...]
  check_interval: int  # seconds between checks, after any automatic lowering
  requested_interval: int
  reminders: List[Reminder]
  missed_notice_minutes: int = DEFAULT_MISSED_MINUTES  # 0 = off
  heartbeat_minutes: int = DEFAULT_HEARTBEAT_MINUTES

  def active(self):
    return [r for r in self.reminders if r.enabled]

  def max_notice_minutes(self):
    return max((n for r in self.active() for n in r.notices), default=0)


# ------------------------------------------------------------------
# Field parsers. Each returns a clean value or raises FieldError with a
# message that is safe to show the user as-is.
# ------------------------------------------------------------------
class FieldError(ValueError):
  pass


def as_int(value, name, low, high):
  if isinstance(value, bool) or not isinstance(value, int):
    raise FieldError(f"'{name}' must be a whole number, not {value!r}.")
  if not low <= value <= high:
    raise FieldError(f"'{name}' must be between {low} and {high}, not {value}.")
  return value


def as_bool(value):
  if not isinstance(value, bool):
    raise FieldError(f"'enabled' must be true or false, not {value!r}.")
  return value


def as_time(value):
  if isinstance(value, dt.time):  # TOML's own time type
    return value.replace(second=0, microsecond=0, tzinfo=None)
  if isinstance(value, str):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
      return dt.time(int(m.group(1)), int(m.group(2)))
    lowered = value.lower()
    if "am" in lowered or "pm" in lowered:
      hint = ' Use 24-hour time, like "20:00" instead of 8pm.'
    else:
      hint = ' Write it like "20:00".'
    raise FieldError(f"'time' isn't a valid time: {value!r}.{hint}")
  raise FieldError('\'time\' must be text like "20:00".')


def as_date(value, name):
  if isinstance(value, dt.datetime):
    raise FieldError(f"'{name}' must be a date only (YYYY-MM-DD), without a time.")
  if isinstance(value, dt.date):  # TOML's own date type
    return value
  if isinstance(value, str):
    try:
      return dt.datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
      hint = " Use hyphens, like \"2026-12-25\"." if "/" in value else ' Write it like "2026-12-25".'
      raise FieldError(f"'{name}' isn't a valid date: {value!r}.{hint}")
  raise FieldError(f"'{name}' must be a date like \"2026-12-25\".")


def as_weekday(value, name):
  if isinstance(value, str):
    lowered = value.strip().lower()
    for index, full in enumerate(DAY_NAMES):
      if lowered == full or (len(lowered) == 3 and full.startswith(lowered)):
        return index
  raise FieldError(f"'{name}' has an unknown day: {value!r}. Use a day name like \"Thursday\" or \"Thu\".")


def as_days(value):
  if isinstance(value, str) and value.strip().lower() in ("weekdays", "weekends"):
    return (0, 1, 2, 3, 4) if value.strip().lower() == "weekdays" else (5, 6)
  if isinstance(value, list) and value:
    return tuple(sorted({as_weekday(v, "days") for v in value}))
  raise FieldError("'days' must be a list like [\"Monday\", \"Thursday\"], or \"weekdays\" / \"weekends\".")


def as_month(value):
  if isinstance(value, str):
    lowered = value.strip().lower()
    for index, full in enumerate(MONTH_NAMES):
      if lowered == full or (len(lowered) == 3 and full.startswith(lowered)):
        return index + 1
    raise FieldError(f"'month' has an unknown month: {value!r}. Use 1-12 or a name like \"December\".")
  return as_int(value, "month", 1, 12)


def as_notices(value, name):
  items = value if isinstance(value, list) else [value]
  if not items:
    raise FieldError(f"'{name}' can't be an empty list.")
  cleaned = {as_int(v, name, 0, MAX_NOTICE_MINUTES) for v in items}
  return tuple(sorted(cleaned, reverse=True))


def as_skip(value):
  if not isinstance(value, list):
    raise FieldError('\'skip\' must be a list, like ["2026-12-25", "2026-12-24..2026-12-31"].')
  ranges = []
  for item in value:
    if isinstance(item, str) and ".." in item:
      first, _, last = item.partition("..")
      start, end = as_date(first.strip(), "skip"), as_date(last.strip(), "skip")
      if end < start:
        raise FieldError(f"'skip' range {item!r} ends before it starts.")
    else:
      start = end = as_date(item, "skip")
    ranges.append((start, end))
  return tuple(ranges)


def as_note(value):
  if not isinstance(value, str):
    raise FieldError("'note' must be text.")
  if len(value) > 500:
    raise FieldError(f"'note' is too long ({len(value)} characters; the limit is 500).")
  return value.strip()


def as_on(value):
  if isinstance(value, str):
    word = value.strip().lower()
    if word in ON_WORDS:
      return word
    raise FieldError(
        f"'on' has an unknown value {value!r}. Use a day number (1-31), \"last\","
        ' "last_weekday" or "first_weekday".'
    )
  return as_int(value, "on", 1, 31)


def as_nth(value):
  if isinstance(value, bool) or value not in NTH_WORDS:
    raise FieldError("'nth' must be 1, 2, 3, 4 (first to fourth) or -1 (last).")
  return value


def as_if_weekend(value):
  if value in ("before", "after"):
    return value
  raise FieldError('\'if_weekend\' must be "before" (move to Friday) or "after" (move to Monday).')


# ------------------------------------------------------------------
# Parsing one reminder / the whole file
# ------------------------------------------------------------------
def parse_reminder(raw, index, default_notices, today):
  """Validate one [[reminders]] table.

  Returns (Reminder or None, [Problem]). Every problem found is reported, not
  just the first. The Reminder is None if there was at least one error.
  """
  problems: List[Problem] = []
  label = f"reminder #{index}"
  name = raw.get("name")
  if isinstance(name, str) and name.strip():
    name = name.strip()
    label = f"reminder '{name}'"
  else:
    problems.append(Problem("error", label, "needs a 'name' (a piece of text)."))
    name = None

  def err(text):
    problems.append(Problem("error", label, text))

  def warn(text):
    problems.append(Problem("warning", label, text))

  if name is not None and len(name) > 100:
    err(f"has a name that's too long ({len(name)} characters; the limit is 100).")

  rtype = raw.get("type")
  if rtype is None:
    err(f"is missing 'type'. Use one of: {', '.join(TYPES)}.")
    return None, problems
  if rtype not in TYPES:
    err(f"has an unknown type {rtype!r}. Use one of: {', '.join(TYPES)}.")
    return None, problems

  # Fields that don't belong, or that don't exist at all (usually typos).
  known = sorted(set(COMMON_FIELDS) | set(FIELD_TYPES))
  for key in raw:
    if key in COMMON_FIELDS:
      continue
    if key in FIELD_TYPES:
      if rtype not in FIELD_TYPES[key]:
        owners = " and ".join(f"'{t}'" for t in FIELD_TYPES[key])
        err(f"'{key}' doesn't belong on a '{rtype}' reminder (only {owners} use it).")
    else:
      close = difflib.get_close_matches(str(key), known, n=1)
      hint = f" Did you mean '{close[0]}'?" if close else ""
      err(f"has an unknown field '{key}'.{hint}")

  def get(key, parse, required=False, default=None):
    if key not in raw:
      if required:
        err(f"is missing '{key}'.")
      return default
    try:
      return parse(raw[key])
    except FieldError as e:
      err(str(e))
      return default

  time_value = get("time", as_time, required=True)
  notices = get("notice_minutes", lambda v: as_notices(v, "notice_minutes"), default=default_notices)
  skip = get("skip", as_skip, default=())
  note = get("note", as_note, default="")
  enabled = get("enabled", as_bool, default=True)

  fields = dict(
      name=name, type=rtype, time=time_value, notices=notices, skip=skip, note=note, enabled=enabled
  )
  starts = None
  if "starts" in raw and rtype in FIELD_TYPES["starts"]:
    starts = get("starts", lambda v: as_date(v, "starts"))
    fields["starts"] = starts

  def needs_starts(every, field_name):
    if every > 1 and starts is None and "starts" not in raw:
      err(f"needs 'starts' when {field_name} is more than 1 (it says which one counts as the first).")

  if rtype == "once":
    date = get("date", lambda v: as_date(v, "date"), required=True)
    if date is not None and date < today:
      warn(f"has a date in the past ({date}), so it will never run.")
    fields["date"] = date

  elif rtype == "daily":
    every = get("every_days", lambda v: as_int(v, "every_days", 1, 365), default=1)
    needs_starts(every, "every_days")
    fields["every"] = every

  elif rtype == "weekly":
    has_day, has_days = "day" in raw, "days" in raw
    weekdays = ()
    if has_day == has_days:
      err("needs exactly one of 'day' (one day) or 'days' (a list, or \"weekdays\" / \"weekends\").")
    elif has_day:
      wd = get("day", lambda v: as_weekday(v, "day"))
      weekdays = (wd,) if wd is not None else ()
    else:
      weekdays = get("days", as_days, default=())
    every = get("every_weeks", lambda v: as_int(v, "every_weeks", 1, 52), default=1)
    needs_starts(every, "every_weeks")
    if every > 1 and starts is not None and weekdays and starts.weekday() not in weekdays:
      warn(
          f"'starts' ({starts}) is a {DAY_NAMES[starts.weekday()].title()}, which isn't one of its"
          " days. Weeks are counted from the week that date is in."
      )
    fields.update(every=every, weekdays=weekdays)

  elif rtype == "monthly":
    has_on, has_nth, has_day = "on" in raw, "nth" in raw, "day" in raw
    on = nth = None
    weekdays = ()
    if has_on == has_nth:
      err(
          "needs exactly one of 'on' (a day number, \"last\", \"last_weekday\" or"
          " \"first_weekday\") or 'nth' with 'day' (like the last Friday)."
      )
    elif has_on:
      on = get("on", as_on)
      if has_day:
        err("has 'day' but no 'nth'. 'day' only works together with 'nth'; use 'on' for a day number.")
    else:
      nth = get("nth", as_nth)
      if not has_day:
        err("has 'nth' but no 'day'. Add the weekday, like day = \"Friday\".")
      else:
        wd = get("day", lambda v: as_weekday(v, "day"))
        weekdays = (wd,) if wd is not None else ()
    every = get("every_months", lambda v: as_int(v, "every_months", 1, 60), default=1)
    needs_starts(every, "every_months")
    if_weekend = get("if_weekend", as_if_weekend, default="none")
    if "if_weekend" in raw and (nth is not None or isinstance(on, str)):
      err(
          "has 'if_weekend', which only applies when 'on' is a day number."
          " (\"last_weekday\" and \"first_weekday\" already avoid weekends.)"
      )
    fields.update(every=every, weekdays=weekdays, on=on, nth=nth, if_weekend=if_weekend)

  elif rtype == "yearly":
    month = get("month", as_month, required=True)
    on = get("on", lambda v: as_int(v, "on", 1, 31), required=True)
    if month is not None and on is not None:
      most = calendar.monthrange(2024, month)[1]  # 2024 is a leap year
      if on > most:
        err(f"has day {on}, but {MONTH_NAMES[month - 1].title()} never has more than {most} days.")
    fields.update(month=month, on=on, if_weekend=get("if_weekend", as_if_weekend, default="none"))

  if any(p.level == "error" for p in problems):
    return None, problems
  return Reminder(**fields), problems


def _interval_limit(reminder):
  """Longest check interval (seconds) that still lands two checks in the
  shortest gap between this reminder's notices."""
  points = list(reminder.notices)
  if points[-1] != 0:
    points.append(0)
  gaps = [a - b for a, b in zip(points, points[1:])]
  return min(gaps) * 30 if gaps else None


def parse_schedule(raw, today=None):
  """Validate the whole file. Returns (Schedule or None, [Problem]).

  The Schedule is None only for file-level errors (bad timezone, 'reminders'
  not a list). A reminder with an error is skipped; the rest still load.
  """
  today = today or dt.date.today()
  problems: List[Problem] = []
  where = "reminders.toml:"

  for key in raw:
    if key not in TOP_LEVEL_KEYS:
      close = difflib.get_close_matches(str(key), TOP_LEVEL_KEYS, n=1)
      hint = f" Did you mean '{close[0]}'?" if close else ""
      if key in ("bosses", "boss", "events", "reminder"):
        hint = " Did you mean 'reminders'? Each entry should start with [[reminders]]."
      problems.append(Problem("warning", where, f"unknown setting '{key}' is ignored.{hint}"))

  tz_name = raw.get("timezone", "")
  tz = None
  if isinstance(tz_name, str) and tz_name.strip():
    tz_name = tz_name.strip()
    try:
      tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
      problems.append(Problem(
          "error", where,
          f"unknown timezone '{tz_name}'. Use a name like \"Australia/Melbourne\" (on Windows"
          " this also needs: pip install tzdata). This version of the file is ignored.",
      ))
      return None, problems
  else:
    tz_name = "system"
    problems.append(Problem(
        "warning", where,
        'no \'timezone\' set, so the machine\'s own timezone is used. Add timezone = "Australia/Melbourne"'
        " (or yours) so it behaves the same everywhere.",
    ))

  default_notices = DEFAULT_NOTICES
  if "default_notice_minutes" in raw:
    try:
      default_notices = as_notices(raw["default_notice_minutes"], "default_notice_minutes")
    except FieldError as e:
      problems.append(Problem("warning", where, f"{e} Using 15 minutes instead."))

  requested = DEFAULT_INTERVAL
  if "check_interval_seconds" in raw:
    try:
      requested = as_int(raw["check_interval_seconds"], "check_interval_seconds", MIN_INTERVAL, 3600)
    except FieldError as e:
      problems.append(Problem("warning", where, f"{e} Using {DEFAULT_INTERVAL} seconds instead."))

  missed = DEFAULT_MISSED_MINUTES
  if "missed_notice_minutes" in raw:
    try:
      missed = as_int(raw["missed_notice_minutes"], "missed_notice_minutes", 0, 1440)
    except FieldError as e:
      problems.append(Problem("warning", where, f"{e} Using {DEFAULT_MISSED_MINUTES} minutes instead."))

  heartbeat = DEFAULT_HEARTBEAT_MINUTES
  if "heartbeat_minutes" in raw:
    try:
      heartbeat = as_int(raw["heartbeat_minutes"], "heartbeat_minutes", 1, 60)
    except FieldError as e:
      problems.append(Problem("warning", where, f"{e} Using {DEFAULT_HEARTBEAT_MINUTES} minutes instead."))

  items = raw.get("reminders", [])
  if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
    problems.append(Problem(
        "error", where,
        "'reminders' must be a list of tables. Start each entry with [[reminders]]."
        " This version of the file is ignored.",
    ))
    return None, problems

  reminders, seen = [], set()
  for index, item in enumerate(items, start=1):
    reminder, found = parse_reminder(item, index, default_notices, today)
    problems.extend(found)
    if reminder is None:
      continue
    if reminder.name in seen:
      problems.append(Problem(
          "error", f"reminder '{reminder.name}'",
          "is used more than once. Names must be unique, so this later one is skipped.",
      ))
      continue
    seen.add(reminder.name)
    reminders.append(reminder)
  if not items:
    problems.append(Problem("warning", where, "has no [[reminders]] yet."))

  # Make sure at least two checks fit inside the shortest gap between notices.
  interval = requested
  for r in reminders:
    if r.enabled:
      limit = _interval_limit(r)
      if limit is not None:
        interval = min(interval, max(MIN_INTERVAL, limit))

  return Schedule(
      tz, tz_name, default_notices, interval, requested, reminders, missed, heartbeat
  ), problems


# ------------------------------------------------------------------
# Recurrence: does a reminder happen on a given calendar date?
# ------------------------------------------------------------------
def _monday(d):
  return d - dt.timedelta(days=d.weekday())


def _add_months(year, month, delta):
  index = year * 12 + (month - 1) + delta
  return index // 12, index % 12 + 1


def _shift_weekend(d, mode):
  if mode == "before" and d.weekday() >= 5:
    return d - dt.timedelta(days=d.weekday() - 4)  # Sat -> Fri, Sun -> Fri
  if mode == "after" and d.weekday() >= 5:
    return d + dt.timedelta(days=7 - d.weekday())  # Sat -> Mon, Sun -> Mon
  return d


def _nth_weekday(year, month, weekday, nth):
  last = calendar.monthrange(year, month)[1]
  if nth > 0:
    first_offset = (weekday - dt.date(year, month, 1).weekday()) % 7
    return dt.date(year, month, 1 + first_offset + 7 * (nth - 1))
  back = (dt.date(year, month, last).weekday() - weekday) % 7
  return dt.date(year, month, last - back)


def _monthly_date(r, year, month):
  """The date this monthly reminder falls on in a given month (after any weekend shift)."""
  last = calendar.monthrange(year, month)[1]
  if r.nth is not None:
    return _nth_weekday(year, month, r.weekdays[0], r.nth)
  if r.on == "last":
    return dt.date(year, month, last)
  if r.on == "last_weekday":
    d = dt.date(year, month, last)
    while d.weekday() >= 5:
      d -= dt.timedelta(days=1)
    return d
  if r.on == "first_weekday":
    d = dt.date(year, month, 1)
    while d.weekday() >= 5:
      d += dt.timedelta(days=1)
    return d
  # A day number. Short months use their last day (e.g. the 31st -> the 30th).
  return _shift_weekend(dt.date(year, month, min(r.on, last)), r.if_weekend)


def _yearly_date(r, year):
  last = calendar.monthrange(year, r.month)[1]  # Feb 29 becomes Feb 28 in common years
  return _shift_weekend(dt.date(year, r.month, min(r.on, last)), r.if_weekend)


def _is_skipped(r, d):
  return any(start <= d <= end for start, end in r.skip)


def occurs_on(r, d):
  """True if reminder r has a run on calendar date d (in its own timezone)."""
  if not r.enabled or _is_skipped(r, d):
    return False
  if r.starts is not None and d < r.starts:
    return False

  if r.type == "once":
    return d == r.date

  if r.type == "daily":
    return r.every == 1 or (d - r.starts).days % r.every == 0

  if r.type == "weekly":
    if d.weekday() not in r.weekdays:
      return False
    if r.every == 1:
      return True
    return ((_monday(d) - _monday(r.starts)).days // 7) % r.every == 0

  if r.type == "monthly":
    # A weekend shift can move a run into the next or previous month, so look
    # at the neighbouring months as well.
    for delta in (-1, 0, 1):
      year, month = _add_months(d.year, d.month, delta)
      if r.every > 1:
        months_apart = (year - r.starts.year) * 12 + (month - r.starts.month)
        if months_apart % r.every != 0:
          continue
      if _monthly_date(r, year, month) == d:
        return True
    return False

  if r.type == "yearly":
    return any(_yearly_date(r, y) == d for y in (d.year - 1, d.year, d.year + 1))

  return False


def start_datetime(r, d, tzinfo):
  """The aware datetime a run on date d starts at."""
  return dt.datetime.combine(d, r.time, tzinfo=tzinfo)


def occurrences_around(r, tzinfo, local_now, back_days, ahead_days):
  """Run start times (aware, in tzinfo) from back_days ago to ahead_days ahead."""
  today = local_now.date()
  for offset in range(-back_days, ahead_days + 1):
    d = today + dt.timedelta(days=offset)
    if occurs_on(r, d):
      yield start_datetime(r, d, tzinfo)


def upcoming(r, tzinfo, now_utc, days):
  """Runs starting after now_utc, within the next 'days' days, soonest first."""
  local_now = now_utc.astimezone(tzinfo)
  found = []
  for start in occurrences_around(r, tzinfo, local_now, 1, days + 1):
    # Compare in UTC: aware datetimes sharing one tzinfo compare by wall-clock,
    # which is wrong across a daylight saving change.
    if start.astimezone(dt.timezone.utc) > now_utc:
      found.append(start)
  return sorted(found, key=lambda s: s.astimezone(dt.timezone.utc))


# ------------------------------------------------------------------
# Plain-English descriptions (used by --check and --agenda)
# ------------------------------------------------------------------
def _ordinal(n):
  if 10 <= n % 100 <= 20:
    suffix = "th"
  else:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
  return f"{n}{suffix}"


def _day_list(weekdays):
  if weekdays == (0, 1, 2, 3, 4):
    return "weekdays"
  if weekdays == (5, 6):
    return "weekends"
  return ", ".join(DAY_NAMES[i].title() for i in weekdays)


def describe(r):
  at = f"{r.time:%H:%M}"
  begin = f", starting {r.starts}" if r.starts else ""
  shift = ""
  if r.if_weekend == "before":
    shift = " (the Friday before if it falls on a weekend)"
  elif r.if_weekend == "after":
    shift = " (the Monday after if it falls on a weekend)"

  if r.type == "once":
    return f"once, {r.date:%a %Y-%m-%d} at {at}"
  if r.type == "daily":
    base = "every day" if r.every == 1 else f"every {r.every} days"
    return f"{base} at {at}{begin}"
  if r.type == "weekly":
    days = _day_list(r.weekdays)
    base = days if r.every == 1 else f"every {r.every} weeks on {days}"
    return f"{base} at {at}{begin}"
  if r.type == "monthly":
    cycle = "every month" if r.every == 1 else f"every {r.every} months"
    if r.nth is not None:
      what = f"the {NTH_WORDS[r.nth]} {DAY_NAMES[r.weekdays[0]].title()}"
    elif r.on == "last":
      what = "the last day"
    elif r.on == "last_weekday":
      what = "the last weekday"
    elif r.on == "first_weekday":
      what = "the first weekday"
    else:
      what = f"the {_ordinal(r.on)}"
    return f"{what} of {cycle} at {at}{shift}{begin}"
  if r.type == "yearly":
    return f"every year on {_ordinal(r.on)} {MONTH_NAMES[r.month - 1].title()} at {at}{shift}"
  return r.type


def format_minutes(minutes):
  """0 -> 'at start', 10 -> '10m', 120 -> '2h', 1440 -> '1d'."""
  if minutes == 0:
    return "at start"
  if minutes % 1440 == 0:
    return f"{minutes // 1440}d"
  if minutes % 60 == 0:
    return f"{minutes // 60}h"
  return f"{minutes}m"


def describe_notices(notices):
  if notices == (0,):
    return "at the start time"
  return ", ".join(format_minutes(n) for n in notices) + " before"
