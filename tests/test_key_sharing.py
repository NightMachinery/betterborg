"""Shared API keys, key order, the admin share, and key-failure classification.

The tests run on the temporary database conftest points `borg_llm_db_path` at.
"""

import itertools
import unittest

from uniborg import key_sharing as ks
from uniborg import llm_db
from uniborg.llm_util import RateLimitException

_ids = itertools.count(880_000_000)


def _user() -> int:
    return next(_ids)


def _set_key(user_id: int, service: str, key: str) -> None:
    llm_db.set_api_key(user_id=user_id, service=service, key=key)


def _tokens(chain):
    return [credential.token for credential in chain]


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.me, self.alice, self.bob = _user(), _user(), _user()

    def chain(self, service="openai", *, codex_access=False):
        return ks.chain(self.me, service, codex_access=codex_access)

    def test_own_key_first_then_shares_oldest_first(self):
        _set_key(self.me, "openai", "sk-me")
        _set_key(self.alice, "openai", "sk-alice")
        _set_key(self.bob, "openai", "sk-bob")
        ks.share_key(owner_id=self.alice, recipient_id=self.me, service="openai")
        ks.share_key(owner_id=self.bob, recipient_id=self.me, service="openai")

        chain = self.chain()
        self.assertEqual([c.api_key for c in chain], ["sk-me", "sk-alice", "sk-bob"])
        self.assertEqual(chain[1].kind, ks.SHARE)
        self.assertEqual(chain[1].owner_id, self.alice)

    def test_a_share_follows_the_owners_current_key_and_ends_on_revoke(self):
        _set_key(self.alice, "gemini", "AQ.old")
        ks.share_key(owner_id=self.alice, recipient_id=self.me, service="gemini")
        _set_key(self.alice, "gemini", "AQ.new")
        self.assertEqual([c.api_key for c in self.chain("gemini")], ["AQ.new"])

        self.assertTrue(
            ks.revoke_share(owner_id=self.alice, recipient_id=self.me, service="gemini")
        )
        self.assertEqual(self.chain("gemini"), [])

    def test_an_owner_without_a_key_is_listed_but_not_tried(self):
        ks.share_key(owner_id=self.alice, recipient_id=self.me, service="openai")
        self.assertEqual(self.chain(), [])
        found = ks.sources(self.me, "openai", codex_access=False)
        self.assertEqual([s.token for s in found], ["own", f"share:{self.alice}"])
        self.assertEqual([s.available for s in found], [False, False])

    def test_sharing_with_yourself_is_refused_and_duplicates_are_noops(self):
        with self.assertRaises(ValueError):
            ks.share_key(owner_id=self.me, recipient_id=self.me, service="openai")
        self.assertTrue(
            ks.share_key(owner_id=self.alice, recipient_id=self.me, service="openai")
        )
        self.assertFalse(
            ks.share_key(owner_id=self.alice, recipient_id=self.me, service="openai")
        )

    def test_reordering_and_disabling(self):
        _set_key(self.me, "openai", "sk-me")
        _set_key(self.alice, "openai", "sk-alice")
        ks.share_key(owner_id=self.alice, recipient_id=self.me, service="openai")
        current = [s.token for s in ks.sources(self.me, "openai", codex_access=False)]

        ks.move_source(
            user_id=self.me,
            service="openai",
            token=f"share:{self.alice}",
            delta=-1,
            current=current,
        )
        self.assertEqual(_tokens(self.chain()), [f"share:{self.alice}", "own"])

        self.assertFalse(
            ks.toggle_source(user_id=self.me, service="openai", token="own")
        )
        self.assertEqual(_tokens(self.chain()), [f"share:{self.alice}"])
        found = ks.sources(self.me, "openai", codex_access=False)
        self.assertFalse(next(s for s in found if s.token == "own").enabled)

        self.assertTrue(
            ks.toggle_source(user_id=self.me, service="openai", token="own")
        )
        self.assertEqual(len(self.chain()), 2)

    def test_a_new_share_goes_after_the_saved_order(self):
        _set_key(self.me, "openai", "sk-me")
        _set_key(self.alice, "openai", "sk-alice")
        _set_key(self.bob, "openai", "sk-bob")
        ks.share_key(owner_id=self.alice, recipient_id=self.me, service="openai")
        current = [s.token for s in ks.sources(self.me, "openai", codex_access=False)]
        ks.move_source(
            user_id=self.me, service="openai", token="own", delta=1, current=current
        )
        ks.share_key(owner_id=self.bob, recipient_id=self.me, service="openai")
        self.assertEqual(
            _tokens(self.chain()), [f"share:{self.alice}", "own", f"share:{self.bob}"]
        )

    def test_the_same_key_is_tried_once(self):
        _set_key(self.me, "openai", "sk-same")
        _set_key(self.alice, "openai", "sk-same")
        ks.share_key(owner_id=self.alice, recipient_id=self.me, service="openai")
        self.assertEqual(_tokens(self.chain()), ["own"])

    def test_the_own_key_lookup_is_injectable(self):
        chain = ks.chain(
            self.me,
            "gemini",
            codex_access=False,
            own_key=lambda user_id, service: "AQ.rotated",
        )
        self.assertEqual([c.api_key for c in chain], ["AQ.rotated"])


