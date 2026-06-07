# LLM Chat Prompting

`llm_chat` always appends a short context-metadata instruction to the effective system prompt. Injected sender metadata, reply quotes, media IDs, reaction summaries, and runtime context are model-visible grounding data, not wording or formatting to copy into final replies.

This instruction is appended for default, user-custom, and chat-custom system prompts so custom prompts do not accidentally make the model echo metadata labels such as `[Replying to ...]`.
