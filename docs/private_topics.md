# Topics in Private Chats

What a bot's private-chat topics look like over MTProto, how uniborg keeps
its replies inside the right topic, how llm_chat gives each topic its own
conversation, and where that stops. The code is in `uniborg/topics.py`,
`uniborg/history_util.py` and `llm_chat_plugins/llm_chat.py`.

## Terms

- **Threaded mode**: the BotFather setting that gives a bot forum-style topics
  in its private chats. `User.bot_forum_view` reports it, and `.tgcaps` shows
  it as `private_topics`.
- **Private topic**: one topic in such a chat.
- **Topic root**: the `MessageService` with `MessageActionTopicCreate` that
  opens a topic. Its id is in the bot's message box.
- **Topic id**: the id Telegram puts in `reply_to_top_id` for every message
  of a topic. Below it is called T, and the root's id in the bot's box R.
- **"All"**: the client view that shows every message of the chat, outside
  any topic.
- **Placement**: choosing the topic a sent message lands in.
- **Registry**: `TopicRegistry`, a bounded in-memory map from (chat, message
  id) to the topic that message sits in, or to "outside every topic".
- **Recorded history**: what `history_util` stores for each chat in bot mode,
  one item per message the bot received or sent. It lives in Redis, or in
  memory when Redis is unavailable, and keeps at most 5000 items per chat
  (`HISTORY_LIMIT`).
- **Topic context mode**: Topic Thread, Reply Chain or Until Separator,
  resolved as topic setting > chat default for topics > Topic Thread.
- **Topic Thread (thread context)**: the topic's own recorded messages. See
  "Context inside a topic" below.

## What private topics look like on the wire

Observed live on a canary bot (Telethon 1.45, layer 229). Layer 224 has the
same fields.

- Creating a topic delivers the root R to the bot, with
  `MessageReplyHeader(forum_topic=True, reply_to_top_id=T)`.
- T is not in the bot's message-id space: it comes from the user's message
  box. So T is never the id of a message the bot could load or reply to.
- Every message in a topic carries
  `MessageReplyHeader(forum_topic=True, reply_to_msg_id=R, reply_to_top_id=T)`,
  even when the user replied to nothing. An explicit reply carries the
  parent's id in `reply_to_msg_id` and the same T.
- A message typed in "All" makes Telegram open a new topic for it: the bot
  receives a fresh root, then the message inside that topic. This happens for
  commands such as `/status` too. It is the client's and server's doing; the
  bot only sees the result and cannot prevent it. With placement (below), the
  bot's answer lands in that new topic, next to the command.
- A bot reply sent the way Telethon sends every reply,
  `InputReplyToMessage(reply_to_msg_id=<user message>)`, comes back with
  `forum_topic=False` and no `reply_to_top_id`: it lands in "All", outside
  the topic, even though its parent is inside one.
- The same reply with `top_msg_id=T` lands inside the topic and reads back
  with `forum_topic=True, reply_to_top_id=T`.
- `top_msg_id=R` does not work: the message lands outside, with no header.
  Replying to R without `top_msg_id` does not place it either. Forum
  supergroups differ on both counts: there the topic id is the root's own id,
  and a reply places a message by its parent alone.
- Bots cannot read history (`messages.getHistory` and
  `messages.getForumTopics` fail with `BOT_METHOD_INVALID`), but they can load
  single messages by id.
- Bots can read and rename a topic by T: `messages.getForumTopicsByID` returns
  its title, `title_missing` and creation `date`, and
  `messages.editForumTopic(topic_id=T)` renames it, posting a
  `MessageActionTopicEdit` service message in the topic. Given R, the first
  answers `ForumTopicDeleted` and the second `TOPIC_ID_INVALID`. A loaded
  message's `messages.Messages.topics` carries its topic too.
- A topic opened by typing in "All" is named after its message, cut short
  (`/status`, `Hi What's Bitcoi...`), and carries `title_missing`; every such
  topic on the canary did. The flag stays set after a bot renames the topic.

## Reply detection

Because every topic message has a reply header, `event.is_reply` is true for
all of them. `resolve_reply_target` and `is_real_reply` tell a real reply
from a header that only places a message in a topic: a header pointing at the
topic's root is not a reply. In a private chat only the parent's action shows
that it is the root, so the first plain message of a topic loads its parent
once, and `TOPIC_ROOTS` remembers the root. Placement's incoming recorder
(below) also files each root it sees arrive, so a topic opened while the bot
runs needs no load at all. `llm_chat` uses these for reply quotes, the
reply-chain walk, the smart-mode switch and group `mention_and_reply`.

