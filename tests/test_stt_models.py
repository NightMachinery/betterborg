"""The STT bot's model menu and its speech model (`uniborg/stt_models.py`).

The audio conversions run the real ffmpeg on generated files, and are skipped
where it is missing. The Gemini API is faked.
"""

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import llm

from uniborg import bot_util, stt_models

FFMPEG = shutil.which("ffmpeg")


class MenuTests(unittest.TestCase):
    def test_auto_comes_first_and_every_slug_fits_callback_data_unhashed(self):
        options = stt_models.menu_options()

        self.assertEqual(next(iter(options)), stt_models.AUTO)
        for slug in options:
            data = bot_util.sanitize_callback_data(slug)
            self.assertFalse(data.startswith(bot_util.CALLBACK_HASH_PREFIX), slug)
            self.assertEqual(bot_util.unsanitize_callback_data(data), slug)

    def test_a_choice_no_longer_offered_is_auto(self):
        self.assertEqual(
            stt_models.normalize_choice("gemini/gemini-1.0-pro"), stt_models.AUTO
        )
        self.assertEqual(stt_models.normalize_choice(None), stt_models.AUTO)
        self.assertEqual(
            stt_models.models_for_choice("gemini/gone", auto=["gemini/a", "gemini/b"]),
            ["gemini/a", "gemini/b"],
        )

    def test_a_chosen_model_is_tried_alone(self):
        self.assertEqual(
            stt_models.models_for_choice("gemini/gemini-3.8-flash", auto=["gemini/a"]),
            ["gemini/gemini-3.8-flash"],
        )

    def test_only_transcribe_is_a_speech_model(self):
        speech = [m.slug for m in stt_models.STT_MODEL_CHOICES if m.kind == "speech"]
        self.assertEqual(speech, ["3.5-transcribe"])
        self.assertTrue(stt_models.is_speech_model("gemini/gemini-3.5-transcribe"))
        self.assertFalse(stt_models.is_speech_model("gemini/gemini-flash-latest"))


class LoadMediaModelTests(unittest.TestCase):
    def setUp(self):
        stt_models._media_models.clear()
        self.addCleanup(stt_models._media_models.clear)

    def unknown(self, model_id):
        raise llm.UnknownModelError(model_id)

    def test_a_menu_model_llm_gemini_lacks_is_built_with_a_schema(self):
        with patch.object(llm, "get_async_model", self.unknown):
            model = stt_models.load_media_model("gemini/gemini-3.8-flash")
            again = stt_models.load_media_model("gemini/gemini-3.8-flash")

        self.assertIs(model, again)
        self.assertEqual(model.model_id, "gemini/gemini-3.8-flash")
        self.assertTrue(model.supports_schema)

    def test_an_installed_model_is_used_as_it_is(self):
        installed = object()
        with patch.object(llm, "get_async_model", return_value=installed):
            self.assertIs(
                stt_models.load_media_model("gemini/gemini-3.8-flash"), installed
            )

    def test_unknown_ids_and_the_speech_model_are_not_built(self):
        with patch.object(llm, "get_async_model", self.unknown):
            for model_id in ("gemini/gemini-0-nope", "gemini/gemini-3.5-transcribe"):
                with self.assertRaises(llm.UnknownModelError):
                    stt_models.load_media_model(model_id)


#: The shape of a real gemini-3.5-transcribe answer of 2026-10-01.
ANSWER = {
    "candidates": [
        {
            "content": {
                "parts": [{"audioTranscription": {"text": "Hello, this is a test."}}],
                "role": "model",
            },
            "finishReason": "STOP",
        }
    ],
    "modelVersion": "gemini-3.5-transcribe",
}


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class _Client:
    """Stands in for httpx.AsyncClient; records each post."""

    def __init__(self, response, posts):
        self.response = response
        self.posts = posts

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, headers, json):
        self.posts.append((url, headers, json))
        return self.response


