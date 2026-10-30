"""Tests for the notifier loop (with a fake clock and a fake Discord).

Run:  python -m unittest discover tests
"""

import contextlib
import datetime as dt
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import notifier as nf  # noqa: E402

MEL = ZoneInfo("Australia/Melbourne")
HEADER = 'timezone = "Australia/Melbourne"\n'
WEEKLY = '[[reminders]]\nname = "Hard Lotus"\ntype = "weekly"\nday = "Thursday"\ntime = "20:00"\n'


def at(*args):
  """A Melbourne wall-clock time, as an aware UTC datetime."""
  return dt.datetime(*args, tzinfo=MEL).astimezone(dt.timezone.utc)


class NotifierTestCase(unittest.TestCase):

  def setUp(self):
    logging.disable(logging.CRITICAL)
    self.addCleanup(logging.disable, logging.NOTSET)
    self._mtime = 1000
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.dir = tmp.name

    names = ("CONFIG_FILE", "SECRETS_FILE", "STATE_FILE", "utcnow", "post_to_discord", "ping_heartbeat")
    originals = {name: getattr(nf, name) for name in names}
    self.addCleanup(lambda: [setattr(nf, k, v) for k, v in originals.items()])
    nf.CONFIG_FILE = os.path.join(self.dir, "reminders.toml")
    nf.SECRETS_FILE = os.path.join(self.dir, "secrets.json")
    nf.STATE_FILE = os.path.join(self.dir, "state.json")
    env = mock.patch.dict(os.environ)
    env.start()
    self.addCleanup(env.stop)
    os.environ.pop("DISCORD_WEBHOOK_URL", None)
    os.environ.pop("DISCORD_USER_ID", None)
    os.environ.pop("HEARTBEAT_URL", None)

    self.set_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc", "discord_user_id": "42"})
    self.now = at(2026, 10, 29, 12, 0)  # a Thursday
    nf.utcnow = lambda: self.now
    self.sent = []
    self.post_ok = True

    def fake_post(url, content, user_id=None):
      if not self.post_ok:
        return False
      self.sent.append(content)
      return True

    nf.post_to_discord = fake_post
    self.pings = []
    self.ping_ok = True

    def fake_ping(url):
      self.pings.append(url)
      return (True, "ok") if self.ping_ok else (False, "HTTP 502")

    nf.ping_heartbeat = fake_ping

  # -- helpers --
  def _write(self, path, text):
    with open(path, "w", encoding="utf-8") as f:
      f.write(text)
    self._mtime += 1
    os.utime(path, (self._mtime, self._mtime))  # make sure the file looks changed

  def write(self, body, header=HEADER):
    self._write(nf.CONFIG_FILE, header + body)

  def set_secrets(self, data):
    self._write(nf.SECRETS_FILE, json.dumps(data))

  def ticks(self, notifier, *times):
    """Tick at each time; returns how many messages each tick sent."""
    counts = []
    for when in times:
      self.now = when
      before = len(self.sent)
      notifier.tick()
      counts.append(len(self.sent) - before)
    return counts

  def state_entries(self):
    with open(nf.STATE_FILE, encoding="utf-8") as f:
      return json.load(f)["sent"]

  def seed_state(self, last_check, sent=()):
    """Pretend an earlier run left this state behind. sent: [(name, start, notice)]."""
    nf.save_state(nf.STATE_FILE, {k: k[1] for k in sent}, last_check)


