"""Registry of chat models and the reasoning-effort levels each one supports.

Reasoning effort is a per-model setting: the level set and the sensible default
differ between providers (and between models of the same provider). Every spec
inherits `DEFAULT_REASONING_EFFORT` unless it declares its own, so the operator
has a single knob for the global default.
"""

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from uniborg import codex_util
from uniborg import pioneer_util
from uniborg.constants import (
    GEMINI_FLASH_2_5,
    GEMINI_FLASH_3,
    GEMINI_FLASH_LATEST,
    GEMINI_FLASH_LITE_LATEST,
    GEMINI_PRO_LATEST,
    OPENAI_CODEX_ASTRA,
    OPENAI_CODEX_GPT_5_5,
    OPENAI_CODEX_GPT_5_6_LUNA,
    OPENAI_CODEX_GPT_5_6_SOL,
    OPENAI_CODEX_GPT_5_6_TERRA,
    OR_OPENAI_5_6_SOL,
)

#: The single operator-level default. Every model spec falls back to this.
DEFAULT_REASONING_EFFORT = "medium"

#: Level sets, ordered from cheapest to most expensive.
NO_REASONING_LEVELS: Tuple[str, ...] = ()
GEMINI_REASONING_LEVELS = ("disable", "low", "medium", "high")
#: Verified against the ChatGPT Codex backend: the Responses API accepts
#: none/minimal/low/medium/high/xhigh/max. `ultra` is a Codex-app subagent mode
#: and is rejected by the API, so it is deliberately absent.
OPENAI_REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
#: GPT-6 Astra rejects `none`.
ASTRA_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: GPT-5.5 predates `max`.
OPENAI_LEGACY_REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh")
OPENROUTER_REASONING_LEVELS = ("low", "medium", "high")
#: Pioneer's OpenAI-compatible Responses endpoint. Not probed directly; these
#: match what Pioneer's own model list advertises for its Claude models.
PIONEER_REASONING_LEVELS = ("low", "medium", "high")


@dataclass(frozen=True)
class ModelSpec:
    """Everything the bot needs to know about a model's reasoning support."""

    id: str
    display_name: str
    reasoning_levels: Tuple[str, ...] = NO_REASONING_LEVELS
    default_reasoning: str = DEFAULT_REASONING_EFFORT
    admin_only: bool = False
    #: Known to the registry but not offered in the model pickers.
    hidden: bool = False

    def supports_reasoning_p(self) -> bool:
        return bool(self.reasoning_levels)

    def supports_level_p(self, level: Optional[str]) -> bool:
        return bool(level) and level in self.reasoning_levels

    def effective_default_reasoning(self) -> Optional[str]:
        """The level to send when the user has expressed no preference."""
        if not self.reasoning_levels:
            return None
        if self.default_reasoning in self.reasoning_levels:
            return self.default_reasoning
        return None


def is_gemini_model_p(model: str) -> bool:
    return bool(model and re.search(r"\bgemini\b", model, re.IGNORECASE))