## How placement works

`Uniborg` puts `TopicPlacementMixin` first in its bases, ahead of
`telethon_safety.DifferenceFallbackMixin` and `TelegramClient`. Both
`__call__` overrides run, placement outermost. Every Telethon send ends in
`await client(request)`, so one override covers `event.reply`,
`send_message`, `send_file` (single files and albums), the child chunks of
`util.edit_message`, `discreet_send`, history_util's patched sends, stt and
every other plugin, without touching a call site.

It looks at `SendMessageRequest`, `SendMediaRequest` and
`SendMultiMediaRequest`. `ForwardMessagesRequest` also has `reply_to` and
`top_msg_id`, but Telethon's `forward_messages` sets neither, so a forward
never has a reply target to place by. For each send:

- The peer must be a private chat with a user. Groups, channels and forum
  supergroups pass unchanged: supergroups already place a reply by its
  parent.
- A send without `reply_to` passes unchanged. Nothing says which topic it
  belongs to, so placement never guesses; it lands in "All".
- A `top_msg_id` the caller set is kept. A reply target that is not an
  `InputReplyToMessage` (a story reply), or that names another chat, passes
  unchanged.
- Otherwise the replied-to message is looked up in the registry. On a miss it
  is loaded once, by id, with `client.get_messages`, and filed. If it sits in
  topic T, the request's reply target is replaced by a copy with
  `top_msg_id=T`.

The registry is filled from three sources:

- **Incoming messages.** `TopicPlacementMixin._dispatch_update` files every
  private-chat message (new or edited) before any event handler runs, so a
  handler's reply finds its parent without a load. An event handler could
  not do this: Uniborg keeps handlers newest first, so a recorder registered
  at startup would run after every plugin's handler, too late for the reply.
  `_dispatch_update` is a private Telethon method, identical in 1.43.2 and
  1.45.0.
- **Sent messages.** After a send, each message Telegram echoes back in full
  is filed by its own header, which is Telegram's word on where it landed. A
  bare id (`UpdateMessageID`, `UpdateShortSentMessage`) is filed under the
  topic the send was placed in. So a reply to the bot's own message, such as
  the next chunk of a split answer, stays in the topic without a load.
  Scheduled sends are skipped, since their ids are a separate space.
- **Loads on a miss**, as above. A message that no longer exists is filed as
  outside every topic.

If Telegram refuses a placed send over its topic (`TOPIC_DELETED`, or any
other `TOPIC_*` error), the send goes out again without `top_msg_id`, and its
parent is filed as outside topics. Placement therefore never loses a message
that would have been sent without it. Other errors propagate as before.

Placement never raises into a send because of its own bookkeeping. A failed
load or a broken registry sends the request unchanged. The first problem is
logged as a warning, later ones at DEBUG, and all of them are counted in
`client.topic_placement.stats` (`placed`, `fetched`, `refused`, `failures`).

`Uniborg.create(topic_placement=...)` injects a configured `TopicPlacement`
(its registry, its root cache, its logger). Setting
`client.topic_placement = None` switches placement off; the mixin then
changes nothing.

## Context inside a topic

Each private topic has a **topic context mode**: Topic Thread (the default),
Reply Chain or Until Separator. Set it with `/contextModeHere` inside the
topic. Resolution is the topic's setting, then the chat's default for topics,
then Topic Thread. Personal modes, outside-topic chat modes and smart mode's
per-user switching state do not apply or change here. Last N is omitted
because it would duplicate Topic Thread with another limit; Smart is omitted
because its switching state is per user.

- **Topic Thread** uses the topic's own recorded messages, oldest first,
  up to the topic limit (200 by default). A message containing only `---`
  does not cut it, is left out of the thread and gets an explanatory reply.
  That reply points to Until Separator or a new topic to start fresh.
- **Reply Chain** follows only explicit replies through `resolve_reply_target`.
  A plain message gets just itself: its implicit header pointing at the
  topic root is not a reply. Explicit chains can leave the topic and stop
  below the root of the topic they reach. The topic limit does not cap this
  mode; the existing `HISTORY_MESSAGE_LIMIT` applies. Twins are kept. A
  `---` gets an explanation that a plain message already starts fresh.