class SendingTests(NotifierTestCase):

  def test_sends_once_inside_the_window(self):
    self.write(WEEKLY)
    n = nf.Notifier()
    counts = self.ticks(n, at(2026, 10, 29, 19, 44, 30), at(2026, 10, 29, 19, 45, 30),
                        at(2026, 10, 29, 19, 46, 30), at(2026, 10, 29, 20, 0, 30))
    self.assertEqual(counts, [0, 1, 0, 0])
    self.assertEqual(
        self.sent[0],
        "<@42> 🔔 **Reminder:** **Hard Lotus** is scheduled for **20:00 AEDT** (in 15 minutes).",
    )

  def test_two_notices_and_a_late_start(self):
    self.write('[[reminders]]\nname = "Two"\ntype = "weekly"\nday = "Thursday"\ntime = "20:00"\nnotice_minutes = [60, 10]\n')
    n = nf.Notifier()
    counts = self.ticks(n, at(2026, 10, 29, 18, 59), at(2026, 10, 29, 19, 0, 10), at(2026, 10, 29, 19, 30),
                        at(2026, 10, 29, 19, 50, 10), at(2026, 10, 29, 19, 51))
    self.assertEqual(counts, [0, 1, 0, 1, 0])
    self.assertIn("in 1 hour)", self.sent[0])
    self.assertIn("in 10 minutes)", self.sent[1])

    self.sent.clear()
    os.remove(nf.STATE_FILE)
    late = nf.Notifier()  # started 5 minutes before: only the most recent notice applies
    self.assertEqual(self.ticks(late, at(2026, 10, 29, 19, 55)), [1])
    self.assertIn("in 5 minutes", self.sent[0])

  def test_notice_zero_says_starting_now(self):
    self.write('[[reminders]]\nname = "Now"\ntype = "weekly"\nday = "Thursday"\ntime = "20:00"\nnotice_minutes = 0\n')
    n = nf.Notifier()
    counts = self.ticks(n, at(2026, 10, 29, 19, 59), at(2026, 10, 29, 20, 0, 20), at(2026, 10, 29, 20, 0, 40))
    self.assertEqual(counts, [0, 1, 0])
    self.assertIn("starting now", self.sent[0])

  def test_a_reminder_can_cross_midnight(self):
    self.write('[[reminders]]\nname = "Late"\ntype = "weekly"\nday = "Friday"\ntime = "00:05"\nnotice_minutes = 30\n')
    n = nf.Notifier()
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 23, 34), at(2026, 10, 29, 23, 36)), [0, 1])
    self.assertIn("Fri 30 Oct 00:05 AEDT", self.sent[0])

  def test_a_day_before_with_a_note(self):
    self.write('[[reminders]]\nname = "Xmas"\ntype = "once"\ndate = "2026-12-25"\ntime = "19:30"\n'
               'notice_minutes = [1440]\nnote = "Bring cake\\nand the link"\n')
    n = nf.Notifier()
    self.assertEqual(self.ticks(n, at(2026, 12, 24, 19, 29), at(2026, 12, 24, 19, 30, 30)), [0, 1])
    self.assertIn("in 1 day", self.sent[0])
    self.assertIn("> Bring cake\n> and the link", self.sent[0])

  def test_skipped_and_disabled_reminders_send_nothing(self):
    self.write(WEEKLY.replace('time = "20:00"', 'time = "20:00"\nskip = ["2026-10-29"]')
               + '[[reminders]]\nname = "Off"\ntype = "weekly"\nday = "Thursday"\ntime = "20:00"\nenabled = false\n')
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 45, 30)), [0])

  def test_monthly_weekend_shift(self):
    self.write('[[reminders]]\nname = "Pay"\ntype = "monthly"\non = 15\nif_weekend = "before"\ntime = "09:00"\n')
    n = nf.Notifier()
    self.assertEqual(self.ticks(n, at(2026, 11, 13, 8, 45, 30), at(2026, 11, 15, 8, 45, 30)), [1, 0])

  def test_failed_send_is_retried_then_not_repeated(self):
    self.write(WEEKLY)
    n = nf.Notifier()
    self.post_ok = False
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 19, 50)), [0])
    self.post_ok = True
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 19, 51), at(2026, 10, 29, 19, 52)), [1, 0])

  def test_no_webhook_is_not_fatal_and_is_picked_up_later(self):
    self.write(WEEKLY)
    self.set_secrets({})
    n = nf.Notifier()
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 19, 50), at(2026, 10, 29, 19, 51)), [0, 0])
    self.set_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc"})
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 19, 52)), [1])
    self.assertFalse(self.sent[0].startswith("<@"))  # no user id, no ping

  def test_no_timezone_uses_the_system_timezone(self):
    self.write(WEEKLY, header="")
    with mock.patch.dict(os.environ, {"TZ": "Australia/Melbourne"}):
      import time
      time.tzset()
      self.addCleanup(time.tzset)
      self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 45, 30)), [1])


