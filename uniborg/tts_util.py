import traceback
import re
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


async def generate_tts_audio(
    text: str, *, voice: str, model: str, api_key: str
) -> bytes:
    """
    Generate TTS audio using Gemini's speech generation API.

    Args:
        text: Text to convert to speech
        voice: Voice name from GEMINI_VOICES
        model: TTS model (e.g., "gemini-2.5-flash-preview-tts")
        api_key: Gemini API key

    Returns:
        Audio data as bytes

    Raises:
        Exception: On API errors
    """
    import google.generativeai as genai

    # Configure the API
    genai.configure(api_key=api_key)

    # Create the model
    tts_model = genai.GenerativeModel(model_name=model)

    # Configure voice
    voice_config = genai.types.VoiceConfig(name=voice)

    # Generate audio
    response = tts_model.generate_content(
        text, generation_config=genai.GenerationConfig(voice_config=voice_config)
    )

    # Return the audio data
    return response.parts[0].audio_data


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
