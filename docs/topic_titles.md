# Automatic Topic Titles

In a bot's private chat with threaded mode on, a message typed in "All" makes
Telegram open a new topic for it, initially called "New Chat" or named after
the message, depending on the client. The chat bot renames such a topic twice:

1. **The badge, at once.** As soon as the bot starts answering the topic's
   first message, it puts the model's *badge* (its emoji and effort symbol)
   before the initial name, and sets the topic icon to the model's icon.
   `/setTitleModel` offers **Initial: New Chat** (the default) or
   **Initial: Question text**: `⚡◕ New Chat` or `⚡◕ what is a monad`.
2. **The title, after the answer.** Once the answer is delivered, it renames
   the topic to the badge and a short title, sets the title model's chosen
   topic icon:

       ⚡◕ Monads explained

After each rename, the bot schedules immediate deletion of its service message
for both participants. Flood-blocked cleanup retries independently, so it does
not hold up an answer or the final title. Telegram may display a notice until
its cooldown ends. The topic and its creation notice remain.

Topic operations use the shared Telegram queue (`docs/telegram_queue.md`).
Topic edits are paced across the client and explicit flood rejections wait and
retry. If the final title is ready while the initial badge rename is queued,
the final title supersedes that initial rename. Before each delayed attempt,
the bot checks the topic again and preserves an intervening manual rename or
deletion. Title generation runs once per attempt, not once per flood retry.

The name has three parts:

- the emoji of the model (`⚡` is Gemini Flash);
- a circle showing the reasoning effort (`◕` is high), left out for models
  without reasoning levels;
- the selected initial name, and then a title of at most
  six words, written by the user's title model (`/setTitleModel`,
  `docs/title_model.md`) from the first question and its answer.

The badge of the first rename uses the model and effort the request resolved
to; the second uses those the answer was sent with. They differ only when a
request falls back to another model. The whole name is cut to 128
characters, the Bot API's limit.

## Which topics are renamed

All of these must hold:

- The bot is a bot (not a user account), and the message is in one of its
  private topics (`docs/private_topics.md`).
- For the badge: the request passed the model and API-key checks. For the
  title: the answer was delivered. After an error, the Codex quota panel, a
  cancelled request or the "No response" notice, the topic keeps the badge
  and the initial name. An image-only answer counts; its title comes from the
  question alone.
- Telegram, not the user, named the topic: the topic carries `title_missing`.
  Every topic opened from "All" does. A topic the user creates with a name of
  their own should not, by the flag's meaning (the Bot API calls it
  `is_name_implicit`), though that case has not been observed.
- The answered message was sent within 10 minutes of the topic's creation
  (`NEW_TOPIC_WINDOW`). A topic typed in "All" opens with its first message,
  so this keeps older topics out. A Reserve re-run from the quota panel
  answers the original message, so it still counts.
- No other worker is currently naming it, and no completed title mark exists.
  The badge and final title share one in-process claim. Lookup, title-generation
  and terminal rename failures release that owned claim so a later answer can
  retry. Flood retries keep their claim. Requests that finish without scheduling
  a title abandon any queued badge operation and release their pending claim.

Only completed titles and intentional eligibility skips are persisted as
`borg:topic_titled:<chat>:<topic>`, with the long expiry (a month by default).
Legacy marks are preserved and treated as completed; historical failed topics
are not automatically replayed. Pending work and its claims live in memory.
Without Redis, the newest 4096 completion marks live in memory too.

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

## Topic icons

After the answer, the title model chooses an icon matching the conversation
from the supported emoji list supplied in its prompt. The model and effort
badge in the name is kept. An empty or unsupported choice keeps the model's
icon; an unavailable icon list leaves the icon alone. This uses the same
title-model request, with no extra model call.

A bot cannot have Telegram Premium, so the only icons it may set are
Telegram's 112 default topic icons (`inputStickerSetEmojiDefaultTopicIcons`,
the Bot API's `getForumTopicIconStickers`); any other custom emoji needs
Premium. `TopicIcons` loads that set once per process and finds an icon by
its emoji. A model whose emoji is in the set uses it (⚡, 💎, 🤖); the others
use a stand-in (`TOPIC_ICON_STAND_INS` in `uniborg/llm_models.py`):

- 🪶 Flash Lite: 💡
- 🌩️ Gemini 2.5 Flash: ⛅
- 💥 Gemini 3 Flash: 🔥
- 🌞 and ☀️ Sol: ⭐
- ✨ Astra: 🔭
- 🌙 Luna Reserve and 🌕 Luna: 🔮
- 🐋 and 🐳 DeepSeek: 🐟
- 🌬️ Mistral Medium: 💬
- 🧙 Magistral: 🎩
- 🖼️ Pixtral: 🎨
- 🔷 Codex custom ids: 💻
- 🧭 Pioneer: 🧪
- ♊ Gemini custom ids: 💎
- 🔀 OpenRouter custom ids: 🤖