- **Until Separator** reads the topic's own recorded messages after its
  latest `---`, still capped at the topic limit. A separator in another
  topic or in All does not cut it. Cap and cut both keep a suffix, so their
  order gives the same result: a separator older than the newest N items
  leaves the whole capped window. Separators sent before switching modes
  count too. Deleting a recorded separator reopens the earlier context.
  A `---` gets “Context cleared in this topic” and never reaches the model.

The topic limit is personal: `/setThreadLastN N`, `/setThreadLastN reset`
and `/getThreadLastN` set, clear and show it. `/contextMode` opened inside a
topic has quick picks. It caps Topic Thread and Until Separator, separately
from the outside-topic Last N limit (`docs/llm_chat_last_n_context.md`).

In Topic Thread and Until Separator, **Include Reply Chain** merges the
trigger's explicit chain into the window. It can restore messages older than
the cap or separator, including messages from another topic. It is one
chat-wide setting, falling back to the user's personal value; Reply Chain
mode ignores it. These window modes skip twin files, while an explicitly
included chain keeps them (`docs/twin_files.md`). Files, albums, media,
reactions and metadata use the same conversion as every other mode.

Bots cannot read history, so these windows use recorded ids filed with
`topic_id`, loaded in one batch. A loaded message's own header has the last
word: content from another topic and service messages are dropped. Recording
costs nothing per answer; loading the entire chat and filtering each answer
would cost up to 50 `getMessages` calls for 5000 ids.

`/status`, `/getContextModeHere` and the `/contextModeHere` menu show the
effective topic mode and its source. The latter menu's Apply-to row switches
between **This Topic** and **Whole Chat**. Whole Chat sets the default for
all topics without their own setting, never the outside-topic chat mode.
The `/contextMode` menu explains its outside-topic scope and offers topic
limit picks. A callback finds the menu's topic from the registry or by
loading the menu message once after a restart.

`/asfile` (and `..`) exports the topic's context in its selected mode. In a
Reply Chain topic, reply `/asfile` to the last message to export its chain;
a plain command has no earlier context. The file replies to the command so
it lands in the topic, and warnings reply to the file. Outside topics the
file is sent without a reply, as before.

## Limits

- **Sends without a reply stay in "All".** `event.respond(text)` and
  `send_message(chat, text)` with no `reply_to` are not placed. A call site
  that must stay in the topic has to reply, for example
  `event.respond(text, reply_to=event.id)` or `event.reply(text)`.
- **Typing is not placed.** `SetTypingRequest` has `top_msg_id`, but
  `client.action(chat, ...)` carries no reply target, and guessing the topic
  from the chat's latest message goes wrong as soon as the user has two
  topics going. So typing and upload indicators are left chat-wide.
- **Memory only.** The registry lives in the process. After a restart, the
  first reply to an older message costs one `getMessages` call; replies to
  messages that arrive after the restart cost none. The registry keeps
  16384 messages across all chats (`TOPIC_REGISTRY_SIZE`). That load goes
  through Telethon's usual flood handling, so while `getMessages` is under a
  short flood wait (up to `flood_sleep_threshold`), the send waits with it.
- **Single requests only.** A list of requests passed to `client(...)` at
  once goes out unchanged. Nothing in this repo sends lists.
- **Not placed:** `SendInlineBotResultRequest` (a user account sending an
  inline result), and drafts. In a private topic a draft is identified by its
  `random_id`, the chat and `top_msg_id` (see `docs/telegram_ai_apis.md`,
  section 2.1); when draft streaming is adopted it should take the topic from
  the registry too.
- **Thread context holds only what the bot recorded.** A message the bot
  never received or sent through the recorded paths is missing. Items
  recorded before topics were recorded have no topic, so a topic already
  running when this shipped starts its thread at the first message after
  that. The thread is also capped twice: at the topic limit, and by the
  5000 items the recorded history keeps per chat across all its topics.
- **Without Redis, threads do not survive a restart.** The recorded history
  then lives in memory, so a restart empties every thread; the next message
  in a topic starts with no earlier context.
- **Deleted messages.** A message Telegram reports as deleted is skipped. One
  deleted without the bot being told fails to load and is dropped.
- **Bots and private topics only.** A user account keeps its context mode.
  Topics in forum supergroups keep the chat's group context mode: their
  header differs (a plain message there has no `reply_to_top_id`), and a bot
  with privacy mode on does not see every message there, so a recorded
  thread would have gaps.
