import asyncio
import os
import uuid
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Any
from dataclasses import dataclass, field

from pynight.common_icecream import ic
from uniborg import util

try:
    from google import genai
    from google.genai import types

    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    ic(
        "Google GenAI SDK not available. Please install with: pip install google-genai[live]"
    )

# Constants
LIVE_TIMEOUT = 10 * 60  # 10 minutes in seconds
CONCURRENT_LIVE_LIMIT = 3
ADMIN_CONCURRENT_LIVE_LIMIT = 5

# Audio format constants for Gemini Live API
GEMINI_AUDIO_SAMPLE_RATE = 16000
GEMINI_AUDIO_CHANNELS = 1


@dataclass
class LiveSession:
    """Represents an active live session with Gemini Live API."""

    chat_id: int
    user_id: int
    model: str
    api_key: str
    session: Optional[Any] = None  # genai.live.LiveSession
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    is_connected: bool = False
    pending_audio_queue: list = field(default_factory=list)
    _response_task: Optional[asyncio.Task] = None

    def is_expired(self) -> bool:
        """Check if session has expired due to inactivity."""
        return (datetime.now() - self.last_activity).total_seconds() > LIVE_TIMEOUT

    def update_activity(self):
        """Update last activity timestamp."""
        self.last_activity = datetime.now()


class LiveSessionManager:
    """Manages active Gemini Live API sessions."""

    def __init__(self):
        self.sessions: Dict[int, LiveSession] = {}  # chat_id -> LiveSession
        self._cleanup_task = None
        self._start_cleanup_task()

    def _start_cleanup_task(self):
        """Start background task for session cleanup."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_expired_sessions())

    async def _cleanup_expired_sessions(self):
        """Background task to clean up expired sessions."""
        while True:
            try:
                expired_chats = []
                for chat_id, session in self.sessions.items():
                    if session.is_expired():
                        expired_chats.append(chat_id)

                for chat_id in expired_chats:
                    await self.end_session(chat_id)

                await asyncio.sleep(60)  # Check every minute
            except Exception as e:
                ic(f"Error in cleanup task: {e}")
                await asyncio.sleep(60)

    def get_user_session_count(self, user_id: int) -> int:
        """Get current number of active sessions for a user."""
        return sum(
            1 for session in self.sessions.values() if session.user_id == user_id
        )

    def can_create_session(self, user_id: int) -> bool:
        """Check if user can create a new session based on limits."""
        current_count = self.get_user_session_count(user_id)
        limit = (
            ADMIN_CONCURRENT_LIVE_LIMIT
            if util.isAdmin(user_id)
            else CONCURRENT_LIVE_LIMIT
        )
        return current_count < limit

    async def create_session(
        self, chat_id: int, user_id: int, model: str, api_key: str
    ) -> LiveSession:
        """Create a new live session using Google GenAI SDK."""
        if not GENAI_AVAILABLE:
            raise ValueError(
                "Google GenAI SDK not available. Please install with: pip install google-genai[live]"
            )

        if not self.can_create_session(user_id):
            limit = (
                ADMIN_CONCURRENT_LIVE_LIMIT
                if util.isAdmin(user_id)
                else CONCURRENT_LIVE_LIMIT
            )
            raise ValueError(f"Maximum concurrent sessions limit reached ({limit})")

        # End existing session for this chat if any
        if chat_id in self.sessions:
            await self.end_session(chat_id)

        # Create session object
        session_obj = LiveSession(
            chat_id=chat_id, user_id=user_id, model=model, api_key=api_key
        )

        try:
            # Configure Google GenAI client
            client = genai.Client(api_key=api_key)

            # Configure session for audio responses
            config = types.LiveConnectConfig(
                response_modalities=[
                    "AUDIO"
                ],  # Only audio responses to avoid conflicts
            )

            # Create live session
            live_session = client.aio.live.connect(model=model, config=config)
            session_obj.session = live_session
            session_obj.is_connected = True

            ic(
                f"Created live session {session_obj.session_id[:8]}... for chat {chat_id} with model {model}"
            )

        except Exception as e:
            ic(f"Failed to create live session: {e}")
            raise ValueError(f"Failed to connect to Gemini Live API: {str(e)}")

        self.sessions[chat_id] = session_obj
        return session_obj

    def get_session(self, chat_id: int) -> Optional[LiveSession]:
        """Get active session for a chat."""
        return self.sessions.get(chat_id)

    async def end_session(self, chat_id: int) -> bool:
        """End a live session and cleanup resources."""
        session = self.sessions.pop(chat_id, None)
        if session:
            try:
                # Cancel response task if running
                if session._response_task and not session._response_task.done():
                    session._response_task.cancel()
                    try:
                        await session._response_task
                    except asyncio.CancelledError:
                        pass

                # Close the live session
                if session.session:
                    try:
                        await session.session.close()
                    except Exception as e:
                        ic(f"Error closing live session: {e}")

                session.is_connected = False
                ic(f"Ended live session for chat {chat_id}")

            except Exception as e:
                ic(f"Error ending session: {e}")
            return True
        return False

    def is_live_mode_active(self, chat_id: int) -> bool:
        """Check if live mode is active for a chat."""
        session = self.sessions.get(chat_id)
        return session is not None and session.is_connected and not session.is_expired()

    def update_session_activity(self, chat_id: int):
        """Update last activity for a session."""
        session = self.sessions.get(chat_id)
        if session:
            session.update_activity()


class GeminiLiveAPI:
    """Interface for Gemini Live API using Google GenAI SDK."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        if GENAI_AVAILABLE:
            self.client = genai.Client(api_key=api_key)

    async def send_text(self, session: Any, text: str):
        """Send text message to Gemini Live API."""
        try:
            await session.send(text, end_of_turn=True)
            ic(f"Sent text: {text[:50]}...")
        except Exception as e:
            ic(f"Error sending text: {e}")
            raise

    async def send_audio_chunk(self, session: Any, audio_data: bytes):
        """Send audio data to Gemini Live API."""
        try:
            await session.send(audio_data)
            ic(f"Sent audio chunk: {len(audio_data)} bytes")
        except Exception as e:
            ic(f"Error sending audio: {e}")
            raise


