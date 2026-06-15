# LLM Chat Last-N Context Limit

`llm_chat` uses a **Last N Messages** context mode that sends the most recent chat messages to the model. The unconfigured global default is **100** messages.

The effective limit is resolved in this order:

1. This chat's override, set with `/setLastNHere` or `/contextModeHere`.
2. The user's personal default, set with `/setLastN` or the `/contextMode` and `/groupContextMode` inline menus.
3. The global default of `100`.

Inline context menus expose quick picks for `50`, `100`, `200`, `400`, and `800`. Command input remains the advanced path and can still set any valid value up to the history cache maximum (`LAST_N_MAX`).
