# Reminder Notifier

A small Python daemon that sends a Discord webhook message shortly before each scheduled event. Use it for anything: game bosses, meetings, bills, birthdays, renewals. It runs on Linux (systemd) and Windows (run-at-login).

```
🔔 Reminder: Hard Lotus is scheduled for 20:00 AEDT (in 15 minutes).
```

## Features

- Five schedule types: **once**, **daily**, **weekly**, **monthly** and **yearly**
- Every N days, weeks or months (biweekly, every 4 weeks, quarterly)
- Several days in one entry, plus `"weekdays"` and `"weekends"` shortcuts
- Monthly on a date, the last day, the last or first weekday, or the Nth weekday ("last Friday")
- Weekend shifting for fixed dates (pay day on the 15th, or the Friday before)
- Skip dates and date ranges (holidays)
- Several reminders per event (for example a day before and 10 minutes before)
- An optional note under the reminder
- One timezone for the whole file, with daylight saving handled
- Strict validation with plain-English errors; a mistake in one entry never stops the others
- The schedule reloads when you save it; a bad edit keeps the last good version running
- Remembers what it has already sent, so a restart or reboot never repeats a reminder
- Tells you about reminders it missed while it was offline or the computer was asleep
- Optional heartbeat: pings a monitoring URL so you're alerted if the notifier stops
- Checks your webhook URL, user ID and heartbeat URL, and says what's wrong

## Files

| File | Purpose |
| --- | --- |
| `notifier.py` | The notifier, command line and Windows startup |
| `reminder_schedule.py` | All scheduling and validation rules |
| `reminders.toml` | Your schedule (commit this) |
| `reminders.example.toml` | Every option, with examples |
| `secrets.json` | Webhook URL and your Discord user ID (**gitignored, never commit**) |
| `secrets.example.json` | Template for `secrets.json` |
| `state.json` | Which reminders were already sent, and when the script last ran. Written by the script (gitignored) |
| `reminder-notifier.service` | systemd unit for Linux |
| `tests/test_schedule.py` | Tests for the scheduling and validation rules |
| `tests/test_notifier.py` | Tests for sending, reminder windows, saved state and reloading |
| `.github/workflows/tests.yml` | Runs the tests on every pull request to `main` |
| `notifier.log` | Rotating log file, created at runtime |

## Setup

Requires **Python 3.11 or newer** (it reads TOML with the built-in `tomllib`).

```bash
git clone <your-private-repo-url> reminder-notifier
cd reminder-notifier
python3 -m venv venv
./venv/bin/pip install -r requirements.txt      # Windows: venv\Scripts\pip install -r requirements.txt
cp secrets.example.json secrets.json
chmod 600 secrets.json                          # Linux
nano secrets.json
```

`secrets.json`:

```json
{
    "webhook_url": "https://discord.com/api/webhooks/...",
    "discord_user_id": "123456789012345678",
    "heartbeat_url": ""
}
```

