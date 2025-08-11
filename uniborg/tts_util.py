import traceback
import re
import tempfile
import wave
import os
import struct
import mimetypes
from uniborg import util
from uniborg.constants import BOT_META_INFO_PREFIX
import uuid

# --- TTS-Specific Shared Constants and Utilities ---

TTS_MAX_LENGTH = 10000  # Very high but not unlimited

# All 30 Gemini voices from the API documentation
GEMINI_VOICES = {
    "Zephyr": "Bright",
    "Puck": "Upbeat",
    "Charon": "Informative",
    "Kore": "Firm",
    "Enceladus": "Breathy",
    "Fenrir": "Serious",
    "Ceres": "Relaxed",
    "Aoede": "Warm",
    "Hendrix": "Steady",
    "Callisto": "Direct",
    "Dione": "Engaged",
    "Ganymede": "Rich",
    "Hera": "Authoritative",
    "Leda": "Grounded",
    "Mimas": "Energetic",
    "Orion": "Confident",
    "Rhea": "Gentle",
    "Salacia": "Soothing",
    "Tethys": "Expressive",
    "Umbriel": "Thoughtful",
    "Vega": "Professional",
    "Xanthe": "Animated",
    "Yarrow": "Sincere",
    "Atlas": "Deep",
    "Celeste": "Clear",
    "Echo": "Resonant",
    "Luna": "Soft",
    "Nova": "Dynamic",
    "Sol": "Bold",
    "Zen": "Calm",
}

TTS_MODELS = {
    "gemini-2.5-flash-preview-tts": "Flash Preview TTS",
    "gemini-2.5-pro-preview-tts": "Pro Preview TTS",
    "Disabled": "Disabled",
}

DEFAULT_VOICE = "Zephyr"


def truncate_text_for_tts(text: str) -> tuple[str, bool]:
    """
    Truncate text to TTS_MAX_LENGTH if needed.

    Returns:
        tuple: (truncated_text, was_truncated)
    """
    if len(text) <= TTS_MAX_LENGTH:
        return text, False

    # Truncate at character limit
    truncated = text[:TTS_MAX_LENGTH]

    # Try to truncate at word boundary if possible
    if truncated and not truncated[-1].isspace():
        last_space = truncated.rfind(" ")
        if last_space > TTS_MAX_LENGTH * 0.9:  # Only if we don't lose too much text
            truncated = truncated[:last_space]

    return truncated, True


def _parse_audio_mime_type(mime_type: str) -> dict[str, int]:
    """Parse bits per sample and rate from an audio MIME type string."""
    bits_per_sample = 16
    rate = 24000

    # Extract rate from parameters
    parts = mime_type.split(";")
    for param in parts:
        param = param.strip()
        if param.lower().startswith("rate="):
            try:
                rate_str = param.split("=", 1)[1]
                rate = int(rate_str)
            except (ValueError, IndexError):
                pass  # Keep rate as default
        elif param.startswith("audio/L"):
            try:
                bits_per_sample = int(param.split("L", 1)[1])
            except (ValueError, IndexError):
                pass  # Keep bits_per_sample as default

    return {"bits_per_sample": bits_per_sample, "rate": rate}


def _convert_to_wav(audio_data: bytes, mime_type: str) -> bytes:
    """Generate a WAV file header for the given audio data and parameters."""
    parameters = _parse_audio_mime_type(mime_type)
    bits_per_sample = parameters["bits_per_sample"]
    sample_rate = parameters["rate"]
    num_channels = 1
    data_size = len(audio_data)
    bytes_per_sample = bits_per_sample // 8
    block_align = num_channels * bytes_per_sample
    byte_rate = sample_rate * block_align
    chunk_size = 36 + data_size  # 36 bytes for header fields before data chunk size

    # WAV file header structure
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",          # ChunkID
        chunk_size,       # ChunkSize (total file size - 8 bytes)
        b"WAVE",          # Format
        b"fmt ",          # Subchunk1ID
        16,               # Subchunk1Size (16 for PCM)
        1,                # AudioFormat (1 for PCM)
        num_channels,     # NumChannels
        sample_rate,      # SampleRate
        byte_rate,        # ByteRate
        block_align,      # BlockAlign
        bits_per_sample,  # BitsPerSample
        b"data",          # Subchunk2ID
        data_size         # Subchunk2Size (size of audio data)
    )
    return header + audio_data


