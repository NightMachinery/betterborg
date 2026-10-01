import os

##
# Gemini model aliases that always point to the latest version
# GEMINI_FLASH_3 = "gemini/gemini-3-flash-preview"
GEMINI_FLASH_2_5 = "gemini/gemini-2.5-flash"
GEMINI_FLASH_3 = "gemini/gemini-3-flash-preview"
GEMINI_STT_LATEST = "gemini/gemini-flash-latest"
# GEMINI_FLASH_LATEST = GEMINI_FLASH_3
GEMINI_FLASH_LATEST = "gemini/gemini-flash-latest"
GEMINI_FLASH_LITE_LATEST = "gemini/gemini-flash-lite-latest"
GEMINI_PRO_LATEST = "gemini/gemini-3-pro-preview"

# Old 2.5 versions (commented for when 3.0 arrives)
# GEMINI_FLASH_2_5 = "gemini/gemini-2.5-flash"
# GEMINI_FLASH_LITE_2_5 = "gemini/gemini-2.5-flash-lite"

#: The OpenAI families are aliases that follow each family's newest model:
#: OpenRouter's own router alias here, and this repo's for Codex below.
#: `llm_models.RETIRED_MODELS` moves settings saved with older ids forward.
OR_OPENAI_SOL = "openrouter/~openai/gpt-sol-latest"
OR_OPENAI_LATEST = OR_OPENAI_SOL

#: Codex models are reached through the ChatGPT OAuth backend, not the public
#: API. Codex has no aliases of its own, so `uniborg/codex_aliases.py` resolves
#: these three from its catalog.
OPENAI_CODEX_SOL = "openai-codex/gpt-sol-latest"
OPENAI_CODEX_LUNA = "openai-codex/gpt-luna-latest"
OPENAI_CODEX_ASTRA = "openai-codex/gpt-astra-latest"
#: The Luna Reserve routing slug. `gpt-5.6-luna` is the model it presents as,
#: but only `gpt-reserve` bills to the separate reserve meter, which stays
#: usable after the regular plan allowance is spent. The Codex catalog marks it
#: `visibility: hide`, so model listings that filter on that never expose it.
OPENAI_CODEX_LUNA_RESERVE = "openai-codex/gpt-reserve"
#: The Codex catalog's "latest workhorse model"; Astra is its frontier one.
OPENAI_CODEX_LATEST = OPENAI_CODEX_SOL

PIONEER_BASE_URL = "https://api.pioneer.ai/v1"
PIONEER_OPUS_4_8 = "pioneer/claude-opus-4-8"
PIONEER_GPT_5_5 = "pioneer/gpt-5.5"
PIONEER_SONNET_4_6 = "pioneer/claude-sonnet-4-6"
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

#: Starts the caption of a twin file: the file copy of a long answer whose
#: text was also delivered. Window context modes skip twins, since the text
#: already carries the answer; Reply Chain keeps them, as a reply to a twin may
#: be all that brings the answer in. It must neither start with
#: `BOT_META_INFO_PREFIX` nor be a prefix of it, or meta filtering (which
#: applies in every mode) would drop twins too.
TWIN_FILE_MARKER = "\u200b\u2060"
##
GEMINI_CHAT_ROTATE_KEYS_P = True
GEMINI_STT_ROTATE_KEYS_P = True
GEMINI_API_KEYS = os.path.expanduser("~/.gemini_api_keys")

# STT model list — first entry is the default; the rest are tried in order when
# the primary model returns a high-demand / transient error, or refuses the
# caller's API key (Gemini 2.5 Flash is closed to new keys).
STT_MODELS = [
    # GEMINI_STT_LATEST,
    GEMINI_FLASH_2_5,
    # GEMINI_FLASH_3,
    GEMINI_FLASH_LITE_LATEST,
]
#: How long a model that refused an API key is skipped for that key.
STT_MODEL_UNAVAILABLE_SECONDS = 30 * 24 * 3600
STT_RETRIES_PER_MODEL = 4  # attempts on each model before moving to the next
STT_RETRY_SLEEP = 10.0  # seconds between all retry attempts
STT_RETRY_MAX_DELAY = 180.0  # upper cap per sleep
ADMIN_ONLY_COMMAND_IGNORED = (
    "You have invoked an admin-only command. Your request has been ignored."
)
