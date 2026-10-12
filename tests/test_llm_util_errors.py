"""Connection failures are told apart from other errors, with details for admins."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from uniborg import llm_util


def _wrapped_connect_error():
    try:
        try:
            raise httpx.ConnectError("All connection attempts failed")
        except httpx.ConnectError as e:
            raise RuntimeError("transcription failed") from e
    except RuntimeError as e:
        return e


class ConnectionErrorTests(unittest.TestCase):
    def test_a_connect_failure_is_found_through_the_cause_chain(self):
        error = _wrapped_connect_error()
        self.assertIsInstance(llm_util.find_connection_error(error), httpx.ConnectError)
        self.assertIsNone(llm_util.find_connection_error(ValueError("bad request")))

    def test_admin_details_name_the_cause_and_the_proxy_without_its_credentials(self):
        details = llm_util.admin_error_details(
            _wrapped_connect_error(),
            proxy_url="http://user:synthetic-secret@127.0.0.1:2080",
        )
        self.assertIn("RuntimeError: transcription failed", details)
        self.assertIn("caused by ConnectError: All connection attempts failed", details)
        self.assertIn("`http://127.0.0.1:2080`", details)
        self.assertNotIn("synthetic-secret", details)

    def test_other_errors_never_mention_the_proxy(self):
        details = llm_util.admin_error_details(
            ValueError("bad request"), proxy_url="http://127.0.0.1:2080"
        )
        self.assertEqual(details, "**Error:** ValueError: bad request")

    def test_admins_in_private_see_the_details_in_the_error_reply(self):
        event = SimpleNamespace(is_private=True, message=None)
        with patch.object(
            llm_util.util, "isAdmin", AsyncMock(return_value=True)
        ), patch.object(llm_util.util, "discreet_send", AsyncMock()) as send:
            asyncio.run(
                llm_util.handle_llm_error(
                    event=event,
                    exception=_wrapped_connect_error(),
                    base_error_message="Couldn't connect.",
                    error_id_p=False,
                    proxy_url="http://127.0.0.1:2080",
                )
            )
        text = send.await_args.args[1]
        self.assertIn("Couldn't connect.", text)
        self.assertIn("caused by ConnectError", text)
        self.assertIn("**Proxy:**", text)


if __name__ == "__main__":
    unittest.main()