MODEL_SPECS = [
    ## Gemini
    ModelSpec(GEMINI_FLASH_LATEST, "Gemini Flash (Latest)", GEMINI_REASONING_LEVELS),
    ModelSpec(
        GEMINI_FLASH_LITE_LATEST,
        "Gemini Flash Lite (Latest)",
        GEMINI_REASONING_LEVELS,
    ),
    ModelSpec(GEMINI_FLASH_2_5, "Gemini 2.5 Flash", GEMINI_REASONING_LEVELS),
    ModelSpec(GEMINI_PRO_LATEST, "Gemini 3 Pro", GEMINI_REASONING_LEVELS),
    ModelSpec(GEMINI_FLASH_3, "Gemini 3 Flash", GEMINI_REASONING_LEVELS),
    # ModelSpec("gemini/gemini-2.5-pro", "Gemini 2.5 Pro", GEMINI_REASONING_LEVELS),
    # ModelSpec("openrouter/google/gemini-2.5-pro", "Gemini 2.5 Pro (OpenRouter)", GEMINI_REASONING_LEVELS),
    # ModelSpec("gemini/gemini-2.0-flash", "Gemini 2 Flash", GEMINI_REASONING_LEVELS),
    # ModelSpec("gemini/gemini-2.0-flash-preview-image-generation", "Gemini 2 Flash Image"),
    # ModelSpec("gemini/gemini-2.5-flash-image-preview", "Gemini 2.5 Flash Image"),
    ## OpenAI
    ModelSpec(
        OR_OPENAI_5_6_SOL, "GPT-5.6 Sol (OpenRouter)", OPENROUTER_REASONING_LEVELS
    ),
    # ModelSpec("openrouter/openai/chatgpt-4o-latest", "ChatGPT 4o (OpenRouter)"),
    ## Anthropic Claude
    # ModelSpec("openrouter/anthropic/claude-sonnet-4.5", "Claude Sonnet 4.5 (OpenRouter)", OPENROUTER_REASONING_LEVELS),
    # ModelSpec("openrouter/anthropic/claude-opus-4.5", "Claude Opus 4.5 (OpenRouter)", OPENROUTER_REASONING_LEVELS),
    ## Grok
    # ModelSpec("openrouter/x-ai/grok-4", "Grok 4 (OpenRouter)", OPENROUTER_REASONING_LEVELS),
    ## Free models
    # ModelSpec("openrouter/moonshotai/kimi-k2:free", "🎁 Kimi K2 (Free, OpenRouter)"),
    # ModelSpec("openrouter/qwen/qwen3-coder:free", "🎁 Qwen3 Coder (Free, OpenRouter)"),
    # ModelSpec("openrouter/z-ai/glm-4.5-air:free", "🎁 GLM-4.5 Air (Free, OpenRouter)"),
    #: model name is too long for Telegram API's `data` field in callback buttons
    # ModelSpec("openrouter/cognitivecomputations/dolphin-mistral-24b-venice-edition:free", "🎁 Venice Uncensored 24B (Free, OpenRouter)"),
    ## DeepSeek
    ModelSpec("deepseek/deepseek-chat", "DeepSeek Chat"),
    ModelSpec("deepseek/deepseek-reasoner", "DeepSeek Reasoner"),
    ## Mistral
    ModelSpec("mistral/mistral-medium-latest", "Mistral Medium (Latest)"),
    ModelSpec("mistral/magistral-medium-latest", "Magistral Medium (Latest)"),
    ModelSpec("mistral/pixtral-large-latest", "Pixtral Large (Latest)"),
    ## Codex (admin-only, ChatGPT OAuth)
    ModelSpec(
        OPENAI_CODEX_GPT_5_6_SOL,
        "GPT-5.6 Sol (Codex, Admin)",
        OPENAI_REASONING_LEVELS,
        admin_only=True,
    ),
    ModelSpec(
        OPENAI_CODEX_ASTRA,
        "GPT-6 Astra (Codex, Admin)",
        ASTRA_REASONING_LEVELS,
        admin_only=True,
    ),
    ## Pioneer (admin-only) - no longer used, kept for easy re-enabling.
    #: Uncomment these and the `.sn`/`.o` prefixes in llm_chat.py to bring it
    #: back; the Pioneer backend itself is still wired up.
    # ModelSpec(PIONEER_OPUS_4_8, "Pioneer Opus 4.8 (Admin)", PIONEER_REASONING_LEVELS, admin_only=True),
    # ModelSpec(PIONEER_GPT_5_5, "Pioneer GPT-5.5 (Admin)", PIONEER_GPT_REASONING_LEVELS, admin_only=True),
    # ModelSpec(PIONEER_SONNET_4_6, "Pioneer Sonnet 4.6 (Admin)", PIONEER_REASONING_LEVELS, admin_only=True),
    ## Codex models known to the registry but kept out of the pickers.
    ModelSpec(
        OPENAI_CODEX_GPT_5_6_TERRA,
        "GPT-5.6 Terra (Codex, Admin)",
        OPENAI_REASONING_LEVELS,
        admin_only=True,
        hidden=True,
    ),
    ModelSpec(
        OPENAI_CODEX_GPT_5_6_LUNA,
        "GPT-5.6 Luna (Codex, Admin)",
        OPENAI_REASONING_LEVELS,
        admin_only=True,
        hidden=True,
    ),
    ModelSpec(
        OPENAI_CODEX_GPT_5_5,
        "GPT-5.5 (Codex, Admin)",
        OPENAI_LEGACY_REASONING_LEVELS,
        admin_only=True,
        hidden=True,
    ),
]

MODEL_SPECS_BY_ID: Dict[str, ModelSpec] = {spec.id: spec for spec in MODEL_SPECS}


def public_model_choices() -> Dict[str, str]:
    """Picker entries available to everyone, as {model_id: display_name}."""
    return {
        spec.id: spec.display_name
        for spec in MODEL_SPECS
        if not spec.admin_only and not spec.hidden
    }


def admin_model_choices() -> Dict[str, str]:
    """Picker entries available only to bot admins."""
    return {
        spec.id: spec.display_name
        for spec in MODEL_SPECS
        if spec.admin_only and not spec.hidden
    }


def _synthesized_spec(model: str) -> ModelSpec:
    """Best-effort spec for a model the registry does not know (custom IDs)."""
    if codex_util.is_codex_model(model):
        return ModelSpec(
            model,
            model,
            OPENAI_REASONING_LEVELS,
            admin_only=True,
            hidden=True,
        )
    if pioneer_util.is_pioneer_model(model):
        return ModelSpec(
            model,
            model,
            PIONEER_REASONING_LEVELS,
            admin_only=True,
            hidden=True,
        )
    if is_gemini_model_p(model):
        return ModelSpec(model, model, GEMINI_REASONING_LEVELS, hidden=True)
    if model.startswith("openrouter/"):
        return ModelSpec(model, model, OPENROUTER_REASONING_LEVELS, hidden=True)
    #: Unknown providers get no reasoning UI and no reasoning parameter.
    return ModelSpec(model, model, NO_REASONING_LEVELS, hidden=True)


def spec_for_model(model: Optional[str]) -> ModelSpec:
    """Return the spec for `model`, synthesizing one for unregistered IDs."""
    if not model:
        return ModelSpec("", "", NO_REASONING_LEVELS, hidden=True)
    spec = MODEL_SPECS_BY_ID.get(model)
    if spec is not None:
        return spec
    return _synthesized_spec(model)


def reasoning_levels_for_model(model: Optional[str]) -> Tuple[str, ...]:
    return spec_for_model(model).reasoning_levels
