"""Tests for the scheduling and validation rules.  Run:  python -m unittest discover tests"""

import datetime as dt
import os
import sys
import tomllib
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reminder_schedule as rs  # noqa: E402

TODAY = dt.date(2026, 10, 29)  # a Thursday


def load(text):
  """Parse TOML text into (Schedule, problems)."""
  return rs.parse_schedule(tomllib.loads(text), today=TODAY)


def one(body, header='timezone = "Australia/Melbourne"\n'):
  """Parse a single reminder from its fields. Returns (Reminder or None, problems)."""
  schedule, problems = load(f"{header}\n[[reminders]]\nname = \"R\"\n{body}")
  assert schedule is not None, problems
  return (schedule.reminders[0] if schedule.reminders else None), problems


def errors(problems):
  return [str(p) for p in problems if p.level == "error"]


def warnings(problems):
  return [str(p) for p in problems if p.level == "warning"]


def runs(r, *dates):
  return [rs.occurs_on(r, dt.date(*d)) for d in dates]


class RecurrenceTests(unittest.TestCase):

  def test_once(self):
    r, _ = one('type = "once"\ndate = "2026-12-25"\ntime = "19:30"')
    self.assertEqual(runs(r, (2026, 12, 25), (2026, 12, 24), (2027, 12, 25)), [True, False, False])

  def test_daily_and_every_n_days(self):
    r, _ = one('type = "daily"\ntime = "09:00"')
    self.assertTrue(all(runs(r, (2026, 11, 1), (2026, 11, 2), (2030, 1, 1))))
    r, _ = one('type = "daily"\nevery_days = 3\nstarts = "2026-11-01"\ntime = "09:00"')
    self.assertEqual(runs(r, (2026, 11, 1), (2026, 11, 2), (2026, 11, 4), (2026, 10, 29)),
                     [True, False, True, False])

  def test_weekly_single_multiple_and_shortcuts(self):
    r, _ = one('type = "weekly"\nday = "Thu"\ntime = "20:00"')
    self.assertEqual(runs(r, (2026, 10, 29), (2026, 10, 30)), [True, False])
    r, _ = one('type = "weekly"\ndays = ["Monday", "thursday"]\ntime = "20:00"')
    self.assertEqual(runs(r, (2026, 10, 26), (2026, 10, 29), (2026, 10, 27)), [True, True, False])
    r, _ = one('type = "weekly"\ndays = "weekdays"\ntime = "09:00"')
    self.assertEqual(runs(r, (2026, 10, 30), (2026, 10, 31), (2026, 11, 1)), [True, False, False])
    r, _ = one('type = "weekly"\ndays = "weekends"\ntime = "09:00"')
    self.assertEqual(runs(r, (2026, 10, 30), (2026, 10, 31), (2026, 11, 1)), [False, True, True])

  def test_every_two_weeks(self):
    r, _ = one('type = "weekly"\nday = "Thursday"\nevery_weeks = 2\nstarts = "2026-11-05"\ntime = "20:00"')
    self.assertEqual(
        runs(r, (2026, 10, 29), (2026, 11, 5), (2026, 11, 12), (2026, 11, 19), (2026, 12, 3)),
        [False, True, False, True, True],
    )

  def test_every_four_weeks_with_two_days(self):
    r, _ = one('type = "weekly"\ndays = ["Mon", "Thu"]\nevery_weeks = 4\nstarts = "2026-11-02"\ntime = "20:00"')
    self.assertEqual(
        runs(r, (2026, 11, 2), (2026, 11, 5), (2026, 11, 9), (2026, 11, 30), (2026, 12, 3)),
        [True, True, False, True, True],
    )

  def test_monthly_day_number_and_short_months(self):
    r, _ = one('type = "monthly"\non = 31\ntime = "09:00"')
    self.assertEqual(runs(r, (2026, 10, 31), (2026, 11, 30), (2026, 11, 29), (2027, 2, 28)),
                     [True, True, False, True])

  def test_if_weekend(self):
    # 15 Nov 2026 is a Sunday; 15 Aug 2026 is a Saturday.
    r, _ = one('type = "monthly"\non = 15\nif_weekend = "before"\ntime = "09:00"')
    self.assertEqual(runs(r, (2026, 11, 13), (2026, 11, 15), (2026, 8, 14), (2026, 10, 15)),
                     [True, False, True, True])
    r, _ = one('type = "monthly"\non = 15\nif_weekend = "after"\ntime = "09:00"')
    self.assertEqual(runs(r, (2026, 11, 16), (2026, 11, 15), (2026, 8, 17)), [True, False, True])

  def test_weekend_shift_can_cross_a_month_boundary(self):
    # 1 Aug 2026 is a Saturday, so "before" moves it to Friday 31 July.
    r, _ = one('type = "monthly"\non = 1\nif_weekend = "before"\ntime = "09:00"')
    self.assertEqual(runs(r, (2026, 7, 31), (2026, 8, 1)), [True, False])

  def test_last_and_weekday_words(self):
    r, _ = one('type = "monthly"\non = "last"\ntime = "09:00"')
    self.assertEqual(runs(r, (2026, 10, 31), (2027, 2, 28), (2028, 2, 29), (2028, 2, 28)),
                     [True, True, True, False])
    r, _ = one('type = "monthly"\non = "last_weekday"\ntime = "09:00"')
    # 31 Oct 2026 is a Saturday, 31 Jan 2027 a Sunday
    self.assertEqual(runs(r, (2026, 10, 30), (2026, 10, 31), (2027, 1, 29), (2026, 11, 30)),
                     [True, False, True, True])
    r, _ = one('type = "monthly"\non = "first_weekday"\ntime = "09:00"')
    # 1 Nov 2026 is a Sunday
    self.assertEqual(runs(r, (2026, 11, 2), (2026, 11, 1), (2026, 12, 1)), [True, False, True])

  def test_nth_weekday(self):
    r, _ = one('type = "monthly"\nnth = -1\nday = "Friday"\ntime = "20:00"')
    self.assertEqual(runs(r, (2026, 10, 30), (2026, 10, 23), (2026, 11, 27)), [True, False, True])
    r, _ = one('type = "monthly"\nnth = 2\nday = "Thursday"\ntime = "20:00"')
    self.assertEqual(runs(r, (2026, 11, 12), (2026, 11, 5), (2026, 11, 19)), [True, False, False])

  def test_every_three_months(self):
    r, _ = one('type = "monthly"\non = 1\nevery_months = 3\nstarts = "2027-01-01"\ntime = "10:00"')
    self.assertEqual(runs(r, (2027, 1, 1), (2027, 2, 1), (2027, 4, 1), (2027, 10, 1), (2026, 10, 1)),
                     [True, False, True, True, False])

  def test_yearly(self):
    r, _ = one('type = "yearly"\nmonth = "March"\non = 14\ntime = "09:00"')
    self.assertEqual(runs(r, (2027, 3, 14), (2028, 3, 14), (2027, 3, 15)), [True, True, False])
    r, _ = one('type = "yearly"\nmonth = 2\non = 29\ntime = "09:00"')
    self.assertEqual(runs(r, (2028, 2, 29), (2027, 2, 28), (2027, 3, 1)), [True, True, False])
    # 25 Dec 2027 is a Saturday
    r, _ = one('type = "yearly"\nmonth = 12\non = 25\nif_weekend = "before"\ntime = "09:00"')
    self.assertEqual(runs(r, (2027, 12, 24), (2027, 12, 25)), [True, False])

  def test_skip_dates_and_ranges(self):
    r, _ = one('type = "daily"\ntime = "09:00"\nskip = ["2026-12-25", "2026-12-28..2026-12-31"]')
    self.assertEqual(runs(r, (2026, 12, 24), (2026, 12, 25), (2026, 12, 29), (2026, 12, 31), (2027, 1, 1)),
                     [True, False, False, False, True])

  def test_starts_is_an_earliest_date(self):
    r, _ = one('type = "weekly"\nday = "Thursday"\nstarts = "2026-11-12"\ntime = "20:00"')
    self.assertEqual(runs(r, (2026, 11, 5), (2026, 11, 12)), [False, True])

  def test_disabled_reminder_never_runs(self):
    r, _ = one('type = "daily"\ntime = "09:00"\nenabled = false')
    self.assertFalse(rs.occurs_on(r, dt.date(2026, 11, 1)))

  def test_upcoming_is_sorted_and_future_only(self):
    r, _ = one('type = "weekly"\nday = "Thursday"\ntime = "20:00"')
    tz = r and rs.ZoneInfo("Australia/Melbourne")
    now = dt.datetime(2026, 10, 29, 20, 30, tzinfo=tz).astimezone(dt.timezone.utc)  # just after the run
    found = rs.upcoming(r, tz, now, 20)
    self.assertEqual([s.date() for s in found], [dt.date(2026, 11, 5), dt.date(2026, 11, 12), dt.date(2026, 11, 19)])


