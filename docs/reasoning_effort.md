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
- Codex GPT-5.6: `none`, `low`, `medium`, `high`, `xhigh`, `max`
- Codex GPT-6 Astra: same, minus `none`
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
2. this chat's setting for that model
3. the user's personal setting for that model
4. the model's declared default

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
- Per-message prefixes set the effort for a single message, without changing
  the model: `.tn`, `.tl`, `.tm`, `.th`, `.tx`, `.txx` for none, low, medium,
  high, extra high and max. They combine with a model prefix in either order,
  so `.f .th question` and `.th .f question` both work, and an explicit effort
  prefix beats the effort baked into a model prefix.

A prefix only matches when followed by whitespace or the end of the message,
so another plugin's `.tlg` or `.tex` command is never swallowed. Prefixes are
also stripped from earlier messages when history is rebuilt, admin-only ones
included.

`/status` shows the resolved level for the effective model and where it came
from, plus any level stored for the current chat.
