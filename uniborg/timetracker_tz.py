"""Time zones for the timetracker.

An activity stores its ``start`` and ``end`` as wall-clock times in its own
zone, ``tz``, an IANA name such as ``America/New_York``. The local time is the
record; ``start_utc`` and ``end_utc`` are derived from it on every save and
exist only so that ordering and "the latest activity before now" stay right
across daylight-saving changes and travel, where local times repeat or jump.

The *current zone* is where new activities are recorded. It comes from the
``tz`` setting in the database (set with ``.tz``), else the ``timetracker_tz``
environment variable, else the host's zone.

This module uses only the standard library. Run the migration below by
path, not with `-m`: importing the `uniborg` package starts the bot's shell
servers.

    python3 uniborg/timetracker_tz.py migrate --db PATH \\
        --recorded-in ZONE [--recorded-in-from-id ID=ZONE ...] \\
        --lived-in ZONE [--moved INSTANT=ZONE ...] [--apply]
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

UTC = dt.timezone.utc
TZ_ENV = "timetracker_tz"
TZ_SETTING = "tz"
SETTING_TABLE = "timetracker_setting"
LOCALTIME_PATH = Path("/etc/localtime")


def zone(name: str) -> ZoneInfo:
    """The ZoneInfo for NAME, or ValueError naming the bad zone."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown time zone: {name!r}") from e


def canonical_zone_name(name: str) -> str:
    """NAME as a valid IANA zone, matched case-insensitively if need be.

    Phones capitalize and lowercase freely, so `america/new_york` is accepted.
    Matching goes through the zone list rather than ZoneInfo, because on a
    case-insensitive filesystem ZoneInfo accepts `america/new_york` as is, and a
    Linux host would later reject the stored name. Raises ValueError for a
    name that matches no zone.
    """
    zones = available_timezones()
    if name in zones:
        return name
    wanted = name.casefold()
    for candidate in sorted(zones):
        if candidate.casefold() == wanted:
            return candidate
    raise ValueError(
        f"unknown time zone: {name!r} (use an IANA name like America/New_York)"
    )


def host_zone_name(
    *,
    env: Optional[Mapping[str, str]] = None,
    localtime: Path = LOCALTIME_PATH,
) -> Optional[str]:
    """The host's IANA zone: ``TZ`` if it names one, else /etc/localtime's target."""
    env = os.environ if env is None else env
    candidate = (env.get("TZ") or "").lstrip(":")
    if candidate:
        try:
            zone(candidate)
            return candidate
        except ValueError:
            pass

    try:
        target = os.path.realpath(localtime)
    except OSError:
        return None
    marker = "zoneinfo" + os.sep
    if marker not in target:
        return None
    candidate = target.split(marker, 1)[1]
    try:
        zone(candidate)
    except ValueError:
        return None
    return candidate


def default_zone_name(*, env: Optional[Mapping[str, str]] = None) -> str:
    """The zone to use when the database has no ``tz`` setting."""
    env = os.environ if env is None else env
    configured = env.get(TZ_ENV)
    if configured:
        zone(configured)
        return configured
    return host_zone_name(env=env) or "UTC"


def localize(local: dt.datetime, zone_name: str) -> dt.datetime:
    """LOCAL (naive wall-clock time in ZONE_NAME) as an aware datetime.

    A repeated wall-clock time (the hour after a daylight-saving fall-back)
    resolves to its first occurrence.
    """
    if local.tzinfo is not None:
        raise ValueError(f"expected a naive local time, got {local!r}")
    return local.replace(tzinfo=zone(zone_name), fold=0)


def to_utc(local: dt.datetime, zone_name: str) -> dt.datetime:
    """LOCAL in ZONE_NAME as a naive UTC datetime."""
    return localize(local, zone_name).astimezone(UTC).replace(tzinfo=None)


def to_local(moment: dt.datetime, zone_name: str) -> dt.datetime:
    """The aware datetime MOMENT as naive wall-clock time in ZONE_NAME."""
    if moment.tzinfo is None:
        raise ValueError(f"expected an aware datetime, got {moment!r}")
    return moment.astimezone(zone(zone_name)).replace(tzinfo=None)