class AdminShareTests(unittest.TestCase):
    def setUp(self):
        self.admin, self.user = _user(), _user()
        _set_key(self.admin, "openai", "sk-admin")
        ks.set_admin_share_policy(service="openai", owner_id=self.admin, enabled=True)

    def tearDown(self):
        ks.set_admin_share_policy(service="openai", owner_id=self.admin, enabled=False)

    def chain(self, *, codex_access):
        return ks.chain(self.user, "openai", codex_access=codex_access)

    def test_follows_codex_access_by_default(self):
        self.assertEqual([c.kind for c in self.chain(codex_access=True)], [ks.ADMIN])
        self.assertEqual(self.chain(codex_access=False), [])

    def test_per_user_overrides(self):
        ks.set_admin_share_override(service="openai", user_id=self.user, mode=ks.OFF)
        self.assertEqual(self.chain(codex_access=True), [])
        ks.set_admin_share_override(service="openai", user_id=self.user, mode=ks.ON)
        self.assertEqual(len(self.chain(codex_access=False)), 1)
        ks.set_admin_share_override(service="openai", user_id=self.user, mode=ks.FOLLOW)
        self.assertEqual(
            ks.admin_share_override(service="openai", user_id=self.user), ks.FOLLOW
        )

    def test_disabled_policy_shares_nothing(self):
        ks.set_admin_share_policy(service="openai", owner_id=self.admin, enabled=False)
        self.assertEqual(self.chain(codex_access=True), [])

    def test_the_admin_is_not_shared_with_themselves(self):
        chain = ks.chain(self.admin, "openai", codex_access=True)
        self.assertEqual(_tokens(chain), ["own"])

    def test_only_openai_is_covered(self):
        with self.assertRaises(ValueError):
            ks.set_admin_share_policy(
                service="gemini", owner_id=self.admin, enabled=True
            )


class ClassifyTests(unittest.TestCase):
    def error(self, name, *, status=None, text="boom"):
        cls = type(name, (Exception,), {})
        exc = cls(text)
        exc.status_code = status
        return exc

    def test_auth_credit_and_rate_limit(self):
        self.assertEqual(
            ks.classify_key_failure(self.error("AuthenticationError", status=401)),
            ks.KeyFailure.AUTH,
        )
        self.assertEqual(
            ks.classify_key_failure(self.error("APIError", status=402)),
            ks.KeyFailure.CREDIT,
        )
        self.assertEqual(
            ks.classify_key_failure(
                self.error("RateLimitError", status=429, text="insufficient_quota")
            ),
            ks.KeyFailure.CREDIT,
        )
        self.assertEqual(
            ks.classify_key_failure(self.error("RateLimitError", status=429)),
            ks.KeyFailure.RATE_LIMIT,
        )

    def test_wrapped_rate_limits_are_unwrapped(self):
        wrapped = RateLimitException(
            "API rate limit exceeded.",
            original_exception=self.error("RateLimitError", status=429),
        )
        self.assertEqual(ks.classify_key_failure(wrapped), ks.KeyFailure.RATE_LIMIT)

    def test_other_errors_do_not_move_to_another_key(self):
        self.assertIsNone(
            ks.classify_key_failure(self.error("BadRequestError", status=400))
        )
        self.assertIsNone(ks.classify_key_failure(RuntimeError("server down")))


if __name__ == "__main__":
    unittest.main()
