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
- **Thread context**: the conversation context llm_chat uses for a message
  in a private topic, built from that topic's recorded history. See
  "Thread context" below.

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

## Thread context

Each topic is meant to be its own conversation, and a message in a topic
usually replies to nothing, so the reply chain would give it almost no
context. So inside a private topic a bot's llm_chat always uses thread
context, whatever the context mode: reply chain, until separator, last N,
smart mode and a chat's `/contextModeHere` setting all give way to it there.
Outside topics every mode works exactly as before.

What the thread holds:

- The topic's own messages, the user's and the bot's, oldest first: the
  latest N the recorded history holds for that topic, plus the message being
  answered. N is the Last N limit (`/setLastN`, `/setLastNHere` or the menu
  buttons, 100 by default; see `docs/llm_chat_last_n_context.md`).
- A message whose text is only `---` starts the thread afresh. The bot
  answers it with "Context cleared", and later messages in that topic see
  only what came after it. Smart mode's per-user state is neither switched
  nor used inside a topic.
- An explicit reply brings its reply chain when "Include Reply Chain" is on,
  as in the other modes. A reply to a message inside the thread adds
  nothing; a reply to one older than the cap brings that message back.
- Files, media, albums, reactions and metadata go through the same code as
  in every other mode.

Where it comes from. Bots cannot read history (see above), so the thread
comes from the recorded history. Each item carries `topic_id`, which
`topics.private_message_topic_id` reads from the message's header. For the
bot's own sends it reads the message Telegram echoes back, or, when Telegram
answers with a bare `UpdateShortSentMessage`, the `top_msg_id` that placement
gave the request. Items outside topics are stored byte for byte as before,
without the field. The thread's ids are loaded in one batch by id, and each
loaded message's own header has the last word: a message that turns out to
sit elsewhere is dropped, and so are service messages such as the topic
root.

Recording the topic was chosen over filtering at read time. The alternative,
loading every recorded id of the chat and keeping those whose header names
the topic, would also cover history recorded before topics were, but it
costs up to 50 `getMessages` calls per answer (5000 ids, 100 per call).
Recording costs nothing per answer.

Where it shows. `/status` adds an "In This Topic" line. `/contextModeHere`
and `/getContextModeHere` report `Topic Thread` and name the mode that
applies outside topics, and the `/contextMode` menu notes that its choice
applies outside topics. A button pressed on such a menu carries no header, so
the menu's topic comes from the registry, or from loading the menu message
once after a restart.

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
  that. The thread is also capped twice: at the Last N limit, and by the
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
  topic messages included, exactly as before. Only thread context filters by
  topic, and `.s` sent inside a topic still uses its recent mode.
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
  is taken as the answer.
- **API key prompts stay chat-wide.** `llm_db.request_api_key_message` sends
  its prompt without a reply target, so it lands in "All", outside every
  topic, and the next text message in any topic is taken as the key. A
  message that does not match the service's key format is refused and never
  stored.

## Related files

- `uniborg/topics.py`: reply detection, `TopicRegistry`, `TopicPlacement`
  and `TopicPlacementMixin`.
- `uniborg/uniborg.py`: composes the mixin into `Uniborg` and attaches a
  `TopicPlacement` in `Uniborg.create`.
- `uniborg/telethon_safety.py`: the difference fallback the mixin stacks on.
- `uniborg/history_util.py`: the recorded history, its `topic_id` field,
  `record_message` and `get_last_n_topic_ids`.
- `llm_chat_plugins/llm_chat.py`: `start_input_flow` and
  `pending_input_flow`, which bind pending input to its topic; and thread
  context (`THREAD_CONTEXT_MODE`, `_thread_topic_id`, and its branch in
  `build_conversation_history`).
- `tests/test_topics.py`: placement per request type, the registry, the
  result shapes, refusals, the composed client on a fake transport, and
  golden sends that must go out byte for byte outside private topics. Run it
  under both Telethon versions.
- `tests/test_history_topics.py`: topic recording, old items without a
  topic, and the stored form through a fake Redis.
- `tests/test_llm_chat_topics.py`: reply detection as `llm_chat` uses it,
  and thread context: what a thread holds, the mode it replaces, the status
  texts, and the modes outside topics.
- `tests/test_llm_chat_awaited_input.py`: pending input in topics.
- `docs/telegram_ai_apis.md`, section 2.4: the Bot API side of private
  topics.