- **Outside topics, history still spans the chat.** Last N, until separator
  and the `.s` prefix's recent mode read the whole chat's recorded history,
  topic messages included, exactly as before. Topic Thread and Until Separator
  inside a topic read only that topic's ids. `.s` inside a topic still uses
  its chat-wide recent override, ahead of every topic mode.
- **Pending input follows its topic.** A *pending input flow* is a prompt
  that waits for the user's next message: a custom model id after
  `/setmodel` or `/setmodelhere`, a new system prompt, or a numbered menu on a
  user account. `llm_chat.start_input_flow` records the private topic of the
  command that asked, and `pending_input_flow` accepts an answer from that
  topic only. A message anywhere else, including one typed in "All" (which
  opens a new topic), is handled as a normal message and leaves the prompt
  pending. Commands that reset pending input, such as `/start` and `/help`,
  reset it everywhere. A prompt asked for outside topics takes its answer
  from anywhere, as before.
- **The cost of that choice.** A user who stays in "All" cannot answer a
  prompt by typing there: each such message opens a new topic. They have to
  open the prompt's topic (or, untested, reply to the prompt). The flow also
  has no expiry, so it can wait in its topic until a much later message there
  is taken as the answer. The model pickers' `❌ Cancel` button drops their
  prompt from wherever it is pressed, since it matches the menu message, not
  the topic.
- **API key prompts stay chat-wide.** `llm_db.request_api_key_message` sends
  its prompt without a reply target, so it lands in "All", outside every
  topic, and the next text message in any topic is taken as the key. A
  message that does not match the service's key format is refused and never
  stored.

## Per-topic settings

A private topic can have its own model, reasoning effort, system prompt and
context mode. For model, effort and prompt a request uses the first one set, in this order:

1. a message prefix (`.f`, `.th`, ...), for that message only;
2. this topic's setting;
3. the chat's setting (`/setModelHere` and the like);
4. the user's personal setting;
5. the default.

A *topic layer* means step 2: the settings stored for one topic. Reasoning
effort is kept per model, in the topic as in the chat, so a topic's `high`
for one model says nothing about another model.

Context mode has its own order: topic setting > chat default for topics >
Topic Thread. There is no prefix or personal layer for it.

### Commands inside a topic

Inside a private topic, these commands write the topic layer by default:

- `/setModelHere`, its menu, and a custom model ID typed after it;
- `/setThinkHere` and its menu;
- `/contextModeHere` and its menu (Topic Thread, Reply Chain, Until Separator,
  and Not Set to inherit);
- `/setSystemPromptHere`. Without text it opens a menu that shows the
  current prompt and takes the new one as the next message in the topic
  (`clear` removes it, `cancel` stops). Outside topics it still answers
  with its usage line.

Each of these menus has an **Apply to** row, `📍 This Topic` and
`💬 Whole Chat`, with a check mark on the layer it writes now. Pressing the
other one redraws the menu for that layer and writes nothing yet. A custom
model ID or a prompt the menu is still waiting for moves to the new layer
too. For context mode, Whole Chat sets the default for every topic without
its own mode, leaving the outside-topic mode alone. The topic menu offers
no Last N picks; `/setLastNHere` still sets the outside-topic limit.

With an argument (`/setModelHere x/y`, `/setThinkHere high`,
`/setSystemPromptHere text`) the command writes the topic, and its reply
says how to reach the whole chat. `/resetSystemPromptHere` clears only the
topic's prompt.

`/getModelHere`, `/getSystemPromptHere`, `/getContextModeHere` and `/status`
report the topic layer inside a topic. `/getModelHere` names the effective
model and reasoning effort with each setting's source, then shows the saved
topic, whole-chat and personal model settings separately.
Effort is resolved for the displayed model: topic, whole chat, personal, then
model default. Models without a reasoning setting are identified as unsupported.
"Not set (inherit)" means that layer has no override; it can still use a
model from another layer. `/status` adds
lines named "In This Topic" and says "overridden in this topic" when the
topic's model wins.

The model picker contains two settings: the model and the selected model's
**Effort**. Each has its own checkmark. For example, a checked Sol model
and checked "Effort: Use Personal Default" mean Sol is selected and the
whole chat has no reasoning-effort override for Sol. They are compatible.
The Apply-to row has its own checkmark for the target layer.

