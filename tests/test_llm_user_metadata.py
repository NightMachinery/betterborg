import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from uniborg import llm_db


@contextmanager
def isolated_database(*, create_schema=True):
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "metadata.db"
        test_engine = create_engine(f"sqlite:///{database}")
        if create_schema:
            llm_db.Base.metadata.create_all(test_engine)
        old_session = llm_db.Session
        llm_db.Session = sessionmaker(bind=test_engine)
        try:
            yield test_engine, database
        finally:
            llm_db.Session = old_session
            test_engine.dispose()


class ApiKeyMetadataTests(unittest.TestCase):
    def test_empty_metadata(self):
        with isolated_database():
            self.assertEqual(llm_db.get_api_key_metadata(404), [])

    def test_existing_database_migrates_idempotently_without_touching_secret(self):
        with isolated_database(create_schema=False) as (test_engine, database):
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE user_api_keys ("
                "user_id INTEGER NOT NULL, service VARCHAR NOT NULL, "
                "api_key VARCHAR NOT NULL, PRIMARY KEY (user_id, service))"
            )
            connection.execute(
                "INSERT INTO user_api_keys VALUES (?, ?, ?)",
                (7, "gemini", "still-secret"),
            )
            connection.commit()
            connection.close()

            llm_db._migrate_schema(test_engine)
            llm_db._migrate_schema(test_engine)

            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT api_key, last_set_at FROM user_api_keys"
            ).fetchone()
            columns = connection.execute("PRAGMA table_info(user_api_keys)").fetchall()
            connection.close()
            self.assertEqual(row, ("still-secret", None))
            self.assertEqual([column[1] for column in columns].count("last_set_at"), 1)

    def test_simultaneous_migrations_serialize_schema_check_and_alter(self):
        with isolated_database(create_schema=False) as (test_engine, database):
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE user_api_keys ("
                "user_id INTEGER NOT NULL, service VARCHAR NOT NULL, "
                "api_key VARCHAR NOT NULL, PRIMARY KEY (user_id, service))"
            )
            connection.commit()
            connection.close()

            ready = threading.Barrier(2)

            def migrate(_):
                ready.wait()
                return llm_db._migrate_schema(test_engine)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(migrate, range(2)))

            self.assertEqual(results, [None, None])
            connection = sqlite3.connect(database)
            columns = connection.execute("PRAGMA table_info(user_api_keys)").fetchall()
            connection.close()
            self.assertEqual([column[1] for column in columns].count("last_set_at"), 1)

    def test_setting_same_key_refreshes_utc_timestamp(self):
        with isolated_database():
            llm_db.set_api_key(user_id=8, service="gemini", key="same-secret")
            first = llm_db.get_api_key_metadata(8)[0].last_set_at
            time.sleep(0.002)
            llm_db.set_api_key(user_id=8, service="gemini", key="same-secret")
            second = llm_db.get_api_key_metadata(8)[0].last_set_at

            self.assertEqual(llm_db.get_api_key(8), "same-secret")
            self.assertEqual(first.tzinfo, timezone.utc)
            self.assertEqual(second.tzinfo, timezone.utc)
            self.assertGreater(second, first)

    def test_metadata_query_does_not_select_secret_column(self):
        with isolated_database() as (test_engine, _):
            llm_db.set_api_key(user_id=9, service="gemini", key="hidden")
            statements = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            event.listen(test_engine, "before_cursor_execute", capture)
            try:
                metadata = llm_db.get_api_key_metadata(9)
            finally:
                event.remove(test_engine, "before_cursor_execute", capture)

            self.assertEqual([item.service for item in metadata], ["gemini"])
            select_sql = " ".join(statements).lower()
            self.assertNotIn("user_api_keys.api_key", select_sql)
            self.assertNotIn("hidden", select_sql)

    def test_multiple_services_are_ordered_and_users_are_isolated(self):
        with isolated_database():
            llm_db.set_api_key(user_id=10, service="openrouter", key="one")
            llm_db.set_api_key(user_id=10, service="gemini", key="two")
            llm_db.set_api_key(user_id=11, service="deepseek", key="three")

            self.assertEqual(
                [item.service for item in llm_db.get_api_key_metadata(10)],
                ["gemini", "openrouter"],
            )
            self.assertEqual(
                [item.service for item in llm_db.get_api_key_metadata(11)],
                ["deepseek"],
            )


class UserProfileTests(unittest.TestCase):
    def test_missing_profile_and_identity_refresh_leave_contact_null(self):
        with isolated_database():
            self.assertIsNone(llm_db.get_user_profile(1, 404))
            profile = llm_db.record_user_profile(1, 42, "Ada", None, "ada")
            self.assertIsNone(profile.private_contact_at)
            refreshed = llm_db.record_user_profile(1, 42, "Ada", "Lovelace", "ada")
            self.assertIsNone(refreshed.private_contact_at)

    def test_profiles_are_isolated_by_bot(self):
        with isolated_database():
            llm_db.record_user_profile(1, 42, "Ada", None, "ada")
            llm_db.record_user_profile(2, 42, "Grace", "Hopper", "grace")
            self.assertEqual(llm_db.get_user_profile(1, 42).first_name, "Ada")
            self.assertEqual(llm_db.get_user_profile(2, 42).first_name, "Grace")

    def test_earliest_private_contact_wins_and_contact_only_keeps_identity(self):
        with isolated_database():
            middle = datetime(2025, 1, 2, tzinfo=timezone.utc)
            llm_db.record_user_profile(1, 42, "Ada", "Lovelace", "ada", middle)
            llm_db.record_user_profile(
                1,
                42,
                None,
                None,
                None,
                middle + timedelta(days=1),
                refresh_identity=False,
            )
            profile = llm_db.record_user_profile(
                1,
                42,
                None,
                None,
                None,
                middle - timedelta(days=1),
                refresh_identity=False,
            )
            self.assertEqual(profile.first_name, "Ada")
            self.assertEqual(profile.username, "ada")
            self.assertEqual(
                profile.private_contact_at,
                middle - timedelta(days=1),
            )

    def test_identity_refresh_clears_removed_username(self):
        with isolated_database():
            llm_db.record_user_profile(1, 42, "Ada", "Lovelace", "ada")
            profile = llm_db.record_user_profile(
                1, 42, "Ada", "Lovelace", None, refresh_identity=True
            )
            self.assertIsNone(profile.username)

    def test_concurrent_contact_updates_keep_earliest(self):
        with isolated_database():
            later = datetime(2025, 1, 3, tzinfo=timezone.utc)
            earlier = later - timedelta(days=2)

            def record(contact_at):
                return llm_db.record_user_profile(
                    1,
                    42,
                    None,
                    None,
                    None,
                    contact_at,
                    refresh_identity=False,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(record, [later, earlier]))

            self.assertEqual(
                llm_db.get_user_profile(1, 42).private_contact_at,
                earlier,
            )


if __name__ == "__main__":
    unittest.main()