class SavedStateTests(NotifierTestCase):

  def test_restart_inside_the_window_does_not_repeat(self):
    self.write(WEEKLY)
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 45, 30)), [1])
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 46, 30)), [0])  # a fresh process

  def test_a_failed_send_is_not_saved_so_a_restart_retries_it(self):
    self.write(WEEKLY)
    self.post_ok = False
    self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 50))
    self.assertEqual(self.state_entries(), [])  # the file records when it last ran, but nothing as sent
    self.post_ok = True
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 51)), [1])

  def test_each_notice_is_remembered_separately(self):
    self.write('[[reminders]]\nname = "Two"\ntype = "weekly"\nday = "Thursday"\ntime = "20:00"\nnotice_minutes = [60, 10]\n')
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 0, 10)), [1])
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 1)), [0])  # restart: 60 not repeated
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 50, 10)), [1])  # 10 still sends

  def test_entries_are_dropped_an_hour_after_the_run(self):
    self.write(WEEKLY)
    n = nf.Notifier()
    self.ticks(n, at(2026, 10, 29, 19, 45, 30))
    self.assertEqual(len(self.state_entries()), 1)
    self.ticks(n, at(2026, 10, 29, 20, 30))  # run was 30 minutes ago: kept
    self.assertEqual(len(self.state_entries()), 1)
    self.ticks(n, at(2026, 10, 29, 21, 5))  # over an hour ago: dropped
    self.assertEqual(self.state_entries(), [])

  def test_entries_for_runs_in_the_future_are_kept(self):
    self.write('[[reminders]]\nname = "Xmas"\ntype = "once"\ndate = "2026-12-25"\ntime = "19:30"\nnotice_minutes = [1440]\n')
    self.ticks(nf.Notifier(), at(2026, 12, 24, 19, 30, 30))
    later = nf.Notifier()  # restarted the next morning, hours before the run
    self.ticks(later, at(2026, 12, 25, 9, 0))
    self.assertEqual(len(later.sent), 1)

  def test_unreadable_state_is_ignored(self):
    self._write(nf.STATE_FILE, "{not json")
    self.assertEqual(nf.Notifier().sent, {})
    self._write(nf.STATE_FILE, json.dumps({"sent": "nope"}))
    self.assertEqual(nf.Notifier().sent, {})

  def test_bad_entries_are_skipped_but_good_ones_load(self):
    good = {"name": "A", "start": "2026-10-29T09:00:00+00:00", "notice": 15}
    self._write(nf.STATE_FILE, json.dumps({"sent": [good, {"name": "B"}, "junk", {**good, "start": "not a date"}]}))
    self.assertEqual(list(nf.Notifier().sent), [("A", dt.datetime(2026, 10, 29, 9, 0, tzinfo=dt.timezone.utc), 15)])

  def test_save_and_load_round_trip(self):
    start = dt.datetime(2026, 11, 5, 9, 0, tzinfo=dt.timezone.utc)
    sent = {("Hard Lotus", start, 15): start, ("Other, with \"quotes\"", start, 0): start}
    checked = dt.datetime(2026, 11, 5, 8, 59, tzinfo=dt.timezone.utc)
    nf.save_state(nf.STATE_FILE, sent, checked)
    self.assertEqual(nf.load_state(nf.STATE_FILE), (sent, checked))
    self.assertFalse(os.path.exists(nf.STATE_FILE + ".tmp"))
    self.assertEqual(nf.load_state(os.path.join(self.dir, "nope.json")), ({}, None))


class HotReloadTests(NotifierTestCase):

  def names(self, notifier):
    return [r.name for r in notifier.schedule.reminders]

  def test_a_broken_edit_keeps_the_previous_schedule(self):
    self.write(WEEKLY)
    n = nf.Notifier()
    n.tick()
    self.write("this is [not toml")
    n.tick()
    self.assertEqual(self.names(n), ["Hard Lotus"])

  def test_a_bad_timezone_keeps_the_previous_schedule(self):
    self.write(WEEKLY)
    n = nf.Notifier()
    n.tick()
    self.write('[[reminders]]\nname = "Two"\ntype = "daily"\ntime = "09:00"\n', header='timezone = "Mars/Base"\n')
    n.tick()
    self.assertEqual(self.names(n), ["Hard Lotus"])

  def test_one_bad_reminder_does_not_stop_the_others(self):
    self.write(WEEKLY + '[[reminders]]\nname = "Bad"\ntype = "weekly"\ntime = "20:00"\n'
               '[[reminders]]\nname = "Two"\ntype = "daily"\ntime = "09:00"\n')
    n = nf.Notifier()
    n.tick()
    self.assertEqual(self.names(n), ["Hard Lotus", "Two"])