A test checks every model's icon against the set as read on 2026-10-04.

## Effort symbols

The effort shows as a circle that fills as the level grows
(`REASONING_LEVEL_SYMBOLS`). Moon phases would say the same, but as emoji
they would clash with Sol's ☀️ and Luna's 🌕; plain symbols also leave the
model's emoji the one colorful mark in the title.

- `○`: none, and Gemini's `disable`
- `◔`: low (`.tl`)
- `◑`: medium (`.tm`)
- `◕`: high (`.th`)
- `●`: xhigh (`.tx`)
- `◉`: max (`.txx`)

A level without a symbol shows none. A topic is renamed only once, so topics
named earlier keep their old prefix: a letter (`⚡h`), or briefly a bar
(`⚡▆`).

## What Telegram does

Observed on a canary bot (Telethon 1.45, layer 229):

- `messages.editForumTopic` renames a private topic when given the topic id
  T from `reply_to_top_id`. The root's id gets `TOPIC_ID_INVALID`.
- Each rename posts a service message in the topic
  (`MessageActionTopicEdit`), which the user sees as the bot changing the
  topic's name. History does not record service messages, and thread context
  drops them, so the rename never reaches a prompt.
- The [rename API](https://core.telegram.org/method/messages.editForumTopic)
  has no option to suppress its service message. The bot removes the returned
  rename message using
  [deletion for all participants](https://core.telegram.org/method/messages.deleteMessages)
  (`revoke=True`).
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
- A bot sets a private topic's icon with `editForumTopic(icon_emoji_id=...)`,
  without Premium, given one of the default topic icons. A title and an icon
  in one request post one service message. A later rename without
  `icon_emoji_id` keeps the icon.
- The bot can delete its own rename service message
  (`messages.deleteMessages(revoke=True)` answered `pts_count=1`).

## Costs

- The first check of a topic in a process: one Redis `GET`. Completion uses
  `SET NX`; repeated checks are served by the in-process marks.
- Each new topic: an eligibility lookup, another `getForumTopicsByID` before
  each attempted rename, and for a qualifying topic up to two `editForumTopic`,
  two `deleteMessages` and one title-model request (with its Flash Lite fallback).
  Flood retries recheck eligibility; a superseded badge rename is omitted.
- Each process, once: one `getStickerSet` for the default topic icons.

Both renames run as background tasks, so a slow title model or a flood wait
never delays the answer. The badge's rename starts before the answer is
generated; final-title generation can overlap its cooldown, and the final
rename follows any in-flight initial rename. Icon loading is shared across
concurrent workers.

## Limits

- Permanent RPC errors and ambiguous network failures are logged and not
  retried automatically. Explicit flood rejections retry until accepted,
  superseded or cancelled. A later answer may retry released title work.
- Cleanup does not undo a successful rename. A notice can remain after a
  permanent deletion failure or when Telegram returns no service-message id.
- The eligibility recheck preserves a title that differs from the last one
  observed or applied by this worker. A manual edit racing the rename RPC itself
  cannot be made atomic with Telegram's API.
- Pending operations do not survive process restarts.
- Without Redis, a restart within 10 minutes of a topic's creation can rename
  it a second time.
- The STT bot does not rename topics.

## Code

- `uniborg/topic_titles.py`: the claim (`TopicTitleMarks`), the checks,
  `TopicBadge`, `TopicIcons`, the prompt, `compose_topic_title`,
  `prefix_new_topic` and `title_new_topic`, and their `schedule_` versions.
- `uniborg/llm_models.py`: `ModelSpec.emoji`, `model_emoji`,
  `TOPIC_ICON_STAND_INS`, `topic_icon_emoji`, `REASONING_LEVEL_SYMBOLS` and
  `reasoning_level_symbol`.
- `llm_chat_plugins/llm_chat.py`: `_start_topic_title`, called in
  `chat_handler` once the model and API key are settled, and
  `_schedule_topic_title`, called after the final delivery with the start's
  task; `_topic_badge`; `_topic_title_generator`, which uses the same title
  settings as file titles (`_title_settings`); and
  `GenerationResult.reasoning_level`, the effort that was sent.
- `UserPrefs.topic_initial_name`: the personal initial-name style, persisted
  through `UserManager` and selected in the `/setTitleModel` panel.
- Tests: `tests/test_topic_titles.py`, `tests/test_llm_models.py`,
  `TopicTitleHookTests` in `tests/test_llm_chat_topics.py`, and
  `TopicTitleHandoffTests` in `tests/test_delivery_golden.py`.