- **Webhook URL:** in Discord, open the channel's settings, then Integrations, then Webhooks, and create one.
- **User ID (optional, for pings):** turn on Developer Mode in Discord's advanced settings, right-click your name, and choose Copy User ID. Leave it blank for no ping.
- **Heartbeat URL (optional):** see [Heartbeat](#heartbeat-know-if-the-notifier-stops). Leave it blank to switch it off.
- The environment variables `DISCORD_WEBHOOK_URL`, `DISCORD_USER_ID` and `HEARTBEAT_URL` override the file if set.

Then write your schedule (see below), and check it:

```bash
./venv/bin/python notifier.py --check      # validate, and show when each reminder next runs
./venv/bin/python notifier.py --agenda     # every run in the next 30 days, in date order
./venv/bin/python notifier.py --test       # send a test message to Discord
```

If `reminders.toml` doesn't exist, the script creates a one-reminder starter file.

## Running it

**Linux (systemd)**

Edit `User=`, `WorkingDirectory=` and `ExecStart=` in `reminder-notifier.service` if your username or folder differs from `ubuntu` and `/home/ubuntu/bots/reminder-notifier`, then:

```bash
sudo cp reminder-notifier.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reminder-notifier
journalctl -u reminder-notifier -f
```

**Windows**

```bat
venv\Scripts\python notifier.py                               :: run in a console
venv\Scripts\python notifier.py --install-startup --hidden    :: run at login, no console
venv\Scripts\python notifier.py --startup-status
venv\Scripts\python notifier.py --uninstall-startup
```

At login it waits 60 seconds for the network. Re-run `--install-startup` if you move or rename the script.

> **Don't run it on two machines at once.** Both will send every reminder. Uninstall the Windows startup entry when you move to a server.

## The schedule file

`reminders.toml` has a few settings at the top, then one `[[reminders]]` block per reminder.

```toml
timezone = "Australia/Melbourne"
default_notice_minutes = 15

[[reminders]]
name = "Hard Lotus"
type = "weekly"
day = "Thursday"
time = "20:00"
```

### Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `timezone` | the machine's timezone | A name like `"Australia/Melbourne"`. Set it, because servers usually run UTC. All times in the file are in this timezone. |
| `default_notice_minutes` | `15` | How long before to remind you. A list like `[60, 10]` sends two reminders. |
| `check_interval_seconds` | `60` | How often to check (minimum 5). It's lowered automatically if a short notice needs it. |
| `missed_notice_minutes` | `60` | If the notifier was offline when a reminder was due, still tell you about runs that started up to this many minutes ago. `0` turns it off. |
| `heartbeat_minutes` | `5` | How often to ping `heartbeat_url` (1 to 60). Only used if you set one. |

### Every reminder

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Shown in the message. Must be unique. |
| `type` | yes | `once`, `daily`, `weekly`, `monthly` or `yearly` |
| `time` | yes | 24-hour text like `"20:00"` (not `"8pm"`) |
| `notice_minutes` | no | One number or a list, in minutes before. Overrides the default. `0` means at the start time; a day before is `1440`. |
| `skip` | no | Dates and ranges to skip: `["2026-12-25", "2026-12-24..2026-12-31"]` |
| `note` | no | An extra line shown under the reminder (up to 500 characters) |
| `enabled` | no | `false` switches it off without deleting it |

### The five types

| `type` | Fields | Example |
| --- | --- | --- |
| `once` | `date` | `date = "2026-12-25"` |
| `daily` | `every_days` and `starts` (optional) | every 3 days: `every_days = 3`, `starts = "2026-11-01"` |
| `weekly` | `day` or `days`; optional `every_weeks` and `starts` | `days = ["Mon", "Thu"]`, or `days = "weekdays"` |
| `monthly` | `on`, or `nth` with `day`; optional `every_months`, `starts`, `if_weekend` | see below |
| `yearly` | `month`, `on`; optional `if_weekend` | `month = "March"`, `on = 14` |

- **`starts`:** the date of the first run. It's required whenever you use `every_days`, `every_weeks` or `every_months` above 1, because "every 2 weeks" needs to know which week counts as the first. Nothing runs before it. Weeks are counted Monday to Sunday.
- **Day names:** full names or three letters (`"Thursday"`, `"Thu"`). `days` can also be `"weekdays"` or `"weekends"`.

### Monthly options

Use exactly one of these:

| Written as | Meaning |
| --- | --- |
| `on = 15` | The 15th. A day number 1 to 31; in a shorter month it becomes the last day (the 31st in April is the 30th). |
| `on = "last"` | The last day of the month |
| `on = "last_weekday"` | The last Monday to Friday day |
| `on = "first_weekday"` | The first Monday to Friday day |
| `nth = -1` with `day = "Friday"` | The last Friday. `nth` is 1 to 4 (first to fourth) or -1 (last). |

Add `every_months = 3` with `starts` for quarterly, or `6` for twice a year.

### `if_weekend`

For a fixed day number (`on = 15`), say what to do when that date lands on a Saturday or Sunday:

- `"before"` moves it to the Friday
- `"after"` moves it to the Monday
- leave it out to keep the weekend date

Example: `on = 15` with `if_weekend = "before"` fires on Friday 13 November when the 15th is a Sunday. It also works for `yearly`. It isn't allowed with `"last_weekday"`, `"first_weekday"` or `nth`, which already avoid or define their own days.

### Several reminders per event

```toml
notice_minutes = [1440, 60, 10]    # a day, an hour and 10 minutes before
```

If the notifier starts late, it sends only the most recent reminder that applies, so you never get an old "in 1 hour" message 5 minutes before the event. Messages read "in 1 hour", "in 2 days" and so on, and show the date when the event isn't today.

All options are shown, with comments, in `reminders.example.toml`.

## Validation

Every problem is reported in plain English, with the reminder's name:

```
✗ reminder 'Biweekly boss' needs 'starts' when every_weeks is more than 1 (it says which one counts as the first).
✗ reminder 'Pay day' has 'if_weekend', which only applies when 'on' is a day number.
✗ reminder 'Standup' has an unknown field 'notice_minute'. Did you mean 'notice_minutes'?
```

- An entry with an error is **skipped**, and every other reminder keeps working.
- A field that belongs to another type (a `date` on a weekly reminder) is an error, because silently ignoring it is how mistakes hide.
- Warnings, such as a missing timezone or a one-off date in the past, don't skip anything.
- A bad timezone, or a file that isn't valid TOML, ignores that edit and keeps the previous schedule running. The reason is in the log.
- Secrets are checked too: a webhook that isn't a Discord URL (it must look like `https://discord.com/api/webhooks/<number>/<token>`) stops reminders and is reported, a `discord_user_id` that isn't a number turns pings off, and a `heartbeat_url` that isn't `http(s)` is ignored. The messages never include the URL itself, and each problem is logged once, not on every check.

`--check` prints all problems and exits with status 1 if any reminder was skipped or the webhook isn't usable, so you can run it after every edit.

## Restarts and `state.json`

Each time a reminder is sent, it's recorded in `state.json` straight away. After a restart, a reboot or a deploy, the notifier reads that file and knows not to send the same reminder again. Entries are dropped an hour after their run starts, so the file stays tiny. If the file is missing or damaged, the script just carries on (at worst, one reminder inside its window is sent again). Renaming a reminder counts as a new one, since names identify them.

## Missed reminders

If the notifier is stopped, crashes, loses power, or the computer goes to sleep, reminders due in that time can't be sent. When it's running again, it works out that it was away and posts one message for anything that started in the meantime:

```
⚠️ Missed reminder: Hard Lotus started at 20:00 AEDT (10 minutes ago), while the notifier was offline.
```

Several missed runs are combined into one message (up to 10 listed). A few details:

- Only runs that **started** within `missed_notice_minutes` (default 60) of the notifier coming back are reported. Older ones are dropped, and `0` turns the feature off.
- A run is reported only if nothing was sent for it and its reminder was due while the notifier was away. If a reminder was due while it was running, or one was already sent, nothing is reported. A reminder you add just after its time isn't reported either.
- "Away" means no checks for more than three check intervals (at least 3 minutes). The script records when it last ran in `state.json`, so a brand-new install has nothing to report.
- Reminders that are still in the future are unaffected: if you come back before the event, you get the normal reminder.

## Heartbeat: know if the notifier stops

If the server dies, or Oracle reclaims an idle free-tier instance, reminders silently stop. A heartbeat means a monitoring service tells *you* when that happens.

1. Create a check on a service that accepts pings, such as [healthchecks.io](https://healthchecks.io) (it has a free tier) or an Uptime Kuma push monitor. Set its expected period a little longer than `heartbeat_minutes` (default 5), for example a 5 minute period with a few minutes of grace, and choose how it should alert you (email, Discord, and so on).
2. Copy the ping URL it gives you into `secrets.json` as `heartbeat_url` (or set `HEARTBEAT_URL`).
3. Run `python notifier.py --test`. It sends a test message **and** pings the URL, so you can confirm the monitor receives it.

The notifier pings every `heartbeat_minutes`, but **only while it's actually able to send reminders**: the schedule has loaded and the webhook is valid. If it's stopped, stuck, powered off, or its webhook is broken, the pings stop and your monitor raises the alarm. A failed ping never affects reminders; it's logged once and retried at every check. The URL is never written to the log.

## Commands

| Command | What it does |
| --- | --- |
| `python notifier.py` | Run the notifier |
| `--check [--days N]` | Validate the schedule and secrets, then show the next run of each reminder, soonest first (looks 62 days ahead by default) |
| `--agenda [--days N]` | List every run in the next N days (default 30) in date order. Best for confirming a biweekly or monthly pattern. |
| `--test` | Send a test message to the webhook, and ping the heartbeat URL if there is one |
| `--install-startup [--hidden]` | (Windows) Run at login |
| `--uninstall-startup` | (Windows) Remove the login entry |
| `--startup-status` | (Windows) Show whether run-at-login is enabled |

## Moving from the old boss notifier

1. Rename the script and the repo, and use `reminders.toml` instead of `boss_schedule.json`.
2. Rewrite each entry as a `[[reminders]]` block, with a `type` on every one. A `"day"` entry becomes `type = "weekly"`, and a `"date"` entry becomes `type = "once"`.
3. Run `python notifier.py --check` and fix anything it reports.
4. On the VPS, stop and disable the old service, then install `reminder-notifier.service`.

## Tests

```bash
python -m unittest discover tests
```

They cover the recurrence and validation rules, when reminders are sent (including late starts and restarts), saved state, reloading, and `--check` / `--agenda`. No network or real Discord is used.

## Continuous integration

`.github/workflows/tests.yml` runs the whole test suite with GitHub Actions on **every pull request that targets `main`**, on Python 3.11 (the oldest supported version) and 3.12. Each push to the pull request re-runs it, and the result shows on the PR as a green tick or a red cross. It runs the same command you can run locally:

```bash
python -m unittest discover -s tests -v
```

A passing check doesn't stop anyone merging by itself. To make passing tests **required**:

1. In the GitHub repo, open **Settings**, then **Branches** (or **Rules**, then **Rulesets**), and add a rule for `main`.
2. Turn on **Require status checks to pass before merging**.
3. Search for and add `tests (Python 3.11)` and `tests (Python 3.12)`. They only appear in the search once the workflow has run at least once, so open a test pull request first.

On a free personal account, branch protection for *private* repositories may need a paid plan. The checks still run and show on every pull request either way.

The workflow pins `actions/checkout@v7` and `actions/setup-python@v6`. If GitHub warns that one is out of date, bump the number.

## Troubleshooting

- **Reminders arrive at the wrong time:** check `timezone`, and run `--check` or `--agenda` to see the times as the script understands them.
- **"Unknown timezone" on Windows:** `pip install tzdata` (it's in `requirements.txt`), then restart the script.
- **Nothing is sent and the log says the webhook is missing or doesn't look right:** check `secrets.json` is next to the script, is valid JSON, and contains the full webhook URL. `--check` shows what the script sees.
- **A "Missed reminder" message appeared:** the notifier wasn't running when that reminder was due (a restart, outage, or the computer sleeping). Check `journalctl -u reminder-notifier` for the "No checks were made between..." line. If it keeps happening on Windows, the computer is probably going to sleep.
- **The heartbeat monitor says the notifier is down:** check it's running (`systemctl status reminder-notifier`), that the schedule loaded, and that the webhook is valid. The heartbeat is deliberately silent when reminders can't be sent.
- **A reminder is missing from `--check`:** it was skipped because of an error, or it has `enabled = false`.
- **Messages sent twice:** it's running on two machines or two copies. A restart doesn't cause repeats, because `state.json` remembers what was sent. Deleting `state.json` makes it forget, so any reminder still inside its window is sent again.
- **Edited the schedule but nothing changed:** check the log for an error. The previous version keeps running until the file is valid again.

## Security notes

- `secrets.json` is gitignored. If a webhook URL is ever committed, delete the webhook in Discord and create a new one, because git history keeps old versions.
- The webhook URL is never written to the log.
- Keep the repo private.
