# Admin-Only Codex GPT-5.5

`llm_chat` exposes `openai-codex/gpt-5.5` as an admin-only model.
Non-admin users should not see it in `/setModel` or `/setModelHere`, and direct
selection attempts are rejected server-side.

Runtime requirements:

- Install `requirements.txt` in the same Python environment that runs
  `stdborg.py`.
- Run `codex login` for that runtime user so `~/.codex/auth.json` contains
  ChatGPT OAuth credentials.

The integration depends on the published `llm-openai-via-codex` pip package for
Codex OAuth token borrowing and refresh. Betterborg only owns the Telegram
message conversion, admin gating, and streaming response handling.

Reasoning effort uses the existing `/setthink` preference. Admins can select
`xhigh`; non-admin menus stay limited to the public levels.
