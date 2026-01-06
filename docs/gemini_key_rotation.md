# Gemini Key Rotation

This document describes the current Gemini API key rotation behavior for the chat and STT plugins.

## Summary

- Rotation is **disabled by default per user** and must be toggled with the undocumented `.rot` command.
- Rotation is **admin-only** (user id `195391705`).
- Rotation is only used when three conditions are true:
  1. The global flag for the plugin is enabled.
  2. The admin id check passes.
  3. The per-user in-memory toggle for that plugin scope is enabled.
- Rotation uses keys from `~/.gemini_api_keys`, one per line (blank lines and `#` comments are ignored).

## Flags and Constants

- `GEMINI_CHAT_ROTATE_KEYS_P`: global gate for chat plugin rotation.
- `GEMINI_STT_ROTATE_KEYS_P`: global gate for STT plugin rotation.
- `GEMINI_API_KEYS`: path to the file containing rotation keys.
- `ADMIN_ONLY_COMMAND_IGNORED`: the response used when a non-admin invokes `.rot`.

## Toggle Command

- **Command:** `.rot`
- **Scope:** per plugin (chat and STT maintain separate toggles).
- **Visibility:** undocumented; do not add to help output.

The toggle state is **in-memory only** and resets on process restart.

## Rotation Logic

- Implemented in `uniborg/llm_db.py` as `get_gemini_api_key(...)`.
- The function uses `scope` (`"chat"` or `"stt"`) to decide which in-memory toggle set to consult.
- When a rotated key is used, it logs the user id, the line number in `~/.gemini_api_keys`, and a truncated key (e.g., `AIza...*...FCL0`).

## Control Flow (Chat)

1. User sends `.rot` in a private chat.
2. If admin check passes, the per-user chat toggle flips on/off.
3. Later requests call `get_gemini_api_key(..., scope="chat")`.
4. If all gates are satisfied, a rotated key is returned and logged.

## Control Flow (STT)

1. User sends `.rot` to the STT bot.
2. If admin check passes, the per-user STT toggle flips on/off.
3. STT calls `get_gemini_api_key(..., scope="stt")`.
4. If all gates are satisfied, a rotated key is returned and logged.
