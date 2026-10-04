# Reasoning Effort

Reasoning effort is a **per-model** preference. Different models expose
different level sets and want different defaults, so one global "thinking
level" cannot serve them all.

## Model registry

`uniborg/llm_models.py` holds a `ModelSpec` per model:

- `reasoning_levels`: the levels this model accepts, cheapest first. An empty
  tuple means the model has no reasoning knob, so no menu is offered and no
  parameter is sent.
- `default_reasoning`: what to use when nobody has expressed a preference.
  Every spec inherits `DEFAULT_REASONING_EFFORT` unless it overrides it, so the
  operator has one knob for the global default.

Level sets in use:

- Gemini: `disable`, `low`, `medium`, `high`
- Codex GPT Luna, and custom Codex ids: `none`, `low`, `medium`, `high`,
  `xhigh`, `max`
- Codex GPT Sol and GPT Astra: same, minus `none`. The three Codex families
  are aliases whose levels follow the model they point at
  (`docs/codex_models.md`, "Aliases").
- OpenRouter: `low`, `medium`, `high`

Models not in the registry (custom IDs typed by the user) get a spec
synthesized from their provider prefix.

## Storage

Both `UserPrefs` and `ChatPrefs` carry `thinking_by_model`, a
`{model_id: level}` map. Setting a level to `None` deletes the entry rather
than storing a null, so `exclude_defaults=True` keeps the JSON files small.

## Resolution order

`_get_effective_reasoning()` in `llm_chat_plugins/llm_chat.py` resolves, in
order:

1. a message prefix (`.th`, `.cx`, ...)
2. this private topic's setting for that model, inside a bot's private topic
   (`docs/private_topics.md`, "Per-topic settings")
3. this chat's setting for that model
4. the user's personal setting for that model
5. the model's declared default

A stored level the model does not accept is skipped rather than sent, so a
preference kept from a different model can never produce an invalid request.
Models with no reasoning levels resolve to nothing and no `reasoning_effort`
parameter is sent at all.

## Setting it

- `/setthink` shows the levels of whichever model is effective in the current
  chat, and stores the choice against that model for you personally.
- `/setthinkhere` does the same for the current chat, overriding the personal
  setting. In groups it needs group-admin or bot-admin rights.
- Both accept an inline level: `/setthink high`, `/setthinkhere max`. Pass
  `not set`, `none`, `clear`, `remove` or `reset` to drop the stored value and
  fall back to the model default. Note that on Codex models `none` is also a
  real level, so the reset keywords resolve to a reset there.
- Both model pickers (`/setModel`, `/setModelHere`) end with a row of 🧠
  buttons for the selected model's levels, so switching model and effort
  happens in one place. Picking a different model re-renders the row with that
  model's levels.
- On a bot, each picker is one message: the custom-model-ID hint, the
  buttons, and a last `❌ Cancel` row. Cancel drops the custom-ID prompt that
  this menu armed (matched by the menu message, so a newer prompt survives),
  then closes the menu and names the model in effect. A model already picked
  from the menu stays picked. Typing `cancel` closes the menu as well. In a
  group, Cancel on a `/setModelHere` menu needs the same admin rights as its
  other buttons. On a user account the pickers are unchanged.
- In a group, the bot's `/setModelHere` menu asks for no custom ID as the next
  message, and says to send `/setModelHere MODEL_ID` instead. Such a prompt
  could only be answered in private, so the user's next private message,
  whatever it said, became the group's model, and until then the prompt
  silenced their messages to the bot in the group.
- The bot-admin `.codex-users` model panel uses the same menu state for a target
  user's personal model. It keeps full model IDs in that panel and appends only
  the levels supported by the selected model; changes are saved in the target's
  per-model `thinking_by_model` map.
- Per-message prefixes set the effort for a single message, without changing
  the model: `.tn`, `.tl`, `.tm`, `.th`, `.tx`, `.txx` for none, low, medium,
  high, extra high and max. They combine with a model prefix in either order,
  so `.f .th question` and `.th .f question` both work, and an explicit effort
  prefix beats the effort baked into a model prefix. Each is `.t` plus the
  level's short alias (`REASONING_LEVEL_ALIASES` in `uniborg/llm_models.py`).
  Gemini's `disable` has no prefix. Automatic topic titles show the effort as
  a filling circle instead (`REASONING_LEVEL_SYMBOLS`,
  `docs/topic_titles.md`), with `disable` shown like `none`.

A prefix only matches when followed by whitespace or the end of the message,
so another plugin's `.tlg` or `.tex` command is never swallowed. Prefixes are
also stripped from earlier messages when history is rebuilt, admin-only ones
included.

`/status` shows the resolved level for the effective model and where it came
from, plus any level stored for the current chat.

## Codex Luna Reserve

`openai-codex/gpt-reserve` accepts every OpenAI level: `none`, `low`, `medium`,
`high`, `xhigh` and `max`, all verified against the live backend. Because
effort is a per-model preference, the automatic fallback to the Reserve
re-resolves it for the Reserve model rather than reusing the level chosen for
the model that hit the limit.

While a cross-provider stand-in is active, effort resolves for the stand-in
model. The effort saved against the Codex model is untouched and returns with
it. See [Codex quota fallback](codex_quota_fallback.md).