class TranscribeTests(unittest.TestCase):
    model = stt_models.model_for_slug("3.5-transcribe")

    def transcribe(self, status_code=200, payload=ANSWER):
        posts = []
        client = _Client(_Response(status_code, payload), posts)
        with patch.object(stt_models.httpx, "AsyncClient", client):
            text = asyncio.run(
                stt_models.transcribe(
                    self.model,
                    audio=[stt_models.AudioInput("audio/ogg", b"OggS")],
                    api_key="the-key",
                )
            )
        return text, posts

    def test_the_audio_goes_inline_and_the_transcript_comes_back(self):
        text, [(url, headers, body)] = self.transcribe()

        self.assertEqual(text, "Hello, this is a test.")
        self.assertTrue(url.endswith("/models/gemini-3.5-transcribe:generateContent"))
        self.assertEqual(headers, {"x-goog-api-key": "the-key"})
        (part,) = body["contents"][0]["parts"]
        self.assertEqual(
            part["inline_data"], {"mime_type": "audio/ogg", "data": "T2dnUw=="}
        )
        self.assertNotIn("the-key", url)

    def test_an_error_keeps_its_status_for_the_retry_rules(self):
        payload = {
            "error": {
                "code": 429,
                "message": "Rate limit exceeded",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
        with self.assertRaises(stt_models.GeminiApiError) as caught:
            self.transcribe(429, payload)

        self.assertIn("429", str(caught.exception))
        self.assertIn("RESOURCE_EXHAUSTED: Rate limit exceeded", str(caught.exception))

    def test_a_blocked_request_is_an_error_and_silence_is_empty(self):
        with self.assertRaises(stt_models.GeminiApiError):
            stt_models.transcript_text({"promptFeedback": {"blockReason": "OTHER"}})
        self.assertEqual(stt_models.transcript_text({"candidates": []}), "")


def _ffmpeg(*args):
    subprocess.run(
        [FFMPEG, "-nostdin", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class AudioInputsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="stt_models_test_"))
        self.addCleanup(shutil.rmtree, self.dir)

    def make(self, name, *args):
        path = self.dir / name
        _ffmpeg(*args, str(path))
        return path

    def tone(self):
        return ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]

    def picture(self):
        return ["-f", "lavfi", "-i", "color=c=red:size=32x32:duration=1"]

    def test_voice_notes_pass_and_videos_and_mp3_become_ogg(self):
        voice = self.make("voice.ogg", *self.tone(), "-c:a", "libopus")
        song = self.make("song.mp3", *self.tone())
        video = self.make(
            "clip.mp4",
            *self.picture(),
            *self.tone(),
            "-shortest",
            "-pix_fmt",
            "yuv420p"
        )
        attachments = [
            llm.Attachment(content=voice.read_bytes(), type="audio/ogg"),
            llm.Attachment(path=str(song)),
            llm.Attachment(content=video.read_bytes(), type="video/mp4"),
        ]

        inputs = asyncio.run(stt_models.audio_inputs(attachments))

        self.assertEqual(inputs.skipped, 0)
        self.assertEqual([a.mime_type for a in inputs.audio], ["audio/ogg"] * 3)
        self.assertEqual(inputs.audio[0].data, voice.read_bytes())
        for item in inputs.audio[1:]:
            self.assertTrue(item.data.startswith(b"OggS"))

    def test_images_and_silent_videos_are_skipped(self):
        silent = self.make("silent.mp4", *self.picture(), "-pix_fmt", "yuv420p")
        image = self.make("still.png", *self.picture(), "-frames:v", "1")
        voice = self.make("voice.ogg", *self.tone(), "-c:a", "libopus")
        attachments = [
            llm.Attachment(content=silent.read_bytes(), type="video/mp4"),
            llm.Attachment(path=str(image)),
        ]

        with self.assertRaises(stt_models.NoSoundError):
            asyncio.run(stt_models.audio_inputs(attachments))
        inputs = asyncio.run(
            stt_models.audio_inputs(
                attachments + [llm.Attachment(path=str(voice), type="audio/ogg")]
            )
        )
        self.assertEqual((len(inputs.audio), inputs.skipped), (1, 2))


if __name__ == "__main__":
    unittest.main()