def _convert_wav_to_ogg(wav_path: str, ogg_path: str):
    """Convert WAV file to OGG with Opus codec for Telegram voice messages."""
    try:
        import ffmpeg
        
        # Convert WAV to OGG with Opus codec using typed-ffmpeg
        (
            ffmpeg
            .input(wav_path)
            .output(ogg_path, 
                   acodec='libopus',
                   ab='32k',     # Audio bitrate (correct parameter name)
                   ar=16000,     # Sample rate
                   ac=1)         # Channels (mono)
            .run(overwrite_output=True, quiet=True)
        )
        
    except ImportError:
        raise Exception("typed-ffmpeg not found. Please install typed-ffmpeg for TTS voice message support: pip install typed-ffmpeg")
    except Exception as e:
        raise Exception(f"Failed to convert WAV to OGG: {str(e)}")


async def generate_tts_audio(
    text: str, *, voice: str, model: str, api_key: str
) -> str:
    """
    Generate TTS audio using Gemini's speech generation API.

    Args:
        text: Text to convert to speech
        voice: Voice name from GEMINI_VOICES
        model: TTS model (e.g., "gemini-2.5-flash-preview-tts")
        api_key: Gemini API key

    Returns:
        Path to the generated OGG file (Telegram voice message format)

    Raises:
        Exception: On API errors
    """
    from google import genai
    from google.genai import types

    # Create client with API key
    client = genai.Client(api_key=api_key)

    # Prepare content using the modern API structure
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=text),
            ],
        ),
    ]
    
    generate_content_config = types.GenerateContentConfig(
        temperature=1,
        response_modalities=["audio"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice
                )
            )
        ),
    )

    # Create temporary OGG file
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as ogg_file:
        ogg_filename = ogg_file.name

    audio_chunks = []
    mime_type = None
    
    # Use streaming API to get audio data
    for chunk in client.models.generate_content_stream(
        model=model,
        contents=contents,
        config=generate_content_config,
    ):
        if (
            chunk.candidates is None
            or chunk.candidates[0].content is None
            or chunk.candidates[0].content.parts is None
        ):
            continue
            
        if (chunk.candidates[0].content.parts[0].inline_data 
            and chunk.candidates[0].content.parts[0].inline_data.data):
            
            inline_data = chunk.candidates[0].content.parts[0].inline_data
            audio_chunks.append(inline_data.data)
            
            # Store mime type from first chunk
            if mime_type is None:
                mime_type = inline_data.mime_type

    if not audio_chunks:
        raise Exception("No audio data returned from TTS API")
        
    # Combine all audio chunks
    combined_audio_data = b''.join(audio_chunks)
    
    # Create temporary WAV file
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wav_file:
        wav_filename = wav_file.name
    
    try:
        # Convert to WAV format if needed, or use extension from mime type
        file_extension = mimetypes.guess_extension(mime_type)
        if file_extension is None or file_extension != '.wav':
            # Convert to WAV using proper header
            wav_data = _convert_to_wav(combined_audio_data, mime_type)
            with open(wav_filename, 'wb') as f:
                f.write(wav_data)
        else:
            # Already WAV format
            with open(wav_filename, 'wb') as f:
                f.write(combined_audio_data)
        
        # Convert WAV to OGG with Opus codec for Telegram
        _convert_wav_to_ogg(wav_filename, ogg_filename)
        
        return ogg_filename
        
    finally:
        # Clean up intermediate WAV file
        try:
            os.remove(wav_filename)
        except Exception:
            pass  # Ignore cleanup errors


async def handle_tts_error(
    *,
    event,
    exception,
    service: str = "gemini",
    error_id_p: bool = True,
):
    """A generic error handler for TTS related operations."""
    error_id = uuid.uuid4() if error_id_p else None
    error_message = str(exception)

    base_user_facing_error = f"{BOT_META_INFO_PREFIX}TTS generation failed."

    user_facing_error = (
        f"{base_user_facing_error} (Error ID: `{error_id}`)"
        if error_id
        else base_user_facing_error
    )

    should_show_error_to_user = False
    if "quota" in error_message.lower() or "exceeded" in error_message.lower():
        should_show_error_to_user = True
    elif "api key not valid" in error_message.lower():
        user_facing_error = f"{BOT_META_INFO_PREFIX}TTS failed: Invalid Gemini API key. Use /setgeminikey to update."
        should_show_error_to_user = True

    is_admin = await util.isAdmin(event)
    is_private = event.is_private
    if is_private and is_admin:
        should_show_error_to_user = True

    if should_show_error_to_user:
        user_facing_error = f"{user_facing_error}\n\n**Error:** {error_message}"

    try:
        await event.reply(user_facing_error)
    except Exception as e:
        print(f"Error while sending TTS error message: {e}")

    if error_id:
        print(f"--- TTS ERROR ID: {error_id} ---")
    traceback.print_exc()