class ValidationTests(unittest.TestCase):

  def assertError(self, body, fragment, header='timezone = "Australia/Melbourne"\n'):
    r, problems = one(body, header)
    self.assertIsNone(r, f"expected an error for: {body}")
    self.assertTrue(any(fragment in e for e in errors(problems)), (fragment, errors(problems)))

  def test_missing_and_unknown_type(self):
    self.assertError('time = "20:00"', "missing 'type'")
    self.assertError('type = "weekley"\ntime = "20:00"', "unknown type")

  def test_fields_that_dont_belong_or_dont_exist(self):
    self.assertError('type = "weekly"\nday = "Thursday"\ndate = "2026-12-25"\ntime = "20:00"', "'date' doesn't belong on a 'weekly'")
    self.assertError('type = "once"\ndate = "2026-12-25"\ntime = "20:00"\nnotice_minute = 5', "Did you mean 'notice_minutes'")

  def test_bad_times(self):
    self.assertError('type = "daily"\ntime = "8pm"', "24-hour")
    self.assertError('type = "daily"\ntime = "25:00"', "isn't a valid time")
    self.assertError('type = "daily"', "missing 'time'")

  def test_bad_dates(self):
    self.assertError('type = "once"\ndate = "2026/12/25"\ntime = "20:00"', "Use hyphens")
    self.assertError('type = "once"\ndate = "25-12-2026"\ntime = "20:00"', "isn't a valid date")
    self.assertError('type = "once"\ntime = "20:00"', "missing 'date'")

  def test_weekly_needs_exactly_one_of_day_or_days(self):
    self.assertError('type = "weekly"\ntime = "20:00"', "exactly one of 'day'")
    self.assertError('type = "weekly"\nday = "Mon"\ndays = ["Tue"]\ntime = "20:00"', "exactly one of 'day'")
    self.assertError('type = "weekly"\nday = "Funday"\ntime = "20:00"', "unknown day")

  def test_every_n_needs_starts(self):
    self.assertError('type = "weekly"\nday = "Mon"\nevery_weeks = 2\ntime = "20:00"', "needs 'starts'")
    self.assertError('type = "daily"\nevery_days = 5\ntime = "20:00"', "needs 'starts'")
    self.assertError('type = "monthly"\non = 1\nevery_months = 3\ntime = "20:00"', "needs 'starts'")

  def test_starts_on_wrong_weekday_is_a_warning_not_an_error(self):
    r, problems = one('type = "weekly"\nday = "Monday"\nevery_weeks = 2\nstarts = "2026-11-05"\ntime = "20:00"')
    self.assertIsNotNone(r)
    self.assertTrue(any("isn't one of its days" in w for w in warnings(problems)))

  def test_monthly_rules(self):
    self.assertError('type = "monthly"\ntime = "20:00"', "exactly one of 'on'")
    self.assertError('type = "monthly"\non = 5\nnth = 1\nday = "Monday"\ntime = "20:00"', "exactly one of 'on'")
    self.assertError('type = "monthly"\non = 5\nday = "Monday"\ntime = "20:00"', "only works together with 'nth'")
    self.assertError('type = "monthly"\nnth = 1\ntime = "20:00"', "'nth' but no 'day'")
    self.assertError('type = "monthly"\nnth = 5\nday = "Monday"\ntime = "20:00"', "'nth' must be")
    self.assertError('type = "monthly"\non = 32\ntime = "20:00"', "between 1 and 31")
    self.assertError('type = "monthly"\non = "sometime"\ntime = "20:00"', "unknown value")
    self.assertError('type = "monthly"\non = "last"\nif_weekend = "before"\ntime = "20:00"', "only applies when 'on' is a day number")
    self.assertError('type = "monthly"\nnth = 1\nday = "Monday"\nif_weekend = "before"\ntime = "20:00"', "only applies when 'on' is a day number")
    self.assertError('type = "monthly"\non = 5\nif_weekend = "sideways"\ntime = "20:00"', "'if_weekend' must be")

  def test_yearly_rules(self):
    self.assertError('type = "yearly"\non = 5\ntime = "20:00"', "missing 'month'")
    self.assertError('type = "yearly"\nmonth = "March"\ntime = "20:00"', "missing 'on'")
    self.assertError('type = "yearly"\nmonth = 2\non = 30\ntime = "20:00"', "never has more than 29")
    self.assertError('type = "yearly"\nmonth = "Smarch"\non = 5\ntime = "20:00"', "unknown month")

  def test_notice_minutes(self):
    r, _ = one('type = "daily"\ntime = "09:00"\nnotice_minutes = [10, 60, 10]')
    self.assertEqual(r.notices, (60, 10))
    r, _ = one('type = "daily"\ntime = "09:00"\nnotice_minutes = 0')
    self.assertEqual(r.notices, (0,))
    self.assertError('type = "daily"\ntime = "09:00"\nnotice_minutes = -5', "between 0")
    self.assertError('type = "daily"\ntime = "09:00"\nnotice_minutes = true', "whole number")
    self.assertError('type = "daily"\ntime = "09:00"\nnotice_minutes = []', "empty")
    self.assertError('type = "daily"\ntime = "09:00"\nnotice_minutes = 999999', "between 0")

  def test_skip_and_note_and_enabled(self):
    self.assertError('type = "daily"\ntime = "09:00"\nskip = "2026-12-25"', "must be a list")
    self.assertError('type = "daily"\ntime = "09:00"\nskip = ["2026-12-31..2026-12-24"]', "ends before it starts")
    self.assertError('type = "daily"\ntime = "09:00"\nnote = 5', "must be text")
    self.assertError('type = "daily"\ntime = "09:00"\nnote = "' + "x" * 501 + '"', "too long")
    self.assertError('type = "daily"\ntime = "09:00"\nenabled = "yes"', "true or false")

  def test_all_problems_in_one_entry_are_reported(self):
    r, problems = one('type = "weekly"\ntime = "8pm"\nevery_weeks = 2')
    self.assertIsNone(r)
    self.assertGreaterEqual(len(errors(problems)), 3)  # time, day/days, starts

  def test_once_in_the_past_is_a_warning(self):
    r, problems = one('type = "once"\ndate = "2026-01-05"\ntime = "20:00"')
    self.assertIsNotNone(r)
    self.assertTrue(any("in the past" in w for w in warnings(problems)))

  def test_native_toml_dates_and_times_are_accepted(self):
    r, problems = one('type = "once"\ndate = 2026-12-25\ntime = 19:30:00\nskip = [2026-12-01]')
    self.assertEqual((r.date, r.time), (dt.date(2026, 12, 25), dt.time(19, 30)))
    self.assertEqual(errors(problems), [])

  def test_bad_reminders_dont_stop_good_ones(self):
    schedule, problems = load(
        'timezone = "UTC"\n'
        '[[reminders]]\nname = "Bad"\ntype = "weekly"\ntime = "20:00"\n'
        '[[reminders]]\nname = "Good"\ntype = "daily"\ntime = "09:00"\n'
    )
    self.assertEqual([r.name for r in schedule.reminders], ["Good"])
    self.assertEqual(len(errors(problems)), 1)

  def test_names_must_be_present_and_unique(self):
    schedule, problems = load(
        'timezone = "UTC"\n'
        '[[reminders]]\nname = "Same"\ntype = "daily"\ntime = "09:00"\n'
        '[[reminders]]\nname = "Same"\ntype = "daily"\ntime = "10:00"\n'
        '[[reminders]]\ntype = "daily"\ntime = "11:00"\n'
    )
    self.assertEqual(len(schedule.reminders), 1)
    self.assertTrue(any("more than once" in e for e in errors(problems)))
    self.assertTrue(any("needs a 'name'" in e for e in errors(problems)))

  def test_file_level_problems(self):
    schedule, problems = load('timezone = "Mars/Olympus"\n')
    self.assertIsNone(schedule)
    self.assertTrue(any("unknown timezone" in e for e in errors(problems)))

    schedule, problems = load('timezone = "UTC"\nreminders = "oops"\n')
    self.assertIsNone(schedule)

    schedule, problems = load('timezone = "UTC"\n[[bosses]]\nname = "Old style"\n')
    self.assertTrue(any("Did you mean 'reminders'" in w for w in warnings(problems)))
    self.assertTrue(any("no [[reminders]]" in w for w in warnings(problems)))

    schedule, problems = load('[[reminders]]\nname = "A"\ntype = "daily"\ntime = "09:00"\n')
    self.assertTrue(any("no 'timezone'" in w for w in warnings(problems)))
    self.assertIsNone(schedule.tz)

    schedule, problems = load('timezone = "UTC"\ndefault_notice_minutes = "soon"\ncheck_interval_seconds = 1\n')
    self.assertEqual((schedule.default_notices, schedule.check_interval), (rs.DEFAULT_NOTICES, rs.DEFAULT_INTERVAL))
    text = " ".join(warnings(problems))
    self.assertIn("default_notice_minutes", text)
    self.assertIn("check_interval_seconds", text)

  def test_missed_and_heartbeat_settings(self):
    schedule, problems = load('timezone = "UTC"\n[[reminders]]\nname = "A"\ntype = "daily"\ntime = "09:00"\n')
    self.assertEqual((schedule.missed_notice_minutes, schedule.heartbeat_minutes), (60, 5))
    schedule, problems = load('timezone = "UTC"\nmissed_notice_minutes = 0\nheartbeat_minutes = 15\n[[reminders]]\nname = "A"\ntype = "daily"\ntime = "09:00"\n')
    self.assertEqual((schedule.missed_notice_minutes, schedule.heartbeat_minutes), (0, 15))
    self.assertEqual(problems, [])
    schedule, problems = load('timezone = "UTC"\nmissed_notice_minutes = 5000\nheartbeat_minutes = 0\n[[reminders]]\nname = "A"\ntype = "daily"\ntime = "09:00"\n')
    self.assertEqual((schedule.missed_notice_minutes, schedule.heartbeat_minutes), (60, 5))
    text = " ".join(warnings(problems))
    self.assertIn("missed_notice_minutes", text)
    self.assertIn("heartbeat_minutes", text)

  def test_default_notice_minutes_can_be_a_list(self):
    schedule, _ = load('timezone = "UTC"\ndefault_notice_minutes = [60, 10]\n[[reminders]]\nname = "A"\ntype = "daily"\ntime = "09:00"\n')
    self.assertEqual(schedule.reminders[0].notices, (60, 10))

  def test_check_interval_is_lowered_for_short_notices(self):
    schedule, _ = load('timezone = "UTC"\ncheck_interval_seconds = 60\n[[reminders]]\nname = "A"\ntype = "daily"\ntime = "09:00"\nnotice_minutes = 1\n')
    self.assertEqual(schedule.check_interval, 30)
    self.assertEqual(schedule.requested_interval, 60)
    schedule, _ = load('timezone = "UTC"\n[[reminders]]\nname = "A"\ntype = "daily"\ntime = "09:00"\nnotice_minutes = [60, 10]\n')
    self.assertEqual(schedule.check_interval, 60)


