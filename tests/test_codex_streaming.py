import asyncio
import base64
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from uniborg import codex_util


class _Stream:
    def __init__(self, events=(), error=None, close_error=None):
        self.events = list(events)
        self.error = error
        self.close_error = close_error
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error

    async def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _Responses:
    def __init__(self, stream=None, error=None):
        self.stream = stream
        self.error = error
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.stream


class _Client:
    def __init__(self, stream=None, create_error=None):
        self.responses = _Responses(stream, create_error)
        self.closed = False

    async def close(self):
        self.closed = True


def _completed(output=None, status="completed"):
    return {
        "type": "response.completed",
        "response": {"status": status, "output": output or []},
    }


def _image_item(item_id, encoded, status="completed"):
    return {
        "id": item_id,
        "type": "image_generation_call",
        "status": status,
        "result": encoded,
    }


class CodexStreamingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        buffer = io.BytesIO()
        Image.new("RGB", (1, 1), "red").save(buffer, format="PNG")
        cls.png = buffer.getvalue()
        cls.png_b64 = base64.b64encode(cls.png).decode("ascii")

    async def _run(
        self,
        events=(),
        *,
        callback=None,
        stream_error=None,
        create_error=None,
        edit_interval=0.8,
    ):
        stream = _Stream(events, stream_error)
        client = _Client(stream, create_error)
        with mock.patch.object(
            codex_util, "_create_async_client", mock.AsyncMock(return_value=client)
        ):
            result = await codex_util.stream_codex_response(
                event=object(),
                response_message=object(),
                model="openai-codex/test-model",
                messages=[{"role": "user", "content": "hello"}],
                image_callback=callback,
                edit_interval=edit_interval,
            )
        return result, stream, client

    async def test_previews_are_delivered_immediately_and_in_stream_order(self):
        delivered = []

        async def callback(image):
            delivered.append((image.item_id, image.preview_index, image.data))

        png_b64 = self.png_b64

        class AssertingStream(_Stream):
            def __init__(self):
                super().__init__()

            async def _iterate(self):
                yield {
                    "type": "response.image_generation_call.partial_image",
                    "item_id": "image-a",
                    "partial_image_index": 0,
                    "partial_image_b64": png_b64,
                }
                self.assert_first_delivered()
                yield {
                    "type": "response.image_generation_call.partial_image",
                    "item_id": "image-a",
                    "partial_image_index": 1,
                    "partial_image_b64": png_b64,
                }
                yield _completed()

            def assert_first_delivered(self):
                self_test.assertEqual(delivered, [("image-a", 0, self_test.png)])

        self_test = self
        stream = AssertingStream()
        client = _Client(stream)
        with mock.patch.object(
            codex_util, "_create_async_client", mock.AsyncMock(return_value=client)
        ):
            result = await codex_util.stream_codex_response(
                event=object(),
                response_message=object(),
                model="openai-codex/test-model",
                messages=[{"role": "user", "content": "hello"}],
                image_callback=callback,
            )

        self.assertEqual(
            delivered, [("image-a", 0, self.png), ("image-a", 1, self.png)]
        )
        self.assertEqual(result.previews_delivered, 2)
        self.assertEqual(result.images_delivered, 0)

    async def test_duplicate_preview_and_final_events_are_delivered_once(self):
        delivered = []

        async def callback(image):
            delivered.append((image.item_id, image.preview_index))

        preview = {
            "type": "response.image_generation_call.partial_image",
            "item_id": "image-a",
            "partial_image_index": 0,
            "partial_image_b64": self.png_b64,
        }
        item = _image_item("image-a", self.png_b64)
        events = [
            preview,
            preview,
            {"type": "response.output_item.done", "item": item, "output_index": 0},
            {"type": "response.output_item.done", "item": item, "output_index": 0},
            _completed([item]),
        ]
        result, _, _ = await self._run(events, callback=callback)

        self.assertEqual(delivered, [("image-a", 0), ("image-a", None)])
        self.assertEqual((result.previews_delivered, result.images_delivered), (1, 1))

    async def test_distinct_item_ids_with_identical_bytes_are_both_delivered(self):
        delivered = []

        async def callback(image):
            delivered.append(image.item_id)

        items = [
            _image_item("image-a", self.png_b64),
            _image_item("image-b", self.png_b64),
        ]
        result, _, _ = await self._run([_completed(items)], callback=callback)

        self.assertEqual(delivered, ["image-a", "image-b"])
        self.assertEqual(result.images_delivered, 2)

    async def test_terminal_output_supplies_text_when_there_were_no_deltas(self):
        item = {
            "id": "message-a",
            "type": "message",
            "status": "completed",
            "content": [{"type": "output_text", "text": "terminal text"}],
        }
        result, _, _ = await self._run([_completed([item])])
        self.assertEqual(result.text, "terminal text")

    async def test_output_text_done_without_delta_supplies_text(self):
        events = [
            {
                "type": "response.output_text.done",
                "item_id": "message-a",
                "output_index": 0,
                "content_index": 0,
                "text": "done text",
            },
            _completed(),
        ]
        result, _, _ = await self._run(events)
        self.assertEqual(result.text, "done text")

    async def test_terminal_events_do_not_duplicate_streamed_text(self):
        item = {
            "id": "message-a",
            "type": "message",
            "status": "completed",
            "content": [{"type": "output_text", "text": "hello"}],
        }
        events = [
            {
                "type": "response.output_text.delta",
                "item_id": "message-a",
                "output_index": 0,
                "content_index": 0,
                "delta": "hel",
            },
            {
                "type": "response.output_text.delta",
                "item_id": "message-a",
                "output_index": 0,
                "content_index": 0,
                "delta": "lo",
            },
            {
                "type": "response.output_text.done",
                "item_id": "message-a",
                "output_index": 0,
                "content_index": 0,
                "text": "hello",
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            _completed([item]),
        ]
        result, _, _ = await self._run(events)
        self.assertEqual(result.text, "hello")

    async def test_image_only_response_succeeds(self):
        delivered = []

        async def callback(image):
            delivered.append(image)

        result, _, _ = await self._run(
            [_completed([_image_item("image-a", self.png_b64)])], callback=callback
        )
        self.assertEqual(result.text, "")
        self.assertTrue(result.has_image)
        self.assertEqual(delivered[0].file_extension, ".png")

    async def test_malformed_base64_is_wrapped(self):
        with self.assertRaisesRegex(
            codex_util.CodexStreamError, "malformed image data"
        ):
            await self._run(
                [_completed([_image_item("image-a", "not base64!")])],
                callback=mock.AsyncMock(),
            )

    async def test_non_image_bytes_are_rejected(self):
        encoded = base64.b64encode(b"plain text").decode("ascii")
        with self.assertRaisesRegex(
            codex_util.CodexStreamError, "malformed image data"
        ):
            await self._run(
                [_completed([_image_item("image-a", encoded)])],
                callback=mock.AsyncMock(),
            )

    async def test_image_without_item_identity_is_rejected(self):
        with self.assertRaisesRegex(
            codex_util.CodexStreamError, "missing its item identity"
        ):
            await self._run(
                [_completed([_image_item(None, self.png_b64)])],
                callback=mock.AsyncMock(),
            )

    async def test_preview_without_index_is_rejected(self):
        event = {
            "type": "response.image_generation_call.partial_image",
            "item_id": "image-a",
            "partial_image_b64": self.png_b64,
        }
        with self.assertRaisesRegex(codex_util.CodexStreamError, "missing its index"):
            await self._run([event], callback=mock.AsyncMock())

    async def test_callback_failure_is_wrapped_and_keeps_partial_counts_accurate(self):
        callback = mock.AsyncMock(side_effect=LookupError("upload failed"))
        with self.assertRaisesRegex(
            codex_util.CodexStreamError, "Failed to deliver"
        ) as raised:
            await self._run(
                [_completed([_image_item("image-a", self.png_b64)])], callback=callback
            )
        self.assertEqual(raised.exception.response.images_delivered, 0)

    async def test_create_time_usage_limit_reaches_the_error(self):
        error = SimpleNamespace(
            body={
                "type": "usage_limit_reached",
                "plan_type": "prolite",
                "resets_in_seconds": 3600,
            },
            status_code=429,
        )
        #: SimpleNamespace is not raisable, so carry the body on a real exception.
        rate_limited = RuntimeError("Error code: 429")
        rate_limited.body = error.body

        with self.assertRaises(codex_util.CodexStreamError) as raised:
            await self._run(create_error=rate_limited)

        usage_limit = raised.exception.usage_limit
        self.assertIsNotNone(usage_limit)
        self.assertEqual(usage_limit.plan_type, "prolite")
        self.assertIsNotNone(usage_limit.resets_at)

    async def test_mid_stream_usage_limit_is_captured_and_reported(self):
        event = {
            "type": "error",
            "code": "usage_limit_reached",
            "message": "The usage limit has been reached",
        }
        with self.assertRaises(codex_util.CodexStreamError) as raised:
            await self._run([event])

        self.assertIsNotNone(raised.exception.usage_limit)
        #: The payload used to be discarded entirely; keep the backend's words.
        self.assertIn("usage_limit_reached", str(raised.exception))

    async def test_plain_backend_error_carries_no_usage_limit(self):
        with self.assertRaises(codex_util.CodexStreamError) as raised:
            await self._run([{"type": "error"}])
        self.assertIsNone(raised.exception.usage_limit)

    def test_error_is_still_constructible_without_a_usage_limit(self):
        error = codex_util.CodexStreamError("boom", codex_util.CodexResponse(text=""))
        self.assertIsNone(error.usage_limit)

    async def test_backend_error_is_wrapped(self):
        with self.assertRaisesRegex(
            codex_util.CodexStreamError, "backend reported a streaming error"
        ):
            await self._run([{"type": "error"}])

    async def test_empty_completed_response_is_rejected(self):
        with self.assertRaisesRegex(codex_util.CodexStreamError, "empty result"):
            await self._run([_completed()])

    async def test_premature_end_of_stream_is_rejected(self):
        with self.assertRaisesRegex(
            codex_util.CodexStreamError, "ended before response completion"
        ):
            await self._run(
                [
                    {
                        "type": "response.output_text.done",
                        "item_id": "message-a",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "partial",
                    }
                ]
            )

    async def test_incomplete_response_is_rejected_with_partial_text(self):
        event = {
            "type": "response.incomplete",
            "response": {
                "status": "incomplete",
                "output": [
                    {
                        "id": "message-a",
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "partial"}],
                    }
                ],
            },
        }
        with self.assertRaisesRegex(
            codex_util.CodexStreamError, "ended with incomplete"
        ) as raised:
            await self._run([event])
        self.assertEqual(raised.exception.response.text, "partial")

    async def test_stream_and_client_close_after_success(self):
        result, stream, client = await self._run(
            [
                {
                    "type": "response.output_text.done",
                    "item_id": "message-a",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "ok",
                },
                _completed(),
            ]
        )
        self.assertEqual(result.text, "ok")
        self.assertTrue(stream.closed)
        self.assertTrue(client.closed)

    async def test_stream_and_client_close_after_stream_failure(self):
        stream = _Stream(error=OSError("connection lost"))
        client = _Client(stream)
        with mock.patch.object(
            codex_util, "_create_async_client", mock.AsyncMock(return_value=client)
        ):
            with self.assertRaisesRegex(codex_util.CodexStreamError, "connection lost"):
                await codex_util.stream_codex_response(
                    event=object(),
                    response_message=object(),
                    model="openai-codex/test-model",
                    messages=[{"role": "user", "content": "hello"}],
                )
        self.assertTrue(stream.closed)
        self.assertTrue(client.closed)

    async def test_cancellation_propagates_and_closes_stream_and_client(self):
        stream = _Stream(error=asyncio.CancelledError())
        client = _Client(stream)
        with mock.patch.object(
            codex_util, "_create_async_client", mock.AsyncMock(return_value=client)
        ):
            with self.assertRaises(asyncio.CancelledError):
                await codex_util.stream_codex_response(
                    event=object(),
                    response_message=object(),
                    model="openai-codex/test-model",
                    messages=[{"role": "user", "content": "hello"}],
                )
        self.assertTrue(stream.closed)
        self.assertTrue(client.closed)

    async def test_stream_close_failure_does_not_mask_cancellation_or_client_close(
        self,
    ):
        stream = _Stream(
            error=asyncio.CancelledError(), close_error=OSError("close failed")
        )
        client = _Client(stream)
        with mock.patch.object(
            codex_util, "_create_async_client", mock.AsyncMock(return_value=client)
        ):
            with self.assertRaises(asyncio.CancelledError):
                await codex_util.stream_codex_response(
                    event=object(),
                    response_message=object(),
                    model="openai-codex/test-model",
                    messages=[{"role": "user", "content": "hello"}],
                )
        self.assertTrue(stream.closed)
        self.assertTrue(client.closed)

    async def test_stream_close_failure_does_not_mask_backend_failure(self):
        stream = _Stream(
            error=OSError("backend failed"), close_error=OSError("close failed")
        )
        client = _Client(stream)
        with mock.patch.object(
            codex_util, "_create_async_client", mock.AsyncMock(return_value=client)
        ):
            with self.assertRaisesRegex(codex_util.CodexStreamError, "backend failed"):
                await codex_util.stream_codex_response(
                    event=object(),
                    response_message=object(),
                    model="openai-codex/test-model",
                    messages=[{"role": "user", "content": "hello"}],
                )
        self.assertTrue(stream.closed)
        self.assertTrue(client.closed)

    async def test_cancellation_during_callback_keeps_prior_delivery_and_closes(self):
        delivered = []

        async def callback(image):
            if image.preview_index == 1:
                raise asyncio.CancelledError()
            delivered.append((image.item_id, image.preview_index))

        events = [
            {
                "type": "response.image_generation_call.partial_image",
                "item_id": "image-a",
                "partial_image_index": 0,
                "partial_image_b64": self.png_b64,
            },
            {
                "type": "response.image_generation_call.partial_image",
                "item_id": "image-a",
                "partial_image_index": 1,
                "partial_image_b64": self.png_b64,
            },
        ]
        stream = _Stream(events)
        client = _Client(stream)
        with mock.patch.object(
            codex_util, "_create_async_client", mock.AsyncMock(return_value=client)
        ):
            with self.assertRaises(asyncio.CancelledError):
                await codex_util.stream_codex_response(
                    event=object(),
                    response_message=object(),
                    model="openai-codex/test-model",
                    messages=[{"role": "user", "content": "hello"}],
                    image_callback=callback,
                )
        self.assertEqual(delivered, [("image-a", 0)])
        self.assertTrue(stream.closed)
        self.assertTrue(client.closed)

    async def test_client_closes_when_stream_creation_fails(self):
        client = _Client(create_error=ConnectionError("create failed"))
        with mock.patch.object(
            codex_util, "_create_async_client", mock.AsyncMock(return_value=client)
        ):
            with self.assertRaisesRegex(codex_util.CodexStreamError, "create failed"):
                await codex_util.stream_codex_response(
                    event=object(),
                    response_message=object(),
                    model="openai-codex/test-model",
                    messages=[{"role": "user", "content": "hello"}],
                )
        self.assertTrue(client.closed)

    async def test_text_delta_edits_are_immediate_and_ordered(self):
        events = [
            {
                "type": "response.output_text.delta",
                "item_id": "message-a",
                "output_index": 0,
                "content_index": 0,
                "delta": "one",
            },
            {
                "type": "response.output_text.delta",
                "item_id": "message-a",
                "output_index": 0,
                "content_index": 0,
                "delta": " two",
            },
            _completed(),
        ]
        with mock.patch.object(
            codex_util.util, "edit_message", mock.AsyncMock()
        ) as edit:
            result, _, _ = await self._run(events, edit_interval=-1)

        self.assertEqual(result.text, "one two")
        self.assertEqual(
            [call.args[1] for call in edit.await_args_list], ["one▌", "one two▌"]
        )


if __name__ == "__main__":
    unittest.main()
