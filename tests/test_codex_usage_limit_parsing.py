import json
import math
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from uniborg import codex_util


#: The exact 429 a Codex user reported, as openai renders it into the exception
#: message: a Python repr of the decoded body, not JSON.
REPORTED_ERROR_STRING = (
    "Error code: 429 - {'error': {'type': 'usage_limit_reached', 'message': "
    "'The usage limit has been reached', 'plan_type': 'prolite', 'resets_at': "
    "1789921957, 'eligible_promo': None, 'resets_in_seconds': 368592}}"
)

#: What `openai.RateLimitError.body` actually holds for the same response: the
#: inner object, already unwrapped by the SDK.
REPORTED_BODY = {
    "type": "usage_limit_reached",
    "message": "The usage limit has been reached",
    "plan_type": "prolite",
    "resets_at": 1789921957,
    "eligible_promo": None,
    "resets_in_seconds": 368592,
}

NOW = datetime(2026, 9, 16, 11, 0, tzinfo=timezone.utc)
RESETS_AT = datetime.fromtimestamp(1789921957, tz=timezone.utc)


def _error(body=None, message=""):
    """An `openai.APIStatusError`-shaped stub carrying a decoded body."""
    return SimpleNamespace(body=body, status_code=429, args=(message,))


class UsageLimitParsingTests(unittest.TestCase):
    def parse(self, payload, *, now=NOW):
        return codex_util.parse_usage_limit(payload, now=now)

    def test_parses_the_already_unwrapped_sdk_body(self):
        limit = self.parse(_error(REPORTED_BODY))
        self.assertIsNotNone(limit)
        self.assertEqual(limit.plan_type, "prolite")
        self.assertEqual(limit.message, "The usage limit has been reached")
        self.assertEqual(limit.resets_at, RESETS_AT)

    def test_parses_the_wrapped_exception_message_string(self):
        limit = self.parse(REPORTED_ERROR_STRING)
        self.assertIsNotNone(limit)
        self.assertEqual(limit.plan_type, "prolite")
        self.assertEqual(limit.resets_at, RESETS_AT)

    def test_exception_message_is_python_literal_not_json(self):
        #: Pins why the parser must use ast.literal_eval; json.loads cannot read
        #: the single quotes and `None` that repr produces.
        stripped = REPORTED_ERROR_STRING.split(" - ", 1)[1]
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stripped)
        self.assertIsNotNone(codex_util._literal_body(REPORTED_ERROR_STRING))

    def test_exception_without_body_falls_back_to_its_string(self):
        limit = self.parse(RuntimeError(REPORTED_ERROR_STRING))
        self.assertIsNotNone(limit)
        self.assertEqual(limit.resets_at, RESETS_AT)

    def test_absolute_deadline_wins_over_a_disagreeing_relative_one(self):
        body = dict(REPORTED_BODY, resets_in_seconds=60)
        self.assertEqual(self.parse(_error(body)).resets_at, RESETS_AT)

    def test_relative_deadline_is_used_when_absolute_is_absent(self):
        body = {k: v for k, v in REPORTED_BODY.items() if k != "resets_at"}
        limit = self.parse(_error(body))
        self.assertEqual(limit.resets_at, NOW + timedelta(seconds=368592))

    def test_relative_deadline_rescues_an_implausible_absolute_one(self):
        body = dict(REPORTED_BODY, resets_at=1)
        limit = self.parse(_error(body))
        self.assertEqual(limit.resets_at, NOW + timedelta(seconds=368592))

    def test_missing_both_deadlines_still_reports_the_limit(self):
        body = {"type": "usage_limit_reached", "plan_type": "prolite"}
        limit = self.parse(_error(body))
        self.assertIsNotNone(limit)
        self.assertIsNone(limit.resets_at)
        self.assertEqual(limit.plan_type, "prolite")

    def test_deadline_beyond_the_maximum_window_is_discarded(self):
        beyond = NOW + timedelta(
            seconds=codex_util.CODEX_USAGE_LIMIT_MAX_WINDOW_SECONDS + 3600
        )
        body = {"type": "usage_limit_reached", "resets_at": beyond.timestamp()}
        self.assertIsNone(self.parse(_error(body)).resets_at)

    def test_past_deadline_is_discarded(self):
        body = {"type": "usage_limit_reached", "resets_at": (NOW.timestamp() - 5)}
        self.assertIsNone(self.parse(_error(body)).resets_at)

    def test_non_numeric_and_boolean_deadlines_are_discarded(self):
        for value in (True, False, "1789921957", None, math.inf, -1, 0, [1]):
            body = {"type": "usage_limit_reached", "resets_at": value}
            with self.subTest(value=value):
                self.assertIsNone(self.parse(_error(body)).resets_at)

    def test_millisecond_scale_deadline_is_coerced(self):
        body = {"type": "usage_limit_reached", "resets_at": 1789921957 * 1000}
        self.assertEqual(self.parse(_error(body)).resets_at, RESETS_AT)

    def test_non_string_message_and_plan_type_are_dropped(self):
        body = dict(REPORTED_BODY, message=12, plan_type=["pro"])
        limit = self.parse(_error(body))
        self.assertIsNone(limit.message)
        self.assertIsNone(limit.plan_type)

    def test_ordinary_rate_limit_is_not_a_usage_limit(self):
        body = dict(REPORTED_BODY, type="rate_limit_exceeded")
        self.assertIsNone(self.parse(_error(body)))

    def test_observed_bad_request_shape_returns_none(self):
        #: This backend answers a non-streaming request with a string `detail`.
        self.assertIsNone(self.parse(_error({"detail": "Stream must be set to true"})))

    def test_malformed_payloads_return_none_without_raising(self):
        oversized = "Error code: 429 - " + "{" * (
            codex_util.CODEX_ERROR_STRING_MAX_CHARS + 10
        )
        for payload in (
            None,
            "",
            "not an error at all",
            "Error code: 429 - {'unterminated': ",
            "Error code: 429 - [1, 2, 3]",
            oversized,
            _error("plain text body"),
            _error(None),
            123,
        ):
            with self.subTest(payload=repr(payload)[:40]):
                self.assertIsNone(self.parse(payload))

    def test_stream_error_event_uses_code_instead_of_type(self):
        event = {
            "type": "error",
            "code": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "resets_at": 1789921957,
        }
        limit = self.parse(event)
        self.assertIsNotNone(limit)
        self.assertEqual(limit.resets_at, RESETS_AT)

    def test_stream_error_object_is_read_by_attribute(self):
        event = SimpleNamespace(
            type="error", code="usage_limit_reached", resets_in_seconds=600
        )
        limit = self.parse(event)
        self.assertIsNotNone(limit)
        self.assertEqual(limit.resets_at, NOW + timedelta(seconds=600))

    def test_now_defaults_to_the_current_time(self):
        body = {"type": "usage_limit_reached", "resets_in_seconds": 3600}
        limit = codex_util.parse_usage_limit(_error(body))
        self.assertIsNotNone(limit.resets_at)
        self.assertGreater(limit.resets_at, datetime.now(timezone.utc))


class StreamErrorMessageTests(unittest.TestCase):
    def test_backend_code_and_message_are_preserved(self):
        message = codex_util._stream_error_message(
            {"type": "error", "code": "usage_limit_reached", "message": "spent"}
        )
        self.assertIn("usage_limit_reached", message)
        self.assertIn("spent", message)

    def test_bare_error_event_keeps_the_generic_wording(self):
        self.assertEqual(
            codex_util._stream_error_message({"type": "error"}),
            "Codex backend reported a streaming error.",
        )


if __name__ == "__main__":
    unittest.main()
