import os
##
# Gemini model aliases that always point to the latest version
# GEMINI_FLASH_3 = "gemini/gemini-3-flash-preview"
GEMINI_FLASH_2_5 =  "gemini/gemini-2.5-flash-preview-09-2025"
GEMINI_FLASH_3 = "gemini/gemini-3-flash-preview"
GEMINI_STT_LATEST = GEMINI_FLASH_3
GEMINI_FLASH_LATEST = GEMINI_FLASH_3
# GEMINI_FLASH_LATEST = "gemini/gemini-flash-latest"
GEMINI_FLASH_LITE_LATEST = "gemini/gemini-flash-lite-latest"
GEMINI_PRO_LATEST = "gemini/gemini-3-pro-preview"

# Old 2.5 versions (commented for when 3.0 arrives)
# GEMINI_FLASH_2_5 = "gemini/gemini-2.5-flash"
# GEMINI_FLASH_LITE_2_5 = "gemini/gemini-2.5-flash-lite"

OR_OPENAI_5_2 = "openrouter/openai/gpt-5.2"
OR_OPENAI_LATEST = OR_OPENAI_5_2
##
CHAT_TITLE_MODEL = GEMINI_FLASH_LITE_LATEST
##
DEFAULT_FILE_LENGTH_THRESHOLD = 4000
# DEFAULT_FILE_LENGTH_THRESHOLD = 6000

DEFAULT_FILE_ONLY_LENGTH_THRESHOLD = 60000
##
STT_FILE_LENGTH_THRESHOLD = DEFAULT_FILE_LENGTH_THRESHOLD
STT_FILE_ONLY_LENGTH_THRESHOLD = DEFAULT_FILE_ONLY_LENGTH_THRESHOLD
##
#: An invisible character sequence to prefix bot meta messages.
#: This allows us to filter them out from the conversation history.
BOT_META_INFO_PREFIX = "\u200b\u200b\u200b\u200b"

# BOT_META_INFO_LINE = f"{BOT_META_INFO_PREFIX}---{BOT_META_INFO_PREFIX}"
BOT_META_INFO_LINE = f"{BOT_META_INFO_PREFIX}── ※ ──{BOT_META_INFO_PREFIX}"
##
GEMINI_ROTATE_KEYS_P = True
GEMINI_STT_ROTATE_KEYS_P = True
GEMINI_API_KEYS = os.path.expanduser("~/.gemini_api_keys")
