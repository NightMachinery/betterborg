# Timetracker time zones

The timetracker (`stdplugins/timetracker.py`, with the model in
`uniborg/timetracker_util.py`) records activities as a chain: each one starts
where the previous one ended, and `.` stretches the latest one to now.

## What is stored

Every activity keeps its times as **local wall-clock time in its own zone**:

- `start` and `end`: the local time where the activity happened, exactly as
  you lived it;
- `tz`: that place's IANA zone name, such as `America/New_York`;
- `start_utc` and `end_utc`: the same instants in UTC, derived from the three
  columns above whenever the activity is saved.

The local time is the record. The UTC columns are there only for ordering.
Local times can repeat or jump: an hour repeats when daylight saving ends, and
a trip moves you between zones. So "the latest activity that ended before now",
which `.`, `b` and new entries all depend on, is chosen by `end_utc`. Choosing
it by local time once picked the wrong activity: activities recorded on one
zone's clock looked like they were in the future on another's.

A message is stamped with the time it was sent, not the time the bot handled
it, so messages sent while the bot was down or busy still land where they
belong. Two corrections keep the chain in order:

- Telegram dates messages to the second, so a message that is not later than
  the latest recorded end, such as a second message sent in the same second,
  is stamped one microsecond after that end instead.
- A date ahead of the server's clock is capped at the server's now, so no
  activity ends in the future.

When two activities end at the same instant, the one that started first
counts as the latest. That is the activity a zero-length `+` marker was
recorded after.

Durations are real elapsed time, computed from the instants. A night that
spans the end of daylight saving counts the repeated hour.

Reports keep working in local time. A day in a report is a local day, with
the day boundary at `DAY_START` (05:00), in whatever zone each activity
happened.

## The current zone

The *current zone* is where new activities are recorded and what "now" means.
It is resolved in this order:

1. the `tz` setting in the database's `timetracker_setting` table, set with
   `.tz`;
2. the `timetracker_tz` environment variable;
3. the host's zone (`TZ`, else `/etc/localtime`), then UTC.

Because the setting lives in the database, the tracker follows you when you
travel: change it with `.tz` and the server's own zone does not matter.

- `.tz` reports the current zone, where it came from, the local time now, the
  host's zone, and the zone of the latest activity.
- `.tz ZONE` sets it, for example `.tz America/New_York`. Names match
  case-insensitively, so `.tz america/new_york` works too.

An activity keeps the zone it started in. When you land and send `.`, the
activity you were in (the flight, say) is extended in its own zone; the next
one starts in the new zone at the same instant.

## Other entry points

- `..` lists the ten newest activities with their local times and zone
  abbreviations, and adds the IANA zone name to any activity recorded outside
  the current zone. It reads the database directly instead of shelling out.
- Replying to an activity's message runs the command at that activity's end,
  converted into the current zone.
- The `/timetracker/mark/` webhook (`stdborg.py`) takes an optional
  `received_at` in RFC 2822 form. A date with an offset is converted into the
  current zone. One without an offset is read as the current zone's local time.

## Habit heatmaps on Python 3.13 and later

Habit reports first display their totals, then run `calendarheatmap` through
`uniborg.util.za` to generate the image. If the totals appear followed by
`TypeError: globals must be a real dict`, command interpolation failed before
`calendarheatmap` ran.

Python 3.13 changed a function frame's `f_locals` from a dictionary to a
`FrameLocalsProxy`. The async helper passed that proxy as Brish's explicit
namespace, which Brish passes to `eval` as globals. `eval` requires a real
dictionary there. See the
[Python porting notes](https://docs.python.org/3.13/whatsnew/3.13.html#changes-in-the-python-api).

`za` now copies the selected namespace to a dictionary before scheduling the
worker. This preserves Brish's expression evaluation and shell quoting, accepts
explicit mappings, and avoids `eval` adding builtins to the caller's namespace.
An explicitly empty namespace remains empty rather than falling back to the
caller's locals. The copy is shallow: referenced objects retain their identity.

Reloading the timetracker plugin cannot update the already imported helper.
After deploying this change, restart the bot processes that import
`uniborg.util`. No activity database migration or Brish upgrade is required.

## Migrating a database recorded without zones

Before zones were stored, every time was the host clock's naive local time.
If the host's zone never differed from where you were, the first start of the
new code handles everything itself. It adds the columns and assigns every old
row the current zone, reading its times as local there.

If they did differ, for example because you moved while the server kept its
old zone, or because the server's zone changed under a running bot, run the
migration first, with the bot stopped. It needs two timelines:

- `--recorded-in ZONE`, plus any `--recorded-in-from-id ID=ZONE`: which zone's
  clock wrote each row, by row id. Rows from `ID` on were written under
  `ZONE`'s clock.
- `--lived-in ZONE`, plus any `--moved INSTANT=ZONE`: where you were. From
  `INSTANT` (ISO 8601 with a UTC offset) on, you were in `ZONE`. A row takes
  the zone you were in when it started.

For each row it reads the times on the recording clock, then rewrites them as
local time where you were. It stores that zone, the UTC instants, and finally
sets the current zone to where you ended up.

```sh
python3 uniborg/timetracker_tz.py migrate --db ~/.timetracker/timetracker.db \
    --recorded-in America/Chicago \
    --lived-in America/Chicago --moved 2026-03-01T18:00:00-06:00=Europe/Lisbon
```

Run it by path. Importing the `uniborg` package starts the bot's shell servers.

Without `--apply` it writes nothing. It prints how many rows it would shift,
per recording and living zone, and samples around each zone change. It also
prints a consistency check: how many rows do not start exactly when the
previous one ended, before and after. Those counts must match, because the
migration moves local times but never instants. It touches only rows without a
zone, so running it twice never shifts a row twice. Back the database up first
all the same.

Rows whose times were rewritten by two different clocks cannot be read by
either timeline. For example, a row the old clock created and a new clock then
extended with `.`. Repair those by hand before migrating.