def convert(local: dt.datetime, *, from_zone: str, to_zone: str) -> dt.datetime:
    """Wall-clock time LOCAL in FROM_ZONE, as wall-clock time in TO_ZONE."""
    if from_zone == to_zone:
        return local
    return to_local(localize(local, from_zone), to_zone)


def local_now(zone_name: str, *, clock=None) -> dt.datetime:
    """Now, as naive wall-clock time in ZONE_NAME. CLOCK returns an aware now."""
    now = clock() if clock else dt.datetime.now(UTC)
    return to_local(now, zone_name)


def elapsed(start: dt.datetime, end: dt.datetime, zone_name: str) -> dt.timedelta:
    """Real time between two wall-clock times in ZONE_NAME (DST-correct).

    Both go through UTC: Python subtracts aware datetimes that share a tzinfo
    as wall-clock times, which would ignore a daylight-saving change.
    """
    return localize(end, zone_name).astimezone(UTC) - localize(
        start, zone_name
    ).astimezone(UTC)


def db_datetime(value: dt.datetime) -> str:
    """VALUE as the text peewee's DateTimeField stores and parses."""
    return value.isoformat(" ", timespec="microseconds")


def parse_db_datetime(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


## Migration


@dataclasses.dataclass(frozen=True)
class ZoneChange:
    """From AT on, the zone is ZONE. AT is an aware instant or a row id."""

    at: object
    zone: str


@dataclasses.dataclass(frozen=True)
class ZoneTimeline:
    """A zone that changes at given points, which must be in increasing order."""

    initial: str
    changes: tuple = ()

    def __post_init__(self):
        zone(self.initial)
        for change in self.changes:
            zone(change.zone)
        points = [change.at for change in self.changes]
        if points != sorted(points):
            raise ValueError("zone changes must be given in increasing order")

    def zone_at(self, point) -> str:
        current = self.initial
        for change in self.changes:
            if point >= change.at:
                current = change.zone
            else:
                break
        return current

    @property
    def final(self) -> str:
        return self.changes[-1].zone if self.changes else self.initial


def parse_instant_change(text: str) -> ZoneChange:
    """``2025-06-11T02:00:00-05:00=Europe/Lisbon`` as a ZoneChange."""
    instant, sep, zone_name = text.rpartition("=")
    if not sep:
        raise ValueError(f"expected INSTANT=ZONE, got {text!r}")
    at = dt.datetime.fromisoformat(instant)
    if at.tzinfo is None:
        raise ValueError(f"the instant needs a UTC offset: {instant!r}")
    return ZoneChange(at=at, zone=zone_name)


def parse_id_change(text: str) -> ZoneChange:
    """``1200=Europe/Lisbon`` as a ZoneChange keyed by row id."""
    row_id, sep, zone_name = text.partition("=")
    if not sep:
        raise ValueError(f"expected ID=ZONE, got {text!r}")
    return ZoneChange(at=int(row_id), zone=zone_name)


@dataclasses.dataclass(frozen=True)
class MigratedRow:
    id: int
    name: str
    old_start: dt.datetime
    old_end: dt.datetime
    start: dt.datetime
    end: dt.datetime
    tz: str
    recorded_in: str
    start_utc: dt.datetime
    end_utc: dt.datetime

    @property
    def shifted(self) -> bool:
        return (self.start, self.end) != (self.old_start, self.old_end)


def migrate_row(
    row_id: int,
    name: str,
    start: dt.datetime,
    end: dt.datetime,
    *,
    recorded: ZoneTimeline,
    lived: ZoneTimeline,
) -> MigratedRow:
    """Reinterpret one legacy row.

    RECORDED says, by row id, which zone's clock wrote the naive times.
    LIVED says, by instant, which zone the user was in; the row takes the
    zone they were in when it started.
    """
    recorded_in = recorded.zone_at(row_id)
    start_at = localize(start, recorded_in)
    end_at = localize(end, recorded_in)
    tz = lived.zone_at(start_at)
    return MigratedRow(
        id=row_id,
        name=name,
        old_start=start,
        old_end=end,
        start=to_local(start_at, tz),
        end=to_local(end_at, tz),
        tz=tz,
        recorded_in=recorded_in,
        start_utc=start_at.astimezone(UTC).replace(tzinfo=None),
        end_utc=end_at.astimezone(UTC).replace(tzinfo=None),
    )


ACTIVITY_TZ_COLUMNS = (
    ("tz", "VARCHAR(255)"),
    ("start_utc", "DATETIME"),
    ("end_utc", "DATETIME"),
)
ACTIVITY_TZ_INDEXES = (
    "CREATE INDEX IF NOT EXISTS activity_end_utc_index ON activity (end_utc DESC)",
    "CREATE INDEX IF NOT EXISTS activity_start_utc_index ON activity (start_utc DESC)",
)


def ensure_schema(conn: sqlite3.Connection) -> list:
    """Add the time-zone columns, indexes and settings table if missing.

    Returns the names of the columns it added.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(activity)")}
    added = []
    for column, column_type in ACTIVITY_TZ_COLUMNS:
        if column not in have:
            conn.execute(f'ALTER TABLE activity ADD COLUMN "{column}" {column_type}')
            added.append(column)
    for statement in ACTIVITY_TZ_INDEXES:
        conn.execute(statement)
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{SETTING_TABLE}" '
        '("key" VARCHAR(255) NOT NULL PRIMARY KEY, "value" TEXT NOT NULL)'
    )
    return added


def backfill_legacy_rows(conn: sqlite3.Connection, *, zone_name: str) -> int:
    """Give rows without a zone ZONE_NAME, reading their times as local there.

    This is the pre-zone behaviour exactly: every naive time meant the one
    clock in use. Rows written under a different clock need `migrate` instead.
    """
    rows = conn.execute(
        'SELECT id, start, "end" FROM activity WHERE tz IS NULL'
    ).fetchall()
    for row_id, start, end in rows:
        conn.execute(
            "UPDATE activity SET tz = ?, start_utc = ?, end_utc = ? WHERE id = ?",
            (
                zone_name,
                db_datetime(to_utc(parse_db_datetime(start), zone_name)),
                db_datetime(to_utc(parse_db_datetime(end), zone_name)),
                row_id,
            ),
        )
    return len(rows)


@dataclasses.dataclass(frozen=True)
class MigrationReport:
    rows: int
    shifted: int
    by_zones: Counter
    chain_breaks_before: int
    chain_breaks_after: int
    samples: tuple

    def __str__(self):
        lines = [f"rows to migrate: {self.rows}; local times shifted: {self.shifted}"]
        for (recorded_in, tz), count in sorted(self.by_zones.items()):
            lines.append(f"  recorded in {recorded_in}, stored as {tz}: {count}")
        lines.append(
            "rows not starting when the previous one ended: "
            f"{self.chain_breaks_before} before, {self.chain_breaks_after} after"
        )
        for row in self.samples:
            lines.append(
                f"  #{row.id} {row.name[:30]:30} {row.old_start:%Y-%m-%d %H:%M} -> "
                f"{row.start:%Y-%m-%d %H:%M} {row.tz} (until {row.end:%H:%M})"
            )
        return "\n".join(lines)


def _chain_breaks(pairs) -> int:
    """How many rows (in id order) do not start exactly where the previous ended."""
    breaks = 0
    previous_end = None
    for start, end in pairs:
        if previous_end is not None and start != previous_end:
            breaks += 1
        previous_end = end
    return breaks


def migrate(
    conn: sqlite3.Connection,
    *,
    recorded: ZoneTimeline,
    lived: ZoneTimeline,
    apply: bool = False,
    sample_count: int = 12,
) -> MigrationReport:
    """Store a zone for every row that has none, per RECORDED and LIVED.

    Idempotent: only rows without a zone are touched, so running it twice
    never shifts a row twice. Without APPLY it changes nothing and reports
    what it would do. With APPLY it also sets the current-zone setting to
    LIVED's final zone. The caller commits.
    """
    if apply:
        ensure_schema(conn)
    has_tz = any(row[1] == "tz" for row in conn.execute("PRAGMA table_info(activity)"))
    legacy = "WHERE tz IS NULL " if has_tz else ""
    rows = [
        (row_id, name, parse_db_datetime(start), parse_db_datetime(end))
        for row_id, name, start, end in conn.execute(
            f'SELECT id, name, start, "end" FROM activity {legacy}ORDER BY id'
        )
    ]
    migrated = [migrate_row(*row, recorded=recorded, lived=lived) for row in rows]

    by_zones = Counter((row.recorded_in, row.tz) for row in migrated)
    shifted = [row for row in migrated if row.shifted]
    samples = []
    for index, row in enumerate(migrated):
        boundary = index > 0 and migrated[index - 1].tz != row.tz
        if boundary:
            samples.extend(migrated[max(0, index - 2) : index + 2])
    samples.extend(shifted[:2] + shifted[-2:])
    unique_samples = tuple({row.id: row for row in samples}.values())[:sample_count]

    report = MigrationReport(
        rows=len(migrated),
        shifted=len(shifted),
        by_zones=by_zones,
        chain_breaks_before=_chain_breaks(
            (localize(r.old_start, r.recorded_in), localize(r.old_end, r.recorded_in))
            for r in migrated
        ),
        chain_breaks_after=_chain_breaks((r.start_utc, r.end_utc) for r in migrated),
        samples=unique_samples,
    )
    if not apply:
        return report

    for row in migrated:
        conn.execute(
            'UPDATE activity SET start = ?, "end" = ?, tz = ?, start_utc = ?, '
            "end_utc = ? WHERE id = ?",
            (
                db_datetime(row.start),
                db_datetime(row.end),
                row.tz,
                db_datetime(row.start_utc),
                db_datetime(row.end_utc),
                row.id,
            ),
        )
    conn.execute(
        f'INSERT INTO "{SETTING_TABLE}" ("key", "value") VALUES (?, ?) '
        'ON CONFLICT("key") DO UPDATE SET "value" = excluded."value"',
        (TZ_SETTING, lived.final),
    )
    return report


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 uniborg/timetracker_tz.py")
    commands = parser.add_subparsers(dest="command", required=True)
    m = commands.add_parser(
        "migrate", help="store a time zone for every activity that has none"
    )
    m.add_argument("--db", required=True, type=Path)
    m.add_argument(
        "--recorded-in",
        required=True,
        help="the zone whose clock wrote the legacy naive times",
    )
    m.add_argument(
        "--recorded-in-from-id",
        action="append",
        default=[],
        metavar="ID=ZONE",
        help="rows with this id and above were written under ZONE's clock",
    )
    m.add_argument(
        "--lived-in", required=True, help="the zone the user was in at first"
    )
    m.add_argument(
        "--moved",
        action="append",
        default=[],
        metavar="INSTANT=ZONE",
        help="from INSTANT (ISO 8601 with a UTC offset) on, the user was in ZONE",
    )
    m.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        recorded = ZoneTimeline(
            initial=args.recorded_in,
            changes=tuple(parse_id_change(c) for c in args.recorded_in_from_id),
        )
        lived = ZoneTimeline(
            initial=args.lived_in,
            changes=tuple(parse_instant_change(c) for c in args.moved),
        )
        conn = sqlite3.connect(args.db)
        try:
            with conn:
                report = migrate(conn, recorded=recorded, lived=lived, apply=args.apply)
        finally:
            conn.close()
        print(report)
        print("applied" if args.apply else "dry run: nothing written (pass --apply)")
        return 0
    raise ValueError(f"unknown command: {args.command!r}")


if __name__ == "__main__":
    sys.exit(_main())