`/getLastNHere` and `/setLastNHere` always describe outside-topic context for
the whole chat, even when sent inside a topic. Their replies there point to
`/getThreadLastN` and `/setThreadLastN`, which control the personal limit for
Topic Thread and Until Separator inside private topics.

Outside private topics (a chat without threaded mode, a group, a user
account) there is no Apply-to row, and the same flows and saved settings
apply. The model getter labels that scope "whole chat".

### Why the topic is the default target

In a threaded private chat every message sits in a topic, since a message
typed in "All" opens a new one. If the commands wrote the chat by default,
the topic layer would be reachable only through the Apply-to row, and a
topic is the narrower change: a wrong choice affects one conversation, not
every topic that inherits from the chat.

### Safety

- A press on a topic menu reads its topic from the topic registry, or loads
  the menu message once (`_thread_topic_id_for_display`). If the topic
  still cannot be told, the press is refused with an alert. It never falls
  back to writing the chat.
- Choosing a model for a topic is an explicit model choice, so it ends a
  Codex quota stand-in like any other (`docs/codex_quota_fallback.md`).

### Storage

`TopicManager` keeps the topic layer in its own store (purpose
`llm_chat_topics`), keyed `<chat id>:<topic id>`, apart from the chat
settings. Entries do not expire, and a deleted topic's entry stays; each is
a few fields. `TopicPrefs.context_mode` stores the topic mode;
`ChatPrefs.topic_context_mode` in `llm_chat_chats` stores the default for
topics. Unset fields are omitted, so existing JSON needs no migration.

### Limits

- Model, reasoning effort, system prompt and context mode have a topic
  layer. Last N limits, TTS and other chat settings stay chat-wide; the
  topic limit is personal.
- In a threaded chat, almost every message is in a topic, so the
  outside-topic chat context menu is practically unreachable. Its saved
  value remains visible in the Outside Topics status line. Old chat-mode
  menus inside topics are refused; send `/contextModeHere` again.
- `/setThink` (personal) ignores topics: its menu is for the chat's
  effective model, as before.
- The STT bot has no topic layer.

## Automatic titles

In a topic Telegram named, llm_chat puts the model's emoji and a circle for
the effort before Telegram's name as soon as it starts answering the first
message, and sets the model's topic icon. After the answer it renames the
topic to that badge and a short title. See `docs/topic_titles.md`.

## Related files

- `uniborg/topics.py`: reply detection, `TopicRegistry`, `TopicPlacement`
  and `TopicPlacementMixin`.
- `uniborg/uniborg.py`: composes the mixin into `Uniborg` and attaches a
  `TopicPlacement` in `Uniborg.create`.
- `uniborg/telethon_safety.py`: the difference fallback the mixin stacks on.
- `uniborg/history_util.py`: the recorded history, its `topic_id` field,
  `record_message` and `get_last_n_topic_ids`.
- `uniborg/topic_titles.py`: automatic titles for new topics.
- `llm_chat_plugins/llm_chat.py`: `start_input_flow` and
  `pending_input_flow`, which bind pending input to its topic; topic
  modes (`TOPIC_CONTEXT_MODES`, `TOPIC_UNTIL_SEPARATOR_MODE`,
  `_get_effective_topic_context_mode`, `_topic_context_mode_menu`,
  `TOPIC_CONTEXT_CALLBACK_PREFIX` and `build_conversation_history`); and the topic layer (`TopicManager`,
  `REASONING_SCOPE_TOPIC`, the Apply-to row and `_apply_to_press_handler`,
  the prompt menu, `retarget_menu_input_flows`).
- `tests/test_topics.py`: placement per request type, the registry, the
  result shapes, refusals, the composed client on a fake transport, and
  golden sends that must go out byte for byte outside private topics. Run it
  under both Telethon versions.
- `tests/test_history_topics.py`: topic recording, old items without a
  topic, and the stored form through a fake Redis.
- `tests/test_llm_chat_topics.py`: reply detection as `llm_chat` uses it;
  topic context (`TopicContextModeResolutionTests`, `TopicReplyChainTests`,
  `TopicUntilSeparatorTests`), status and modes outside topics; and the topic layer
  (`TopicSettingsResolutionTests`, `TopicSettingsMenuTests`).
- `tests/test_llm_chat_awaited_input.py`: pending input in topics.
- `docs/telegram_ai_apis.md`, section 2.4: the Bot API side of private
  topics.