class DescribeTests(unittest.TestCase):

  def test_descriptions_read_naturally(self):
    cases = [
        ('type = "weekly"\nday = "Thursday"\ntime = "20:00"', "Thursday at 20:00"),
        ('type = "weekly"\ndays = "weekdays"\ntime = "09:30"', "weekdays at 09:30"),
        ('type = "weekly"\nday = "Thursday"\nevery_weeks = 2\nstarts = "2026-11-05"\ntime = "20:00"', "every 2 weeks on Thursday at 20:00, starting 2026-11-05"),
        ('type = "monthly"\non = 15\nif_weekend = "before"\ntime = "09:00"', "the 15th of every month at 09:00 (the Friday before"),
        ('type = "monthly"\nnth = -1\nday = "Friday"\ntime = "20:00"', "the last Friday of every month"),
        ('type = "monthly"\non = "last_weekday"\ntime = "16:00"', "the last weekday of every month"),
        ('type = "yearly"\nmonth = "March"\non = 1\ntime = "09:00"', "every year on 1st March"),
    ]
    for body, expected in cases:
      r, _ = one(body)
      self.assertIn(expected, rs.describe(r))

  def test_notice_descriptions(self):
    self.assertEqual(rs.describe_notices((60, 10)), "1h, 10m before")
    self.assertEqual(rs.describe_notices((1440,)), "1d before")
    self.assertEqual(rs.describe_notices((0,)), "at the start time")


if __name__ == "__main__":
  unittest.main()