class AudioProcessor:
    """Handles audio format conversion for Gemini Live API."""

    @staticmethod
    async def convert_ogg_to_pcm(ogg_path: str) -> bytes:
        """Convert Telegram OGG audio to PCM format required by Gemini."""
        temp_pcm = tempfile.NamedTemporaryFile(delete=False, suffix=".pcm")
        temp_pcm.close()

        try:
            # Convert OGG to 16-bit PCM, 16kHz, mono using ffmpeg
            cmd = [
                "ffmpeg",
                "-i",
                ogg_path,
                "-ar",
                str(GEMINI_AUDIO_SAMPLE_RATE),
                "-ac",
                str(GEMINI_AUDIO_CHANNELS),
                "-f",
                "s16le",  # 16-bit PCM little-endian
                "-y",
                temp_pcm.name,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                ic(f"FFmpeg error: {stderr.decode()}")
                raise ValueError(
                    f"ffmpeg conversion failed with return code {process.returncode}"
                )

            # Read the converted PCM data
            with open(temp_pcm.name, "rb") as f:
                pcm_data = f.read()

            return pcm_data

        finally:
            # Clean up temporary file
            Path(temp_pcm.name).unlink(missing_ok=True)

    @staticmethod
    async def convert_pcm_to_ogg(pcm_data: bytes, sample_rate: int = 24000) -> bytes:
        """Convert PCM audio from Gemini to OGG format for Telegram."""
        temp_pcm = tempfile.NamedTemporaryFile(delete=False, suffix=".pcm")
        temp_ogg = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")

        try:
            # Write PCM data to temporary file
            with open(temp_pcm.name, "wb") as f:
                f.write(pcm_data)

            # Convert PCM to OGG using ffmpeg
            cmd = [
                "ffmpeg",
                "-f",
                "s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",  # mono
                "-i",
                temp_pcm.name,
                "-c:a",
                "libopus",  # Use Opus codec for OGG
                "-y",
                temp_ogg.name,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                ic(f"FFmpeg error: {stderr.decode()}")
                raise ValueError(
                    f"ffmpeg conversion failed with return code {process.returncode}"
                )

            # Read the converted OGG data
            with open(temp_ogg.name, "rb") as f:
                ogg_data = f.read()

            return ogg_data

        finally:
            # Clean up temporary files
            Path(temp_pcm.name).unlink(missing_ok=True)
            Path(temp_ogg.name).unlink(missing_ok=True)


# Global session manager instance
live_session_manager = LiveSessionManager()
