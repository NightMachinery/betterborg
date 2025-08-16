from pynight.common_icecream import ic
import asyncio
import os
import uuid
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Any
from dataclasses import dataclass, field

from pynight.common_icecream import ic
import traceback
from uniborg import util

try:
    from google import genai
    from google.genai import types

    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    print(
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
    _session_context: Optional[Any] = None
    _live_connection: Optional[Any] = None

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
                print(f"Error in cleanup task: {e}")
                traceback.print_exc()
                await asyncio.sleep(60)

    def get_user_session_count(self, user_id: int) -> int:
        """Get current number of active sessions for a user."""
        return sum(
            1 for session in self.sessions.values() if session.user_id == user_id
        )

    async def can_create_session(self, user_id: int) -> bool:
        """Check if user can create a new session based on limits."""
        current_count = self.get_user_session_count(user_id)
        is_admin = util.is_admin_by_id(user_id)
        limit = ADMIN_CONCURRENT_LIVE_LIMIT if is_admin else CONCURRENT_LIVE_LIMIT
        return current_count < limit

    async def create_session(
        self, chat_id: int, user_id: int, model: str, api_key: str
    ) -> LiveSession:
        """Create a new live session using Google GenAI SDK."""
        if not GENAI_AVAILABLE:
            raise ValueError(
                "Google GenAI SDK not available. Please install with: pip install google-genai[live]"
            )

        if not await self.can_create_session(user_id):
            is_admin = util.is_admin_by_id(user_id)
            limit = ADMIN_CONCURRENT_LIVE_LIMIT if is_admin else CONCURRENT_LIVE_LIMIT
            raise ValueError(f"Maximum concurrent sessions limit reached ({limit})")

        # Check proxy configuration and admin permissions
        from uniborg.llm_util import get_proxy_config_or_error

        proxy_url, _ = get_proxy_config_or_error(user_id)

        # End existing session for this chat if any
        if chat_id in self.sessions:
            await self.end_session(chat_id)

        # Create session object
        session_obj = LiveSession(
            chat_id=chat_id, user_id=user_id, model=model, api_key=api_key
        )

        try:
            # Base configuration for live session
            config_kwargs = {
                "response_modalities": ["AUDIO"],  # Audio responses
                "realtime_input_config": {
                    "automatic_activity_detection": {"disabled": True}
                },
            }

            # Add proxy configuration if available
            if proxy_url:
                config_kwargs["http_options"] = types.HttpOptions(
                    client_args={"proxy": proxy_url},
                    async_client_args={"proxy": proxy_url},
                )

            live_connect_config = types.LiveConnectConfig(**config_kwargs)

            # Configure Google GenAI client
            client = genai.Client(api_key=api_key)

            # Create live session connection object
            session_obj.session = client.aio.live.connect(
                model=model, config=live_connect_config
            )
            session_obj.is_connected = False  # Will be set to True after connection

            print(f"Live session object created for chat {chat_id}")

            print(
                f"Created live session {session_obj.session_id[:8]}... for chat {chat_id} with model {model}"
            )

        except Exception as e:
            print(f"Failed to create live session: {e}")
            traceback.print_exc()
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

                # Close the live session properly
                if session._session_context:
                    try:
                        await session._session_context.__aexit__(None, None, None)
                        print(f"Live session context closed for chat {chat_id}")
                    except Exception as e:
                        print(f"Error closing live session context: {e}")
                        traceback.print_exc()
                elif session.session:
                    try:
                        # For non-context sessions, close directly
                        if hasattr(session.session, "close"):
                            await session.session.close()
                        print(f"Live session closed for chat {chat_id}")
                    except Exception as e:
                        print(f"Error closing live session: {e}")
                        traceback.print_exc()

                session.is_connected = False
                print(f"Ended live session for chat {chat_id}")

            except Exception as e:
                print(f"Error ending session: {e}")
                traceback.print_exc()
            return True
        return False

    def is_live_mode_active(self, chat_id: int) -> bool:
        """Check if live mode is active for a chat."""
        session = self.sessions.get(chat_id)
        if session is None or session.is_expired():
            return False

        # If session context was started but connection lost, consider it inactive
        if session._session_context is not None and not session.is_connected:
            return False

        return True

    def update_session_activity(self, chat_id: int):
        """Update last activity for a session."""
        session = self.sessions.get(chat_id)
        if session:
            session.update_activity()


class GeminiLiveAPI:
    """Interface for Gemini Live API using Google GenAI SDK."""

    def __init__(self, api_key: str, *, user_id: int = None):
        self.api_key = api_key
        if GENAI_AVAILABLE:
            http_options = None

            if user_id is not None:
                # Check proxy configuration and admin permissions
                from uniborg.llm_util import get_proxy_config_or_error

                proxy_url, _ = get_proxy_config_or_error(user_id)

                if proxy_url:
                    try:
                        http_options = types.HttpOptions(
                            client_args={"proxy": proxy_url},
                            async_client_args={"proxy": proxy_url},
                        )
                        print(f"GeminiLiveAPI: Using proxy: {proxy_url}")
                    except Exception as e:
                        print(
                            f"GeminiLiveAPI: Error configuring proxy {proxy_url}: {e}"
                        )
                        print("GeminiLiveAPI: Falling back to no proxy")
                        http_options = None

            self.client = genai.Client(api_key=api_key, http_options=http_options)

    async def send_text(self, session: Any, text: str):
        """Send text message to Gemini Live API."""
        try:
            # Send text with proper API format
            content = types.Content(role="user", parts=[types.Part(text=text)])
            await session.send_client_content(turns=content)
            print(f"Sent text: {text[:50]}...")
        except Exception as e:
            print(f"Error sending text: {e}")
            traceback.print_exc()
            raise

    async def send_audio_chunk(self, session: Any, audio_data: bytes):
        """Send audio data to Gemini Live API with manual VAD."""
        try:
            # Send activity start
            await session.send_realtime_input(activity_start=types.ActivityStart())

            # Send audio data
            await session.send_realtime_input(
                audio=types.Blob(data=audio_data, mime_type="audio/pcm;rate=16000")
            )

            # Send activity end
            await session.send_realtime_input(activity_end=types.ActivityEnd())

            print(f"Sent audio chunk: {len(audio_data)} bytes")
        except Exception as e:
            print(f"Error sending audio: {e}")
            traceback.print_exc()
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
                print(f"FFmpeg error: {stderr.decode()}")
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
                print(f"FFmpeg error: {stderr.decode()}")
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
