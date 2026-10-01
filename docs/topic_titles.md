# Automatic Topic Titles

In a bot's private chat with threaded mode on, a message typed in "All" makes
Telegram open a new topic for it and name the topic after that message. Once
the chat bot has answered the first message of such a topic, it renames the
topic once, to something like:

    ⚡h Monads explained

The name has three parts:

- the emoji of the model that answered (`⚡` is Gemini Flash);
- the short alias of the reasoning effort sent with the request (`h` is
  high), left out for models without reasoning levels;
- a title of at most six words, written by the user's title model
  (`/setTitleModel`, `docs/title_model.md`) from the first question and its
  answer.

The whole name is cut to 128 characters, the Bot API's limit.

## Which topics are renamed

All of these must hold:

- The bot is a bot (not a user account), and the message is in one of its
  private topics (`docs/private_topics.md`).
- The answer was delivered. Errors, the Codex quota panel, a cancelled
  request and the "No response" notice rename nothing. An image-only answer
  counts; its title comes from the question alone.
- Telegram, not the user, named the topic: the topic carries `title_missing`.
  Every topic opened from "All" does. A topic the user creates with a name of
  their own should not, by the flag's meaning (the Bot API calls it
  `is_name_implicit`), though that case has not been observed.
- The answered message was sent within 10 minutes of the topic's creation
  (`NEW_TOPIC_WINDOW`). A topic typed in "All" opens with its first message,
  so this keeps older topics out. A Reserve re-run from the quota panel
  answers the original message, so it still counts.
- No earlier answer in the topic claimed it. The first answer to reach the
  check claims the topic whether or not it turns out to qualify, so each
  topic is checked once and renamed at most once.

The claim is a Redis key, `borg:topic_titled:<chat>:<topic>`, kept for the
long expiry (a month by default), so a restart does not rename a topic twice.
Without Redis the claims live in memory (the newest 4096).

## Model emoji

These are set by `emoji=` on each `ModelSpec` in `uniborg/llm_models.py`;
change them there. A test requires the registered models' emoji to be
distinct.

- ⚡ Gemini Flash (Latest), `gemini/gemini-flash-latest`
- 🪶 Gemini Flash Lite (Latest), `gemini/gemini-flash-lite-latest`
- 🌩️ Gemini 2.5 Flash
- 💥 Gemini 3 Flash
- 💎 Gemini 3 Pro
- 🌞 GPT Sol Latest (OpenRouter), `openrouter/~openai/gpt-sol-latest`
- ☀️ GPT Sol (Codex), the newest; named after its model, such as GPT-6.1 Sol
- ✨ GPT Astra (Codex), the newest
- 🌙 Luna Reserve (Codex)
- 🌕 GPT Luna (Codex), the newest
- 🐋 DeepSeek Chat
- 🐳 DeepSeek Reasoner
- 🌬️ Mistral Medium
- 🧙 Magistral Medium
- 🖼️ Pixtral Large

A custom model id gets its provider's emoji (`_synthesized_spec`): 🔷 Codex,
🧭 Pioneer, ♊ Gemini, 🔀 OpenRouter, and 🤖 for anything else.

## Effort aliases

The aliases are the suffixes of the `.t` effort prefixes, which are built from
the same map (`REASONING_LEVEL_ALIASES`), so `h` in a title is what `.th`
asks for:

- `n`: none, and Gemini's `disable`, which has no prefix of its own
- `l`: low
- `m`: medium
- `h`: high
- `x`: xhigh
- `xx`: max

A level without an alias is shown in full.

## What Telegram does

Observed on a canary bot (Telethon 1.45, layer 229):

- `messages.editForumTopic` renames a private topic when given the topic id
  T from `reply_to_top_id`. The root's id gets `TOPIC_ID_INVALID`.
- Each rename posts a service message in the topic
  (`MessageActionTopicEdit`), which the user sees as the bot changing the
  topic's name. History does not record service messages, and thread context
  drops them, so the rename never reaches a prompt.
- `messages.getForumTopicsByID` works for a bot and returns the topic with
  its current title, `title_missing` and `date`, the creation time.
- Every topic opened by typing in "All" carried `title_missing`, with the
  message as its name, cut short (`Hi What's Bitcoi...`).
- `title_missing` stays set after the bot renames the topic. It says how the
  topic was opened, not whether it was renamed since, which is why the claim
  is kept separately.
- Ten renames in a row drew no flood wait.
- A 129-character title was accepted, though the Bot API documents 128 as the
  limit; names are cut to 128.

## Costs

- Every answer in a private topic: one Redis `SET NX`.
- Each new topic, once: one `getForumTopicsByID`, and for a qualifying
  topic, one title-model request (with its Flash Lite fallback) and one
  `editForumTopic`.

The rename runs as a background task after the answer is delivered, so a slow
title model or a flood wait never delays the answer.

## Limits

- A failed rename is not retried: the topic was claimed, and keeps
  Telegram's name. Failures are logged.
- If the user renames a topic before its first answer arrives, the bot may
  still rename it: whether a user's rename clears `title_missing` has not
  been observed.
- Without Redis, a restart within 10 minutes of a topic's creation can rename
  it a second time.
- The STT bot does not rename topics.

## Code

- `uniborg/topic_titles.py`: the claim (`TopicTitleMarks`), the checks,
  the prompt, `compose_topic_title`, `title_new_topic` and
  `schedule_title_new_topic`.
- `uniborg/llm_models.py`: `ModelSpec.emoji`, `model_emoji`,
  `REASONING_LEVEL_ALIASES` and `reasoning_level_alias`.
- `llm_chat_plugins/llm_chat.py`: `_schedule_topic_title`, called after the
  final delivery in `chat_handler`; `_topic_title_generator`, which uses the
  same title settings as file titles (`_title_settings`); and
  `GenerationResult.reasoning_level`, the effort that was sent.
- Tests: `tests/test_topic_titles.py`, `tests/test_llm_models.py`,
  `TopicTitleHookTests` in `tests/test_llm_chat_topics.py`, and
  `TopicTitleHandoffTests` in `tests/test_delivery_golden.py`.
