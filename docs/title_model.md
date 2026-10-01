# Title Model

The chat bot names some of the files it sends with a short LLM call. That call
writes three things: a title, a file name and a summary of at most 70 words,
which becomes the caption. The **title model** is the model that makes this
call. Each user picks theirs with `/setTitleModel`.

## Where titles are written

- **The file copy of a long answer.** An answer of 4000 characters or more
  also goes out as a `.md` file, and an answer past the file-only threshold
  goes out only as a file (`docs/twin_files.md`). When the text was also
  sent, the title is written after it, so a slow title delays only the file.
- **`/asfile` exports** of the conversation.

The STT bot names its transcript files with Gemini Flash Lite, as before. It
runs as a separate instance and does not read the chat bot's settings.

## Choices

`/setTitleModel` opens a menu: **Auto**, then the chat models the user may
use, then **Cancel**. A custom model id can be typed instead, and
`/setTitleModel MODEL_ID` sets one directly. `auto`, `reset` and the other
reset words go back to Auto.

**Auto** (the default) means:

- the Codex Luna Reserve (`openai-codex/gpt-reserve`) for a user with Codex
  access;
- the latest Gemini Flash Lite (`gemini/gemini-flash-lite-latest`) for
  everyone else.

The menu's Auto button names the model it means for that user. Auto is
stored as `"auto"`, not as the model it resolves to, so it follows changes to
the user's Codex access.

## Fallbacks

`uniborg.title_util.generate_title` tries the chosen model, then Flash Lite
when the chosen model is anything else:

- A Codex model chosen by a user without Codex access is replaced by Flash
  Lite.
- Any failure falls through to Flash Lite: an error, a reply that is not the
  JSON object asked for, no API key for the model's service, or no answer
  within 30 seconds.
- When the Reserve answers `usage_limit_reached`, Codex titles stay off in
  that process until the meter resets (or for an hour, when the error names
  no reset time). The chat itself is unaffected.
- When Flash Lite fails too, the file gets a random name and its caption says
  the title failed.

Gemini calls use the user's effective Gemini key, the same one the answer
used (the owner's rotation included). Other services use the key the user
stored for that service.

## The Reserve as a default

`docs/codex_luna_reserve.md` keeps chat answers off the Reserve unless the
user asks, because the Reserve is a separate allowance. Titles are the one
exception, chosen deliberately: a title costs one short request at low
effort, and a user who prefers their regular allowance or Gemini can pick
another title model.

## Code

- `uniborg/title_util.py`: `resolve_title_model`, `generate_title` (choice,
  fallbacks, the Codex pause) and `complete_structured` (one request: Codex
  through `codex_util.complete_codex_text` with the JSON schema in its
  instructions, anything else through litellm's structured output).
- `uniborg/util.py`: `edit_message` and `send_as_file_with_filename` take a
  `title_generator`. Without one they keep using `title_model` (default
  `CHAT_TITLE_MODEL`) and the sender's stored key.
- `llm_chat_plugins/llm_chat.py`: `UserPrefs.title_model`,
  `_file_title_generator`, `_build_title_model_menu`, and the
  `MODEL_MENU_SCOPE_TITLE` scope of the model menus.