class ReportTests(NotifierTestCase):

  def run_report(self, fn, days):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
      code = fn(days)
    return code, buffer.getvalue()

  def test_check_lists_soonest_first_and_no_run_entries_last(self):
    example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reminders.example.toml")
    with open(example, encoding="utf-8") as f:
      self._write(nf.CONFIG_FILE, f.read())
    code, out = self.run_report(nf.run_check, 62)
    self.assertEqual(code, 0, out)
    dated = [l for l in out.splitlines() if l.startswith("- ") and l[2:5] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    stamps = [l[6:22] for l in dated]  # "2026-10-29 20:00"
    self.assertEqual(stamps, sorted(stamps))
    self.assertIn("Paused reminder: disabled", out)
    code, out = self.run_report(nf.run_check, 3)
    self.assertGreater(out.index("no run in the next 3 days"), out.index("Daily check-in"))

  def test_agenda_shows_every_run_in_order(self):
    example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reminders.example.toml")
    with open(example, encoding="utf-8") as f:
      self._write(nf.CONFIG_FILE, f.read())
    code, out = self.run_report(nf.run_agenda, 14)
    lines = [l for l in out.splitlines() if l[:3] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    stamps = [l[4:20] for l in lines]
    self.assertEqual(stamps, sorted(stamps))
    biweekly = [l for l in lines if "Biweekly boss" in l]
    self.assertEqual(len(biweekly), 1)
    self.assertTrue(biweekly[0].startswith("Thu 2026-11-05"))

  def test_check_exits_1_and_explains_errors(self):
    self.write('[[reminders]]\nname = "Bad"\ntype = "weekly"\ntime = "8pm"\n')
    code, out = self.run_report(nf.run_check, 62)
    self.assertEqual(code, 1)
    self.assertIn("24-hour", out)
    self.assertIn("exactly one of 'day'", out)

  def test_check_reports_a_toml_syntax_error(self):
    self.write("oops [")
    code, out = self.run_report(nf.run_check, 62)
    self.assertEqual(code, 1)
    self.assertIn("Could not read reminders.toml", out)

  def test_a_fresh_install_gets_a_valid_starter_file(self):
    code, out = self.run_report(nf.run_check, 62)  # no reminders.toml exists yet
    self.assertEqual(code, 0, out)
    self.assertIn("Example weekly reminder", out)


class MissedRunTests(NotifierTestCase):
  """If the notifier was offline when a reminder was due, it says so afterwards."""

  def lotus(self, notice=15):
    return dt.datetime(2026, 10, 29, 20, 0, tzinfo=MEL).astimezone(dt.timezone.utc)

  def test_downtime_reports_the_missed_run_once(self):
    self.write(WEEKLY)
    self.seed_state(last_check=at(2026, 10, 29, 19, 30))
    n = nf.Notifier()
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 20, 10)), [1])
    text = self.sent[0]
    self.assertIn("Missed reminder", text)
    self.assertIn("**Hard Lotus** started at **20:00 AEDT** (10 minutes ago)", text)
    self.assertIn("while the notifier was offline", text)
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 20, 11)), [0])
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 20, 12)), [0])  # not repeated after a restart
    self.assertIn(nf.MISSED_NOTICE, [e["notice"] for e in self.state_entries()])

  def test_not_reported_if_a_reminder_for_the_run_was_already_sent(self):
    self.write(WEEKLY)
    self.seed_state(last_check=at(2026, 10, 29, 19, 50), sent=[("Hard Lotus", self.lotus(), 15)])
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 20, 10)), [0])

  def test_not_reported_if_the_reminder_was_due_before_the_downtime(self):
    # It was due at 19:45 and the notifier was up until 19:50, so downtime isn't the cause.
    self.write(WEEKLY)
    self.seed_state(last_check=at(2026, 10, 29, 19, 50))
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 20, 10)), [0])

  def test_runs_that_started_too_long_ago_are_not_reported(self):
    self.write(WEEKLY)
    self.seed_state(last_check=at(2026, 10, 29, 19, 30))
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 21, 30)), [0])  # 90 minutes ago; limit is 60

  def test_a_longer_window_can_be_chosen_and_zero_turns_it_off(self):
    self.write(WEEKLY, header=HEADER + "missed_notice_minutes = 120\n")
    self.seed_state(last_check=at(2026, 10, 29, 19, 30))
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 21, 30)), [1])
    os.remove(nf.STATE_FILE)
    self.sent.clear()
    self.write(WEEKLY, header=HEADER + "missed_notice_minutes = 0\n")
    self.seed_state(last_check=at(2026, 10, 29, 19, 30))
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 20, 10)), [0])

  def test_a_fresh_install_has_no_downtime_to_report(self):
    self.write(WEEKLY)
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 20, 10)), [0])

  def test_no_false_alarm_for_a_reminder_added_after_its_time(self):
    self.write('[[reminders]]\nname = "Other"\ntype = "weekly"\nday = "Friday"\ntime = "09:00"\n')
    n = nf.Notifier()
    self.ticks(n, at(2026, 10, 29, 19, 58), at(2026, 10, 29, 19, 59))
    self.write(WEEKLY)  # added at 20:00, so Hard Lotus "started" 0 minutes ago, but the notifier was running
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 20, 0, 30)), [0])

  def test_a_sleeping_computer_counts_as_downtime(self):
    self.write(WEEKLY)
    n = nf.Notifier()
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 19, 0)), [0])
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 20, 10)), [1])  # next check, two hours later, same process
    self.assertIn("Missed reminder", self.sent[0])

  def test_normal_checks_never_look_like_downtime(self):
    self.write(WEEKLY)
    n = nf.Notifier()
    minutes = [at(2026, 10, 29, 19, m) for m in range(40, 60)] + [at(2026, 10, 29, 20, 0, 30), at(2026, 10, 29, 20, 5)]
    self.ticks(n, *minutes)
    self.assertEqual(len(self.sent), 1)
    self.assertNotIn("Missed", self.sent[0])

  def test_several_missed_runs_are_combined_into_one_message(self):
    self.write(WEEKLY + '[[reminders]]\nname = "Second"\ntype = "weekly"\nday = "Thursday"\ntime = "20:05"\n')
    self.seed_state(last_check=at(2026, 10, 29, 19, 30))
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 20, 10)), [1])
    text = self.sent[0]
    self.assertIn("Missed 2 reminders", text)
    self.assertLess(text.index("Hard Lotus"), text.index("Second"))

  def test_a_failed_missed_message_is_retried(self):
    self.write(WEEKLY)
    self.seed_state(last_check=at(2026, 10, 29, 19, 30))
    n = nf.Notifier()
    self.post_ok = False
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 20, 10)), [0])
    self.post_ok = True
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 20, 11)), [1])
    self.assertEqual(self.ticks(n, at(2026, 10, 29, 20, 12)), [0])

  def test_the_note_is_included_for_a_single_missed_run(self):
    self.write(WEEKLY.replace('time = "20:00"', 'time = "20:00"\nnote = "Bring the link"'))
    self.seed_state(last_check=at(2026, 10, 29, 19, 30))
    self.ticks(nf.Notifier(), at(2026, 10, 29, 20, 10))
    self.assertIn("> Bring the link", self.sent[0])

  def test_last_check_is_saved(self):
    self.write(WEEKLY)
    n = nf.Notifier()
    self.ticks(n, at(2026, 10, 29, 12, 0))
    _, last_check = nf.load_state(nf.STATE_FILE)
    self.assertEqual(last_check, at(2026, 10, 29, 12, 0))


