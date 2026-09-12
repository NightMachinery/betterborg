import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from uniborg import llm_chat_config


class Event:
    def __init__(self, sender_id):
        self.sender_id = sender_id


BASE = """// retained header
{
  unrelated: {theme: 'dark'}, // retained setting
  codex_allowed_users: [7, "MAGIC_ADMINS"],
  codex_imagegen_allowed_users: [7, "MAGIC_ADMINS"],
  codex_users: [
    {id: 7, name: 'Seven', codex_enabled: false, imagegen_enabled: true}, // retained name
  ],
}
"""


class RosterTests(unittest.TestCase):
    def test_roster_is_authoritative_over_legacy_numeric_grants(self):
        config = llm_chat_config.parse_config(BASE)
        with mock.patch("uniborg.util.isAdmin", new=mock.AsyncMock(return_value=False)):
            self.assertFalse(asyncio.run(llm_chat_config.can_use_codex(Event(7), config)))
            self.assertFalse(asyncio.run(llm_chat_config.can_use_codex_imagegen(Event(7), config)))

    def test_sentinel_is_independent_of_disabled_manual_flag(self):
        config = llm_chat_config.parse_config(BASE)
        with mock.patch("uniborg.util.isAdmin", new=mock.AsyncMock(return_value=True)):
            self.assertTrue(asyncio.run(llm_chat_config.can_use_codex(Event(7), config)))
            self.assertTrue(asyncio.run(llm_chat_config.can_use_codex_imagegen(Event(7), config)))

    def test_complete_manual_and_sentinel_authorization_matrix(self):
        for manual_codex in (False, True):
            for manual_image in (False, True):
                for is_admin in (False, True):
                    for codex_sentinel in (False, True):
                        for image_sentinel in (False, True):
                            codex_policy = ("MAGIC_ADMINS",) if codex_sentinel else ()
                            image_policy = ("MAGIC_ADMINS",) if image_sentinel else ()
                            config = llm_chat_config.LLMChatConfig(
                                codex_policy,
                                image_policy,
                                codex_users=(llm_chat_config.CodexUser(
                                    7, None, manual_codex, manual_image
                                ),),
                            )
                            expected_codex = manual_codex or (is_admin and codex_sentinel)
                            expected_image = expected_codex and (
                                manual_image or (is_admin and image_sentinel)
                            )
                            with self.subTest(
                                manual_codex=manual_codex,
                                manual_image=manual_image,
                                is_admin=is_admin,
                                codex_sentinel=codex_sentinel,
                                image_sentinel=image_sentinel,
                            ), mock.patch(
                                "uniborg.util.isAdmin",
                                new=mock.AsyncMock(return_value=is_admin),
                            ):
                                event = Event(7)
                                self.assertEqual(
                                    asyncio.run(llm_chat_config.can_use_codex(event, config)),
                                    expected_codex,
                                )
                                self.assertEqual(
                                    asyncio.run(llm_chat_config.can_use_codex_imagegen(event, config)),
                                    expected_image,
                                )

    def test_configured_users_merges_legacy_and_retains_disabled(self):
        config = llm_chat_config.parse_config(
            """{codex_allowed_users:[1,2], codex_imagegen_allowed_users:[2,3],
            codex_users:[{id:1,name:'One',codex_enabled:false,imagegen_enabled:false}]}"""
        )
        self.assertEqual(
            llm_chat_config.configured_users(config),
            (
                llm_chat_config.CodexUser(1, "One", False, False),
                llm_chat_config.CodexUser(2, None, True, True),
                llm_chat_config.CodexUser(3, None, False, True),
            ),
        )

    def test_invalid_rosters_are_rejected(self):
        values = (
            "true",
            "[{id:1,codex_enabled:true,imagegen_enabled:false},{id:1,codex_enabled:false,imagegen_enabled:false}]",
            "[{id:true,codex_enabled:true,imagegen_enabled:false}]",
            "[{id:1,codex_enabled:1,imagegen_enabled:false}]",
            "[{id:1,codex_enabled:true}]",
            "[{id:1,name:4,codex_enabled:true,imagegen_enabled:false}]",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                llm_chat_config.parse_config(
                    "{codex_allowed_users:[],codex_imagegen_allowed_users:[],codex_users:" + value + "}"
                )

    def test_duplicate_keys_are_rejected_at_root_and_in_records(self):
        duplicate_sources = (
            "{codex_allowed_users:[],codex_allowed_users:[1],codex_imagegen_allowed_users:[]}",
            "{codex_allowed_users:[],codex_imagegen_allowed_users:[],codex_users:[{id:1,id:2,codex_enabled:true,imagegen_enabled:false}]}",
        )
        for source in duplicate_sources:
            with self.subTest(source=source), self.assertRaises(ValueError):
                llm_chat_config.parse_config(source)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "config.json5"
        self.path.write_text(BASE)
        self.path.chmod(0o640)
        self.path_patch = mock.patch.object(llm_chat_config, "config_path", return_value=self.path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.directory.cleanup()

    def test_existing_record_update_preserves_source_and_mode(self):
        result = llm_chat_config.update_user_access(7, "codex_enabled", True)
        text = self.path.read_text()
        self.assertIn("// retained header", text)
        self.assertIn("unrelated: {theme: 'dark'}, // retained setting", text)
        self.assertIn("name: 'Seven'", text)
        self.assertIn("codex_enabled: true", text)
        self.assertTrue(result.codex_users[0].codex_enabled)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_legacy_record_is_inserted_without_losing_other_flag(self):
        self.path.write_text("{codex_allowed_users:[8],codex_imagegen_allowed_users:[8]}")
        result = llm_chat_config.update_user_access(8, "codex_enabled", False)
        self.assertEqual(result.codex_users, (llm_chat_config.CodexUser(8, None, False, True),))
        self.assertIn("codex_users", self.path.read_text())

    def test_idempotent_update_does_not_replace_file(self):
        inode = self.path.stat().st_ino
        result = llm_chat_config.update_user_access(7, "codex_enabled", False)
        self.assertFalse(result.codex_users[0].codex_enabled)
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_unknown_user_and_invalid_current_config_leave_source_untouched(self):
        original = self.path.read_text()
        with self.assertRaises(llm_chat_config.ConfigUpdateError):
            llm_chat_config.update_user_access(999, "codex_enabled", True)
        self.assertEqual(self.path.read_text(), original)
        self.path.write_text("{broken:")
        broken = self.path.read_text()
        with self.assertRaises(llm_chat_config.ConfigUpdateError):
            llm_chat_config.update_user_access(7, "codex_enabled", True)
        self.assertEqual(self.path.read_text(), broken)

    def test_removed_target_and_atomic_replace_failure_are_clear(self):
        self.path.unlink()
        with self.assertRaises(llm_chat_config.ConfigUpdateError):
            llm_chat_config.update_user_access(7, "codex_enabled", True)
        self.path.write_text(BASE)
        original = self.path.read_text()
        with mock.patch.object(os, "replace", side_effect=OSError("no replace")):
            with self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.update_user_access(7, "codex_enabled", True)
        self.assertEqual(self.path.read_text(), original)

    def test_detects_change_during_update(self):
        original_read = Path.read_text
        calls = 0

        def changing_read(path, *args, **kwargs):
            nonlocal calls
            value = original_read(path, *args, **kwargs)
            if path.name == self.path.name:
                calls += 1
                if calls == 1:
                    path.write_text(value + "// concurrent\n")
            return value

        with mock.patch.object(Path, "read_text", changing_read):
            with self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.update_user_access(7, "codex_enabled", True)
        self.assertIn("// concurrent", self.path.read_text())

    def test_generated_invalid_source_is_validated_before_replace(self):
        original = self.path.read_text()
        with mock.patch.object(llm_chat_config, "_updated_source", return_value="{broken:"):
            with self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.update_user_access(7, "codex_enabled", True)
        self.assertEqual(self.path.read_text(), original)

    def test_change_while_temp_is_written_is_not_overwritten(self):
        original_fsync = os.fsync

        def edit_then_fsync(fd):
            self.path.write_text(self.path.read_text() + "// late edit\n")
            return original_fsync(fd)

        with mock.patch.object(os, "fsync", side_effect=edit_then_fsync):
            with self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.update_user_access(7, "codex_enabled", True)
        self.assertIn("// late edit", self.path.read_text())

    def test_symlink_is_preserved_and_target_is_updated(self):
        target = Path(self.directory.name) / "actual.json5"
        target.write_text(BASE)
        link = Path(self.directory.name) / "linked.json5"
        link.symlink_to(target)
        with mock.patch.object(llm_chat_config, "config_path", return_value=link):
            result = llm_chat_config.update_user_access(7, "codex_enabled", True)
        self.assertTrue(link.is_symlink())
        self.assertTrue(result.codex_users[0].codex_enabled)
        self.assertIn("codex_enabled: true", target.read_text())

    def test_symlink_retarget_during_update_is_refused(self):
        first = Path(self.directory.name) / "first.json5"
        second = Path(self.directory.name) / "second.json5"
        first.write_text(BASE)
        second.write_text(BASE)
        link = Path(self.directory.name) / "moving.json5"
        link.symlink_to(first)
        original_fsync = os.fsync

        def retarget_then_fsync(fd):
            link.unlink()
            link.symlink_to(second)
            return original_fsync(fd)

        with mock.patch.object(llm_chat_config, "config_path", return_value=link), mock.patch.object(
            os, "fsync", side_effect=retarget_then_fsync
        ):
            with self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.update_user_access(7, "codex_enabled", True)
        self.assertIn("codex_enabled: false", first.read_text())
        self.assertIn("codex_enabled: false", second.read_text())

    def test_escaped_unquoted_roster_key_is_edited_without_duplicate(self):
        self.path.write_text(BASE.replace("codex_users", r"codex\u005fusers"))
        result = llm_chat_config.update_user_access(7, "codex_enabled", True)
        text = self.path.read_text()
        self.assertTrue(result.codex_users[0].codex_enabled)
        self.assertEqual(text.count(r"codex\u005fusers"), 1)
        self.assertNotIn("\n  codex_users:", text)


class AddUserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "config.json5"
        self.path.write_text(BASE)
        self.path.chmod(0o640)
        self.path_patch = mock.patch.object(llm_chat_config, "config_path", return_value=self.path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.directory.cleanup()

    def test_adds_disabled_named_user_and_preserves_source(self):
        result = llm_chat_config.add_user(12, name='Twelve "quoted"')
        self.assertEqual(
            result.codex_users[-1],
            llm_chat_config.CodexUser(12, 'Twelve "quoted"', False, False),
        )
        text = self.path.read_text()
        self.assertIn("// retained header", text)
        self.assertIn("unrelated: {theme: 'dark'}, // retained setting", text)
        self.assertIn('name: "Twelve \\"quoted\\""', text)
        self.assertEqual(result.codex_allowed_users, (7, "MAGIC_ADMINS"))
        self.assertEqual(result.codex_imagegen_allowed_users, (7, "MAGIC_ADMINS"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_adds_roster_array_when_missing(self):
        self.path.write_text("""// keep me
{codex_allowed_users:[], codex_imagegen_allowed_users:[]} // tail
""")
        result = llm_chat_config.add_user(13)
        self.assertEqual(result.codex_users, (llm_chat_config.CodexUser(13, None, False, False),))
        text = self.path.read_text()
        self.assertIn("// keep me", text)
        self.assertIn("// tail", text)
        self.assertIn("codex_users: [{id: 13, codex_enabled: false, imagegen_enabled: false}]", text)

    def test_existing_roster_and_legacy_users_are_unchanged(self):
        for user_id in (7, 19):
            with self.subTest(user_id=user_id):
                self.path.write_text(BASE.replace(
                    'codex_allowed_users: [7, "MAGIC_ADMINS"]',
                    'codex_allowed_users: [7, 19, "MAGIC_ADMINS"]',
                ))
                before = self.path.read_text()
                inode = self.path.stat().st_ino
                result = llm_chat_config.add_user(user_id, name="Replacement")
                self.assertEqual(self.path.read_text(), before)
                self.assertEqual(self.path.stat().st_ino, inode)
                configured = next(user for user in llm_chat_config.configured_users(result) if user.id == user_id)
                if user_id == 7:
                    self.assertEqual(configured, llm_chat_config.CodexUser(7, "Seven", False, True))
                else:
                    self.assertEqual(configured, llm_chat_config.CodexUser(19, None, True, False))

    def test_rejects_invalid_ids_names_and_current_config(self):
        original = self.path.read_text()
        for user_id in (True, 1.5, "1", 0, -1, 2**63):
            with self.subTest(user_id=user_id), self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.add_user(user_id)
            self.assertEqual(self.path.read_text(), original)
        for name in (False, 3, []):
            with self.subTest(name=name), self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.add_user(21, name=name)
            self.assertEqual(self.path.read_text(), original)
        self.path.write_text("{broken:")
        broken = self.path.read_text()
        with self.assertRaises(llm_chat_config.ConfigUpdateError):
            llm_chat_config.add_user(21)
        self.assertEqual(self.path.read_text(), broken)

    def test_locked_concurrent_additions_both_survive(self):
        barrier = threading.Barrier(3)
        results = []

        def add(user_id):
            barrier.wait()
            results.append(llm_chat_config.add_user(user_id))

        threads = [threading.Thread(target=add, args=(user_id,)) for user_id in (31, 32)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        config = llm_chat_config.parse_config(self.path.read_text())
        self.assertEqual({user.id for user in config.codex_users}, {7, 31, 32})

    def test_uncooperative_concurrent_edit_is_preserved(self):
        original_fsync = os.fsync

        def edit_then_fsync(fd):
            self.path.write_text(self.path.read_text() + "// uncooperative edit\n")
            return original_fsync(fd)

        with mock.patch.object(os, "fsync", side_effect=edit_then_fsync):
            with self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.add_user(41)
        text = self.path.read_text()
        self.assertIn("// uncooperative edit", text)
        self.assertNotIn("id: 41", text)

    def test_generated_semantic_change_is_rejected_before_replace(self):
        original = self.path.read_text()
        changed = BASE.replace(
            "  ],",
            "    {id: 42, codex_enabled: true, imagegen_enabled: false},\n  ],",
        )
        with mock.patch.object(llm_chat_config, "_added_user_source", return_value=changed):
            with self.assertRaises(llm_chat_config.ConfigUpdateError):
                llm_chat_config.add_user(42)
        self.assertEqual(self.path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
