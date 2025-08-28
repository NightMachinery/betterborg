##
CHAT_TITLE_MODEL = "gemini/gemini-2.5-flash-lite"
##
DEFAULT_FILE_LENGTH_THRESHOLD = 8000
DEFAULT_FILE_ONLY_LENGTH_THRESHOLD = 60000
##
#: An invisible character sequence to prefix bot meta messages.
#: This allows us to filter them out from the conversation history.
BOT_META_INFO_PREFIX = "\u200b\u200b\u200b\u200b"

# BOT_META_INFO_LINE = f"{BOT_META_INFO_PREFIX}---{BOT_META_INFO_PREFIX}"
BOT_META_INFO_LINE = f"{BOT_META_INFO_PREFIX}── ※ ──{BOT_META_INFO_PREFIX}"
##