class HeartbeatTests(NotifierTestCase):

  def with_heartbeat(self, extra_header=""):
    self.set_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc",
                      "heartbeat_url": "https://hc-ping.example/abc-123"})
    self.write(WEEKLY, header=HEADER + extra_header)

  def test_pings_on_the_first_check_then_every_five_minutes(self):
    self.with_heartbeat()
    n = nf.Notifier()
    self.ticks(n, at(2026, 10, 29, 12, 0), at(2026, 10, 29, 12, 1), at(2026, 10, 29, 12, 4),
               at(2026, 10, 29, 12, 5), at(2026, 10, 29, 12, 6), at(2026, 10, 29, 12, 10))
    self.assertEqual(len(self.pings), 3)  # 12:00, 12:05, 12:10
    self.assertEqual(self.pings[0], "https://hc-ping.example/abc-123")

  def test_the_interval_can_be_changed(self):
    self.with_heartbeat("heartbeat_minutes = 1\n")
    self.ticks(nf.Notifier(), at(2026, 10, 29, 12, 0), at(2026, 10, 29, 12, 1), at(2026, 10, 29, 12, 2))
    self.assertEqual(len(self.pings), 3)

  def test_no_heartbeat_url_means_no_pings(self):
    self.write(WEEKLY)
    self.ticks(nf.Notifier(), at(2026, 10, 29, 12, 0))
    self.assertEqual(self.pings, [])

  def test_the_environment_variable_works(self):
    self.write(WEEKLY)
    with mock.patch.dict(os.environ, {"HEARTBEAT_URL": "https://hc-ping.example/from-env"}):
      self.ticks(nf.Notifier(), at(2026, 10, 29, 12, 0))
    self.assertEqual(self.pings, ["https://hc-ping.example/from-env"])

  def test_no_ping_while_reminders_cant_be_sent(self):
    self.set_secrets({"heartbeat_url": "https://hc-ping.example/abc-123"})  # no webhook
    self.write(WEEKLY)
    n = nf.Notifier()
    self.ticks(n, at(2026, 10, 29, 12, 0), at(2026, 10, 29, 12, 6))
    self.assertEqual(self.pings, [])
    self.set_secrets({"webhook_url": "https://example.com/not-discord", "heartbeat_url": "https://hc-ping.example/abc-123"})
    self.ticks(n, at(2026, 10, 29, 12, 12))
    self.assertEqual(self.pings, [])  # a malformed webhook counts as unusable too
    self.set_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc", "heartbeat_url": "https://hc-ping.example/abc-123"})
    self.ticks(n, at(2026, 10, 29, 12, 13))
    self.assertEqual(len(self.pings), 1)

  def test_no_ping_before_a_schedule_has_loaded(self):
    self.set_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc", "heartbeat_url": "https://hc-ping.example/abc-123"})
    self.write("not [valid toml")
    self.ticks(nf.Notifier(), at(2026, 10, 29, 12, 0))
    self.assertEqual(self.pings, [])

  def test_a_failing_ping_is_retried_every_check_and_recovers(self):
    self.with_heartbeat()
    n = nf.Notifier()
    self.ping_ok = False
    self.ticks(n, at(2026, 10, 29, 12, 0), at(2026, 10, 29, 12, 1), at(2026, 10, 29, 12, 2))
    self.assertEqual(len(self.pings), 3)  # tried at every check
    self.assertTrue(n.heartbeat_failing)
    self.ping_ok = True
    self.ticks(n, at(2026, 10, 29, 12, 3), at(2026, 10, 29, 12, 4))
    self.assertEqual(len(self.pings), 4)  # worked at 12:03, so 12:04 waits
    self.assertFalse(n.heartbeat_failing)

  def test_a_failing_ping_never_stops_reminders(self):
    self.with_heartbeat()
    self.ping_ok = False
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 45, 30)), [1])


