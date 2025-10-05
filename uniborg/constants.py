##
# Gemini model aliases that always point to the latest version
GEMINI_FLASH_LATEST = "gemini/gemini-flash-latest"
GEMINI_FLASH_LITE_LATEST = "gemini/gemini-flash-lite-latest"

# Old 2.5 versions (commented for when 3.0 arrives)
# GEMINI_FLASH_2_5 = "gemini/gemini-2.5-flash"
# GEMINI_FLASH_LITE_2_5 = "gemini/gemini-2.5-flash-lite"
##
CHAT_TITLE_MODEL = GEMINI_FLASH_LITE_LATEST
##
DEFAULT_FILE_LENGTH_THRESHOLD = 4000
# DEFAULT_FILE_LENGTH_THRESHOLD = 6000

DEFAULT_FILE_ONLY_LENGTH_THRESHOLD = 60000
##
#: An invisible character sequence to prefix bot meta messages.
#: This allows us to filter them out from the conversation history.
BOT_META_INFO_PREFIX = "\u200b\u200b\u200b\u200b"

# BOT_META_INFO_LINE = f"{BOT_META_INFO_PREFIX}---{BOT_META_INFO_PREFIX}"
BOT_META_INFO_LINE = f"{BOT_META_INFO_PREFIX}── ※ ──{BOT_META_INFO_PREFIX}"
##
