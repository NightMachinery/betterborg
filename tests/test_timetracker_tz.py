import datetime as dt
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    #: From the file, so the `uniborg` package __init__ (which starts shell
    #: servers through uniborg.util) never runs.
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", _ROOT / "uniborg" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tz = _load("timetracker_tz")

UTC = dt.timezone.utc


def _db(rows):
    """An in-memory legacy database holding ROWS of (id, name, start, end)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        'CREATE TABLE "activity" ("id" INTEGER NOT NULL PRIMARY KEY, '
        '"name" VARCHAR(255) NOT NULL, "start" DATETIME NOT NULL, '
        '"end" DATETIME NOT NULL)'
    )
    conn.executemany("INSERT INTO activity VALUES (?, ?, ?, ?)", rows)
    #: Committed, or `Connection.backup` waits forever on the open transaction.
    conn.commit()
    return conn


class ZoneHelpersTest(unittest.TestCase):
    def test_canonical_zone_name_is_case_insensitive_and_canonical(self):
        self.assertEqual(tz.canonical_zone_name("America/New_York"), "America/New_York")
        self.assertEqual(tz.canonical_zone_name("america/new_york"), "America/New_York")
        with self.assertRaises(ValueError):
            tz.canonical_zone_name("Mars/Olympus_Mons")

    def test_host_zone_name_prefers_tz_then_localtime(self):
        self.assertEqual(tz.host_zone_name(env={"TZ": ":Asia/Tokyo"}), "Asia/Tokyo")
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "localtime"
            os.symlink("/usr/share/zoneinfo/America/New_York", link)
            self.assertEqual(
                tz.host_zone_name(env={}, localtime=link), "America/New_York"
            )
            self.assertIsNone(tz.host_zone_name(env={}, localtime=Path(d) / "missing"))

    def test_default_zone_name_uses_env_and_rejects_bad_names(self):
        self.assertEqual(
            tz.default_zone_name(env={tz.TZ_ENV: "Asia/Tokyo"}), "Asia/Tokyo"
        )
        with self.assertRaises(ValueError):
            tz.default_zone_name(env={tz.TZ_ENV: "Nowhere/Land"})

    def test_elapsed_is_real_time_across_fall_back(self):
        #: Europe/Lisbon repeats 01:00-02:00 on 2025-10-26; 00:30 to 02:30 is 3 h.
        self.assertEqual(
            tz.elapsed(
                dt.datetime(2025, 10, 26, 0, 30),
                dt.datetime(2025, 10, 26, 2, 30),
                "Europe/Lisbon",
            ),
            dt.timedelta(hours=3),
        )

    def test_convert_and_local_now(self):
        local = dt.datetime(2025, 6, 11, 4, 0)
        self.assertEqual(
            tz.convert(local, from_zone="America/Chicago", to_zone="Europe/Lisbon"),
            dt.datetime(2025, 6, 11, 10, 0),
        )
        self.assertIs(tz.convert(local, from_zone="UTC", to_zone="UTC"), local)
        clock = lambda: dt.datetime(2025, 6, 11, 8, 0, tzinfo=UTC)
        self.assertEqual(
            tz.local_now("Europe/Lisbon", clock=clock), dt.datetime(2025, 6, 11, 9, 0)
        )

    def test_message_stamp_is_the_send_time(self):
        #: Handled an hour late, a message still counts from when it was sent.
        sent = dt.datetime(2025, 6, 11, 9, 0, tzinfo=UTC)
        self.assertEqual(
            tz.message_stamp(
                sent,
                latest_end=dt.datetime(2025, 6, 11, 8, 30),
                now=dt.datetime(2025, 6, 11, 10, 0),
            ),
            dt.datetime(2025, 6, 11, 9, 0),
        )
        self.assertEqual(
            tz.message_stamp(sent, latest_end=None, now=dt.datetime(2025, 6, 11, 10)),
            dt.datetime(2025, 6, 11, 9, 0),
        )

    def test_message_stamp_stays_after_the_latest_end(self):
        #: Sent in the second the previous activity ended in, or while the bot
        #: was still handling it.
        sent = dt.datetime(2025, 6, 11, 9, 0, tzinfo=UTC)
        now = dt.datetime(2025, 6, 11, 9, 0, 5)
        for latest_end in (
            dt.datetime(2025, 6, 11, 9, 0),
            dt.datetime(2025, 6, 11, 9, 0, 0, 400000),
        ):
            self.assertEqual(
                tz.message_stamp(sent, latest_end=latest_end, now=now),
                latest_end + tz.STAMP_STEP,
            )

    def test_message_stamp_is_never_in_the_future(self):
        sent = dt.datetime(2025, 6, 11, 9, 0, 2, tzinfo=UTC)
        now = dt.datetime(2025, 6, 11, 9, 0, 1)
        self.assertEqual(tz.message_stamp(sent, latest_end=None, now=now), now)
        with self.assertRaises(ValueError):
            tz.message_stamp(dt.datetime(2025, 6, 11), latest_end=None, now=now)

    def test_naive_and_aware_inputs_are_checked(self):
        with self.assertRaises(ValueError):
            tz.localize(dt.datetime(2025, 1, 1, tzinfo=UTC), "UTC")
        with self.assertRaises(ValueError):
            tz.to_local(dt.datetime(2025, 1, 1), "UTC")


class TimelineTest(unittest.TestCase):
    def test_zone_at_and_final(self):
        moved = dt.datetime(2025, 6, 11, 7, 0, tzinfo=UTC)
        timeline = tz.ZoneTimeline(
            initial="America/Chicago",
            changes=(tz.ZoneChange(at=moved, zone="Europe/Lisbon"),),
        )
        self.assertEqual(
            timeline.zone_at(moved - dt.timedelta(seconds=1)), "America/Chicago"
        )
        self.assertEqual(timeline.zone_at(moved), "Europe/Lisbon")
        self.assertEqual(timeline.final, "Europe/Lisbon")

    def test_changes_must_be_ordered_and_zones_valid(self):
        with self.assertRaises(ValueError):
            tz.ZoneTimeline(
                initial="UTC",
                changes=(
                    tz.ZoneChange(at=5, zone="UTC"),
                    tz.ZoneChange(at=2, zone="UTC"),
                ),
            )
        with self.assertRaises(ValueError):
            tz.ZoneTimeline(initial="Nowhere/Land")

    def test_parsers(self):
        change = tz.parse_instant_change("2025-06-11T02:00:00-05:00=Europe/Lisbon")
        self.assertEqual(change.zone, "Europe/Lisbon")
        self.assertEqual(change.at.utcoffset(), dt.timedelta(hours=-5))
        with self.assertRaises(ValueError):
            tz.parse_instant_change("2025-06-11T02:00:00=Europe/Lisbon")
        self.assertEqual(tz.parse_id_change("12=UTC"), tz.ZoneChange(at=12, zone="UTC"))
        with self.assertRaises(ValueError):
            tz.parse_id_change("12")


class MigrationTest(unittest.TestCase):
    #: Recorded on a Chicago clock throughout; the user moved to Lisbon at the
    #: start of row 3; rows from id 5 on were written by a Lisbon clock.
    ROWS = [
        (1, "a", "2025-06-10 10:00:00.000000", "2025-06-10 12:00:00.000000"),
        (2, "commute", "2025-06-10 12:00:00.000000", "2025-06-11 02:00:00.000000"),
        (3, "b", "2025-06-11 02:00:00.000000", "2025-06-11 03:30:00.000000"),
        (4, "c", "2025-06-11 03:30:00.000000", "2025-06-11 04:00:00"),
        (5, "d", "2025-06-11 10:00:00.000000", "2025-06-11 10:30:00.000000"),
    ]
    MOVED = "2025-06-11T02:00:00-05:00=Europe/Lisbon"
    RECORDED = tz.ZoneTimeline(
        initial="America/Chicago",
        changes=(tz.ZoneChange(at=5, zone="Europe/Lisbon"),),
    )
    LIVED = tz.ZoneTimeline(
        initial="America/Chicago", changes=(tz.parse_instant_change(MOVED),)
    )

    def _migrate(self, conn, *, apply):
        with conn:
            return tz.migrate(
                conn, recorded=self.RECORDED, lived=self.LIVED, apply=apply
            )

    def test_dry_run_writes_nothing(self):
        conn = _db(self.ROWS)
        report = self._migrate(conn, apply=False)
        self.assertEqual(report.rows, 5)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(activity)")]
        self.assertEqual(columns, ["id", "name", "start", "end"])
        self.assertEqual(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = ?", (tz.SETTING_TABLE,)
            ).fetchall(),
            [],
        )

    def test_apply_reinterprets_rows_and_is_idempotent(self):
        conn = _db(self.ROWS)
        report = self._migrate(conn, apply=True)
        self.assertEqual(report.rows, 5)
        self.assertEqual(report.shifted, 2)  #: rows 3 and 4; row 5 was already Lisbon
        self.assertEqual(report.chain_breaks_before, report.chain_breaks_after)

        got = {
            row[0]: row[1:]
            for row in conn.execute(
                'SELECT id, start, "end", tz, start_utc, end_utc FROM activity'
            )
        }
        #: Before the move: unchanged local times, zone recorded.
        self.assertEqual(
            got[1],
            (
                "2025-06-10 10:00:00.000000",
                "2025-06-10 12:00:00.000000",
                "America/Chicago",
                "2025-06-10 15:00:00.000000",
                "2025-06-10 17:00:00.000000",
            ),
        )
        #: The commute started before the move, so it keeps the departure zone.
        self.assertEqual(got[2][2], "America/Chicago")
        #: After the move: Chicago-clock times become Lisbon wall-clock times.
        self.assertEqual(
            got[3][:3],
            (
                "2025-06-11 08:00:00.000000",
                "2025-06-11 09:30:00.000000",
                "Europe/Lisbon",
            ),
        )
        self.assertEqual(got[4][1], "2025-06-11 10:00:00.000000")
        #: Written by a Lisbon clock: unchanged.
        self.assertEqual(
            got[5][:3],
            (
                "2025-06-11 10:00:00.000000",
                "2025-06-11 10:30:00.000000",
                "Europe/Lisbon",
            ),
        )
        #: Instants line up across the zone change.
        self.assertEqual(got[2][4], got[3][3])
        self.assertEqual(
            conn.execute(
                f'SELECT value FROM "{tz.SETTING_TABLE}" WHERE key = ?',
                (tz.TZ_SETTING,),
            ).fetchone(),
            ("Europe/Lisbon",),
        )

        again = self._migrate(conn, apply=True)
        self.assertEqual(again.rows, 0)
        self.assertEqual(
            conn.execute("SELECT start FROM activity WHERE id = 3").fetchone(),
            ("2025-06-11 08:00:00.000000",),
        )

    def test_backfill_legacy_rows_keeps_local_times(self):
        conn = _db(self.ROWS[:1])
        tz.ensure_schema(conn)
        self.assertEqual(tz.backfill_legacy_rows(conn, zone_name="America/Chicago"), 1)
        self.assertEqual(
            conn.execute("SELECT start, tz, start_utc FROM activity").fetchone(),
            (
                "2025-06-10 10:00:00.000000",
                "America/Chicago",
                "2025-06-10 15:00:00.000000",
            ),
        )
        self.assertEqual(tz.backfill_legacy_rows(conn, zone_name="America/Chicago"), 0)

    def test_ensure_schema_is_idempotent(self):
        conn = _db([])
        self.assertEqual(tz.ensure_schema(conn), ["tz", "start_utc", "end_utc"])
        self.assertEqual(tz.ensure_schema(conn), [])

    def test_cli_dry_run_and_apply(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tt.db"
            conn = _db(self.ROWS)
            disk = sqlite3.connect(path)
            conn.backup(disk)
            disk.close()
            args = [
                "migrate",
                "--db",
                str(path),
                "--recorded-in",
                "America/Chicago",
                "--recorded-in-from-id",
                "5=Europe/Lisbon",
                "--lived-in",
                "America/Chicago",
                "--moved",
                self.MOVED,
            ]
            self.assertEqual(tz._main(args), 0)
            check = sqlite3.connect(path)
            self.assertNotIn(
                "tz", [r[1] for r in check.execute("PRAGMA table_info(activity)")]
            )
            check.close()
            self.assertEqual(tz._main(args + ["--apply"]), 0)
            check = sqlite3.connect(path)
            self.assertEqual(
                check.execute(
                    "SELECT count(*) FROM activity WHERE tz IS NULL"
                ).fetchone(),
                (0,),
            )
            check.close()


if __name__ == "__main__":
    unittest.main()