class SecretsTests(NotifierTestCase):

  def test_a_malformed_webhook_sends_nothing_and_says_why(self):
    self.set_secrets({"webhook_url": "https://example.com/hook"})
    self.write(WEEKLY)
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 45, 30)), [0])
    secrets = nf.resolve_secrets({"webhook_url": "https://example.com/hook"})
    self.assertEqual(secrets.webhook, "")
    self.assertIn("doesn't look like a Discord webhook URL", str(secrets.problems[0]))
    self.assertNotIn("example.com", str(secrets.problems[0]))  # never echo a secret

  def test_accepted_webhook_shapes(self):
    good = ["https://discord.com/api/webhooks/123456/abc_DEF-123",
            "https://discordapp.com/api/webhooks/1/abc",
            "https://canary.discord.com/api/webhooks/1/abc",
            "https://discord.com/api/webhooks/1/abc?thread_id=99"]
    bad = ["", "discord.com/api/webhooks/1/abc", "http://discord.com/api/webhooks/1/abc",
           "https://discord.com/api/webhooks/abc/def", "https://discord.com/api/webhooks/1/"]
    for url in good:
      self.assertEqual(nf.resolve_secrets({"webhook_url": url}).webhook, url, url)
    for url in bad:
      self.assertEqual(nf.resolve_secrets({"webhook_url": url}).webhook, "", url)

  def test_a_bad_user_id_turns_off_pings_but_still_sends(self):
    self.set_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc", "discord_user_id": "not-a-number"})
    self.write(WEEKLY)
    self.assertEqual(self.ticks(nf.Notifier(), at(2026, 10, 29, 19, 45, 30)), [1])
    self.assertFalse(self.sent[0].startswith("<@"))

  def test_a_bad_heartbeat_url_is_ignored(self):
    secrets = nf.resolve_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc", "heartbeat_url": "ftp://nope"})
    self.assertEqual(secrets.heartbeat_url, "")
    self.assertEqual([p.level for p in secrets.problems], ["warning"])

  def test_secret_problems_are_logged_once_not_every_check(self):
    self.set_secrets({"webhook_url": "https://example.com/hook"})
    self.write(WEEKLY)
    logging.disable(logging.NOTSET)
    n = nf.Notifier()
    with self.assertLogs(level="ERROR") as logs:
      self.ticks(n, at(2026, 10, 29, 12, 0), at(2026, 10, 29, 12, 1), at(2026, 10, 29, 12, 2))
    self.assertEqual(len([r for r in logs.records if "webhook" in r.getMessage()]), 1)

  def test_check_reports_an_unusable_webhook(self):
    self.set_secrets({"webhook_url": "https://example.com/hook"})
    self.write(WEEKLY)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
      code = nf.run_check(62)
    self.assertEqual(code, 1)
    self.assertIn("NOT USABLE", buffer.getvalue())

  def test_check_summarises_heartbeat_and_missed_settings(self):
    self.set_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc", "discord_user_id": "42",
                      "heartbeat_url": "https://hc-ping.example/abc"})
    self.write(WEEKLY)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
      code = nf.run_check(62)
    out = buffer.getvalue()
    self.assertEqual(code, 0, out)
    for expected in ("Webhook:   configured", "Ping you:  yes", "Heartbeat: every 5 min",
                     "runs that started up to 60 min ago"):
      self.assertIn(expected, out)

  def test_the_test_command_sends_a_message_and_pings_the_heartbeat(self):
    self.set_secrets({"webhook_url": "https://discord.com/api/webhooks/1/abc", "discord_user_id": "42",
                      "heartbeat_url": "https://hc-ping.example/abc"})
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
      code = nf.run_test()
    self.assertEqual(code, 0)
    self.assertEqual(len(self.sent), 1)
    self.assertEqual(self.pings, ["https://hc-ping.example/abc"])
    self.assertIn("Heartbeat ping: sent", buffer.getvalue())


class HelperTests(unittest.TestCase):

  def test_due_notice(self):
    start = dt.datetime(2026, 10, 29, 20, 0, tzinfo=dt.timezone.utc)
    m = lambda minutes: start + dt.timedelta(minutes=minutes)
    self.assertIsNone(nf.due_notice((15,), start, m(-16), 60))
    self.assertEqual(nf.due_notice((15,), start, m(-14), 60), 15)
    self.assertIsNone(nf.due_notice((15,), start, m(1), 60))
    self.assertEqual(nf.due_notice((60, 10), start, m(-5), 60), 10)
    self.assertEqual(nf.due_notice((60, 10), start, m(-30), 60), 60)
    self.assertEqual(nf.due_notice((0,), start, start + dt.timedelta(seconds=30), 60), 0)
    self.assertIsNone(nf.due_notice((0,), start, start + dt.timedelta(seconds=61), 60))

  def test_format_remaining(self):
    cases = {1: "1 minute", 15: "15 minutes", 60: "1 hour", 90: "1 hour 30 minutes",
             1440: "1 day", 1500: "1 day 1 hour", 2880: "2 days"}
    for minutes, text in cases.items():
      self.assertEqual(nf.format_remaining(minutes), text)


if __name__ == "__main__":
  unittest.main()
