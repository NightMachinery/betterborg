# Telegram AI-Bot APIs

A maintainer reference for the Telegram features that matter to the chat bot
(`llm_chat_plugins/`) and the transcription bot (`stt_plugins/`): what exists,
how it maps onto MTProto and Telethon, what breaks, and which integration route
this repo chose.

## 1. Scope and date

Current as of 2026-09-29.

- **Bot API 10.3** (2026-08-24) is the latest Bot API version.
- **MTProto layer 229** is the latest layer in tdesktop, TDLib 1.8.67,
  Telegram Android and Telethon 1.45.0. The MTProto pages on
  core.telegram.org still stop at layer 225, so pages for newer constructors
  are missing or stale.
- **A layer number does not pin the wire format.** Telegram re-edits a layer
  in place. Within layer 229, `sendMessageTextDraftAction` changed from
  `#376d975c` to `#3630b85a`. Use the latest schema revision of a layer, which
  Telethon 1.45.0 ships.

Relevant Bot API releases:

- 9.3 (2025-12-31): `sendMessageDraft` (topic-mode bots only at first) and
  topics in private chats.
- 9.4 (2026-02-09): button `style` and icon, `createForumTopic` in private
  chats, custom emoji for bots whose owner has Premium.
- 9.5 (2026-03-01): the `date_time` entity, and `sendMessageDraft` for all
  bots.
- 10.0 (2026-05-08): guest mode, bot-to-bot messages by username, business bots
  without Premium, and empty draft text showing "Thinking…".
- 10.1 (2026-06-11): rich messages.
- 10.2 (2026-07-14): rich messages built from explicit blocks, and ephemeral
  messages.
- 10.3 (2026-08-24): the Stop button for drafts, buttons inside rich messages,
  `DisabledButton`, and `force_reply` on inline keyboards.

Terms used below:

- **Edit streaming**: the classic path. Send a placeholder message, then edit
  it repeatedly as the answer grows.
- **Draft streaming**: Telegram's native live drafts, a temporary preview that
  the bot must replace with a real message at the end.
- **Finalize**: the real send that ends draft streaming.
- **Adoption**: a client replacing a visible draft with the final message in
  place.
- **Rich message**: a message whose body is Markdown or HTML parsed by
  Telegram's server and stored as blocks, instead of text plus entities.
- **Classic formatting**: text plus message entities, as `parse_mode`
  produces.
- **Guest mode**: the feature that lets a bot answer where it was mentioned,
  in a chat it is not a member of. The update it receives is a **guest query**,
  the message that mentioned it is the **trigger**, and its single reply is the
  **guest answer**.
- **Layer leak**: the server sending a constructor newer than the layer the
  client declared.

Evidence tags. Untagged statements about Telegram are documented by Telegram
or read in its published TL schema. Untagged statements about Telethon were
read in its code (the 1.43.2 install and the 1.44.0 and 1.45.0 wheels). Lines
starting with "Decision:" record this repo's choices. The tags are:

- **[src]**: read in TDLib, telegram-bot-api, tdesktop or Android source code,
  but not documented.
- **[field]**: reported by third-party bots running in production.
- **[untested]**: an inference that still needs a live test (section 5).

## 2. Feature inventory

### 2.1 Draft streaming

The API:

- Bot API `sendMessageDraft(chat_id, message_thread_id?, draft_id, text,
  parse_mode | entities, can_stop, keep_on_stop)`. `text` is 0 to 4096
  characters, and empty text shows a "Thinking…" placeholder.
  `sendRichMessageDraft` takes a `rich_message` instead of text.
- Over MTProto this is `messages.setTyping(peer, top_msg_id?, action)` with
  `sendMessageTextDraftAction{can_stop, keep_on_stop, random_id, text:
  TextWithEntities}` [src]. The Bot API `draft_id` is the MTProto `random_id`,
  and `message_thread_id` is `top_msg_id`. Rich drafts use
  `inputSendMessageRichMessageDraftAction`, with the same flags.
- An update with the same `random_id` animates the draft. A new `random_id`
  replaces it without animation.

Rules:

- **Private chats only.** Elsewhere `messages.setTyping` returns
  `TEXTDRAFT_PEER_INVALID`. So there is no draft streaming in groups, in guest
  answers, or (through the Bot API) in business chats. `messages.setTyping`
  itself works over a business connection, so MTProto drafts in business chats
  are [untested] rather than ruled out.
- **A 30 s preview.** The server config `message_typing_draft_ttl` is 30.
  The config page says clients should delete a draft 30 s after its last
  update, or when a normal message arrives in the same chat or topic. Real
  clients are less tidy: tdesktop hard-codes the 30 s and keeps a draft that a
  new message fails to adopt (see below) [src]. A draft is never persisted, so
  the bot must finalize.
- **Quiet.** A draft causes no notification, no unread badge and no
  "typing…" indicator [src].
- **Rate limits.** At most 20 calls in 5 s and 40 calls in 30 s per peer, plus
  short cool-down FLOOD_WAITs of at most 3 s even below the limit. The budget is
  shared by every `messages.setTyping` call to that peer, including Bot API
  `sendChatAction` and this repo's `borg.action()` typing loops.
- **Topics.** In private-chat topics a draft is identified by its
  `random_id`, the chat and `top_msg_id`, and shows only in that topic.

The Stop button (Bot API 10.3, layer 229):

- `can_stop=True` shows a Stop button. When the user presses it, the bot
  receives `updateUserTyping(user_id, top_msg_id?,
  action=sendMessageStopDraftAction(random_id))`. The Bot API calls this
  `stopped_message_generation`, and serializes its `draft_id` as a JSON string
  despite documenting an integer [src].
- The update carries no pts or qts, so a dropped one is simply lost.
- After Stop, clients ignore any further draft with that `random_id` [src].
- `keep_on_stop=True` keeps the draft on screen until it expires or a message
  replaces it. To keep the partial text for good, send it as a real message.

Client adoption rules [src]. This is the part most likely to go wrong:

- Clients keep one draft per thread and sender.
- **tdesktop needs a common prefix.** It adopts the final message only when it
  comes from the same sender, in the same thread, and shares at least one
  leading character with the last draft's text. An empty "Thinking…" draft, a
  status-line draft, or a draft that shows only the tail of a long answer fails
  this test. The draft then sits next to the real message for up to 30 s, still
  showing its Stop button. The fix is a **sync draft**: just before
  finalizing, send and await one draft whose text equals the first chunk of the
  final message.
- **Android blocks the send button.** It adopts only when the other side is a
  bot and the chat is open. While any bot draft is alive, it disables the
  user's send button (or shows only Stop), so a lingering draft blocks the user
  for up to 30 s.
- **Late drafts become ghosts.** A draft update that reaches a client after
  the final message shows as a new draft for 30 s. Telethon's FloodWait retry
  (section 3.5) produces exactly this, so cancel the draft worker before
  finalizing.
- **Do not reuse the draft's `random_id` for the final send.** Recipients never
  see a message's `random_id`; adoption is purely heuristic.

Heartbeats: clients restart the 30 s timer on every update, so re-sending the
draft keeps it on screen [src]. Whether the server relays byte-identical
repeats is [untested], so append a changing elapsed timer. The gap between
draft sends must stay well under 30 s. This repo's edit-streaming slow mode
waits up to 60 s between edits, so draft streaming needs its own heartbeat
instead of reusing that cadence.

Userbots: the MTProto docs say users can also send drafts to users. Server
acceptance is [untested]. Android never adopts a final message from a user
account, yet it still blocks the recipient's send button while the draft
lives [src]. Userbot plugins keep edit streaming.

Decision: the chat bot streams drafts in private chats by default, with the
sync draft, heartbeats, a cancellable worker and a call timeout described
above; groups and the userbot keep edit streaming. The code is
`uniborg/draft_stream.py`, and the behavior and the `/stream` setting are in
[draft_streaming.md](draft_streaming.md).

### 2.2 Rich messages ("full Markdown")

What they are:

- A new message type (Bot API 10.1, MTProto layer 227), not a new parse mode.
  The body is a tree of blocks that reuses Instant View's `PageBlock` and
  `RichText` types, plus new ones such as headings, math, thinking and button
  rows.
- You send an `InputRichMessage` with exactly one of `markdown`, `html` or
  `blocks` (blocks since 10.2).
- **The server parses the Markdown.** TDLib forwards the source string
  unchanged [src], so any MTProto client can send an LLM's Markdown as is. The
  docs call it "compatible with GitHub Flavored Markdown where possible and can
  contain arbitrary HTML".
- Supported: headings, tables with alignment, nested lists and task lists,
  footnotes, inline and block LaTeX (`$…$`, `$$…$$`, fenced `math`), fenced code
  with a language, `==mark==`, `||spoiler||`, `<details>`, media blocks, maps,
  collages and (10.3) buttons. `<tg-thinking>` works only in rich drafts.
- Parser rules worth knowing: table cells hold inline formatting only; media
  must be its own block, and only http(s) URLs are fetched, by the server;
  Markdown is not parsed inside block HTML tags other than `<details>`,
  `<tg-collage>` and `<tg-slideshow>`. URL and mention detection is on unless
  `skip_entity_detection` (MTProto flag `noautolink`) is set.

Limits:

- 32768 UTF-8 characters (whether that means bytes or characters is
  [untested]), 500 blocks counting nested blocks, list items and table rows,
  16 nesting levels, 50 media and 20 table columns.
- Clients fold long messages behind "Show more" after roughly 8000 characters.
- For comparison, classic messages allow 4096 characters of text and 1024 of
  caption.

Sending:

- Bots can send rich messages. Users need Premium (the rich text editor is
  Premium-only), and TDLib marks the Markdown and HTML inputs "for bots only",
  so userbot plugins cannot rely on them. Even for Premium users, the tdesktop
  editor sends classic text when the content is simple [src].
- Over MTProto, pass `message=''` plus `rich_message` to `messages.sendMessage`;
  the same field exists on `messages.editMessage`,
  `messages.editInlineBotMessage` and `messages.saveDraft`, and guest answers
  use `inputBotInlineMessageRichMessage` (section 3.1).
- The send may be answered with `updateShortSentMessage`, which has no
  `rich_message` field, so Telethon builds a contentless `Message` from it.
  Which update the server actually returns is [untested]. Record the sent
  message by its id and never read content off the returned object.

Reading rich messages back:

- **The text is empty.** A rich message has `message.message == ''` and no
  entities. There is no server-generated plain-text fallback [src].
- **The source is gone.** `richMessage` holds only blocks, photos and
  documents. Telegram never returns the Markdown or HTML that was sent [src].
  A bot that needs its own answer text later (history, quoting, export) must
  store the source itself, keyed by chat and message id.
- **It may be truncated.** `richMessage.part` marks a partial message, and
  `messages.getRichMessage(peer, id)` returns the full one [src]. History
  fetches do return partial messages, and the size threshold is undocumented.
  The Bot API hides this flag and never fetches the full message.
- **Nothing converts blocks back to text.** No server, TDLib or Bot API
  function does it, and the client flatteners are lossy (tdesktop drops table
  cells, Android keeps rows as `a | b`) [src]. The blocks keep code language,
  LaTeX source, list and checkbox state and table cells, so a near-lossless
  blocks-to-Markdown converter is possible. Both clients are GPL, so port the
  approach, not the code.
- **Commands and mentions can sit inside rich text** (`textBotCommand`,
  `textMention`). Trigger and command detection must run on flattened text.
- The rich text editor pads empty paragraphs with U+200B [field]. Strip only
  paragraphs that consist of a lone U+200B: this repo's meta-info prefix is
  four U+200B characters and must survive.
- A reply to a rich message gets no automatic quote, because TDLib treats rich
  messages as non-text [src]. Manual quoting is [untested].

Editing between plain and rich:

- TDLib accepts edits from plain to rich and back, and the Bot API describes
  `editMessageText` as editing "text, rich and game messages" [src]. A
  production bot finalizes plain streamed previews into rich messages in place
  [field]. No first-party server document states it, so keep "send a new
  message, delete the placeholder" as the fallback.

Incoming rich messages from Premium users:

- On layer 227 and later they arrive as `message=''` plus `rich_message`.
- On layer 224 (Telethon 1.43.2) they arrive as `message=''` plus
  `MessageMediaUnsupported`, with the content lost [field]. The server
  downgrades them instead of leaking a newer constructor. Code on 1.43.2 must
  treat that combination as an unreadable rich message, not as media to
  download.

Classic formatting barely changed. The only new entity is `date_time`
(section 2.6); parse modes, nesting rules and limits are as before. Note that
Telethon's own `md` parser is not GFM: it leaks a fenced block's language into
the code text, leaves `*italic*` literal and mis-parses `a**b**c` [src].

### 2.3 Guest mode (the Mira mechanism)

What it is:

- Bot API 10.0, MTProto layer 225. A user writes `@bot …` in any non-secret
  private chat, group or supergroup, including chats the bot is not in. Groups
  and supergroups with content protection are excluded, and channels are not
  listed. Up to 3 guest bots can be mentioned in one message. No Premium
  requirement for callers is documented.
- The bot answers as itself. The answer is an ordinary, persistent message
  that everyone in the chat sees; tdesktop labels it "for {caller's first
  name}" [src].
- It differs from inline mode, where the user sends the result as their own
  message "via @bot", and from business bots, which reply as the user.
- This is the mechanism behind the "mention it in any chat" UX of @mira (by The
  Open Platform). That attribution rests on its own documentation and release
  timing; its guest flag was not read directly.

Enabling:

- Toggle "Guest Mode" in the BotFather Mini App. There is no slash command and
  no API method to toggle it.
- The flag shows as Bot API `getMe().supports_guest_queries` and MTProto
  `user.bot_guestchat`. Telethon 1.45's `get_me()` should expose it
  [untested].
- Users have no privacy setting that blocks guest bots.
- The server finds the mention in plain text; the client sends no guest flag
  [src]. So any account, this repo's userbot included, can summon a guest bot
  by sending "@bot …".

Receiving:

- The bot gets `updateBotGuestChatQuery(query_id, message, reference_messages?,
  qts)`. It is qts-sequenced, so it takes part in gap recovery, and in
  Telethon's gap bug (section 3.5).
- It is not an `UpdateNewMessage`, so `events.NewMessage` never fires. Handle
  `events.Raw(UpdateBotGuestChatQuery)`. The embedded messages are not bound to
  the client, so call `_finish_init` on them or use `client.download_media(msg)`
  [src].
- **Context is the direct parent only.** `reference_messages` holds the message
  the trigger replies to, and nothing deeper [src]. The bot sees no history and
  no participant list, and gets no later updates unless it is mentioned or
  replied to again.
- **Albums** [field, 2026-10-02]. A reply to one item of an album brings one
  reference, that item, with its `grouped_id`; the album's other items are
  not sent. An album sent as a reply to a guest answer brings one query per
  item, all in the same second, each with the answer as its reference.
  Untested: an album whose caption mentions the bot, sent as no reply.
- **Every reply to the bot's guest answer re-triggers it**, with or without a
  mention [field]. A chat bot can use that for continuation. A transcription
  bot should act only on an explicit mention with media in the trigger or its
  parent, and stay silent otherwise.
- If the bot is also a member of the chat, update shapes can overlap, so dedupe
  [field]. For a bot that also has inline mode, a mention at the very start
  ("@bot …") opens the inline popup, while one after the text should avoid it;
  which updates then arrive is [untested]. A bot banned in a chat cannot answer
  there.

Answering:

- **Exactly once**, via `messages.setBotGuestChatResult(query_id, result:
  InputBotInlineResult)`, which returns an `InputBotInlineMessageID`. The result
  uses the inline-result format; it may be rich
  (`inputBotInlineMessageRichMessage`) and may carry a keyboard. The Bot API
  requires a non-empty title.
- **Never retry.** A report from 2026-09-26 shows one answer posting about four
  copies before returning 429 [field]. Treat delivery as at most once, and
  dedupe by `query_id`, since qts updates can be redelivered after a restart.
- **Answering is optional.** An unanswered query expires silently [field].
- **The deadline is undocumented** and enforced only by Telegram's servers. An
  answer about 53 s after the query succeeded, and reports say 1 to 2 minutes
  can still work [field]. A late answer fails with `QUERY_ID_INVALID` ("query
  is too old and response timeout expired or query ID is invalid"). Answer with
  a placeholder before any slow work.

Streaming the answer:

- Edit the returned id repeatedly with `messages.editInlineBotMessage` (text,
  entities, reply markup, media or `rich_message`). Several production bots
  stream this way; one edits at most once per 1.2 s or per 80 new characters
  [field]. Edit rate limits are undocumented.
- **Send the edit to the data center named in the id's `dc_id`**, or Telegram
  answers `MESSAGE_ID_INVALID`. Telethon's `edit_message` does this routing but
  cannot send `rich_message` (section 3.4).
- Inline edits cannot upload new files, directly or by URL. Draft streaming is
  not available in guest chats.

Identity and addressing:

- **The caller is the trigger's sender** (`from_id`). `guest_bot_caller_user`
  and `guestchat_via_from` are set only on messages the bot itself posted, for
  example when its earlier answer comes back as a reference message [src,
  field]. There the answer has `from_id` equal to the bot, `guestchat_via_from`
  equal to its original caller, and no outgoing flag. A caller posting as a
  channel should have that channel as `from_id` [untested].
- **In a private chat, the peer id is not the caller.** It is the other
  participant as the caller sees them, so the value flips depending on who
  summons (seen live on the canary). Key private-chat state by the unordered
  pair {from_id, peer_id}, and group state by the chat id.
- **Never send to that chat id.** In a group the bot is not in, `sendMessage`
  fails ("bot was kicked"). In a private chat it succeeded but landed in the
  bot's own DM with another user instead of the chat where it was summoned,
  which is a privacy leak [field].
- The bot is never told the message id of its own answer, and in a private
  chat the two participants see different ids [field]. To recognise a
  continuation, carry a marker inside the answer, such as a deep-link token in
  a URL button. Whether `reply_markup` survives in `reference_messages` is
  [untested].
- Presses on a guest answer's callback buttons probably arrive as
  `updateInlineBotCallbackQuery`, but the routing is [untested]. Prefer URL
  buttons until a live test settles it.

Media:

- A guest bot can download media from the trigger or the replied-to message;
  Bot API bots do this in production for voice notes and images [field]. The
  Telethon route (`upload.getFile` with the same file reference) is
  [untested].
- **Download immediately.** A guest message's file reference cannot be
  refreshed: the bot cannot fetch messages from that chat, and TDLib keeps no
  refresh source for them [src].
- MTProto downloads have no 20 MB cap, unlike Bot API `getFile`.
- Attaching newly generated media (TTS audio, images) to a guest answer should
  work over MTProto by uploading with `messages.uploadMedia(InputPeerSelf)`
  first. From a bot, that upload returns a `MessageMediaPhoto` with a reusable
  photo and sends nothing [field, canary]; editing a guest answer into it is
  what the chat bot does (docs/guest_mode.md) but is [untested] live.

Seen live on a canary bot (Telethon 1.45.0, layer 229, 2026-09-29):

- An article result without a title fails with `ARTICLE_TITLE_EMPTY`.
- Answers took 0.04 to 0.11 s, and every returned inline id was on the home
  data center, in private chats and in a supergroup.
- Eleven plain edits about 1.25 s apart, then a rich final edit over the plain
  answer, hit no flood wait. The rich answer reads back as empty text with a
  `rich_message`.
- A private-chat trigger arrives with `out` set, and its message ids are local
  to the caller's side (1241353 on one side was 29 on the other). A group
  trigger has no `out` flag.
- **In a group, the bot receives its own guest answer** as a new outgoing
  channel message (`from_id` the bot, `guestchat_via_from` the caller,
  replying to the trigger), and an edit update for each streaming edit. No such
  echo arrived in a private chat. Handlers must ignore it.
- `download_media` on a 3.5 MB audio reference worked at once, in about 2.3 s.
- A bot that is a member of a group gets no guest query there, and with
  privacy mode on it does not see the mention either. So there are no
  duplicates, but a member bot is deaf to "@bot text" in that group.

Status: the shell, @vlm_chat_bot and @llm_stt_bot answer guest mentions, on
Telethon 1.45 with the qts re-dispatch and at-most-once safety nets. The flow,
safety rules and per-bot behaviour are in [guest_mode.md](guest_mode.md).

### 2.4 Topics in private chats

- Bot API 9.3 and 9.4: a bot with topic mode enabled in BotFather gets
  forum-style topics in its DMs. `message_thread_id` works in DMs for sends,
  copies, forwards and chat actions; `createForumTopic` works in a private
  chat; `ForumTopic.is_name_implicit` flags topics the bot should rename; a
  BotFather setting controls whether users may create or delete topics.
- MTProto: `user.bot_forum_view`, `bot_forum_can_manage_topics`,
  `messages.createForumTopic(peer: InputPeer)` and `top_msg_id`. All exist in
  layer 224 already.
- Seen live on the canary: a message typed in the All view opens a new topic;
  a bot reply lands in the topic only when it carries `top_msg_id` set to the
  topic id from the replied message's header (the root's own message id does
  not work, and neither does a reply to the root).
- Also seen live: `messages.editForumTopic` and `messages.getForumTopicsByID`
  work for a bot in its private chat with that same topic id, and refuse the
  root's id. A rename posts a `MessageActionTopicEdit` service message, and
  `title_missing` (MTProto's `is_name_implicit`) stays set after it.
- Status: bot replies are placed in their topic, pending inputs are bound to
  the topic that asked for them, each topic is its own conversation (thread
  context), and a topic Telegram named is renamed after its first answer.
  See [private_topics.md](private_topics.md) and
  [topic_titles.md](topic_titles.md).

### 2.5 Button styles

- 9.4: inline and reply buttons take a `style` ("danger" red, "success" green,
  "primary" blue) and an `icon_custom_emoji_id` (which needs a Premium bot
  owner or a Fragment username). MTProto `keyboardButtonStyle` is in layer 224,
  and Telethon 1.43.2 supports `Button.inline(text, data, style=..., icon=...)`.
- 10.3: `DisabledButton` (does nothing, so it can serve as a label) and
  `force_reply` on inline keyboards. Buttons inside rich messages add a "link"
  style.
- Layer 229 rewrote the keyboard constructors. That is the main Telethon 1.45
  break (section 3.3).

### 2.6 The `date_time` entity

- Bot API 9.5: `date_time` with `unix_time` and a `date_time_format` matching
  `r|w?[dD]?[tT]?` (`r` relative, `w` weekday, `d`/`D` short or long date,
  `t`/`T` short or long time). Clients render it in the reader's time zone.
- Syntax: MarkdownV2 `![22:45 tomorrow](tg://time?unix=1647531900&format=wDT)`,
  HTML `<tg-time unix="…" format="…">`.
- MTProto `messageEntityFormattedDate`, with flags `relative`, `day_of_week`,
  `short_date`, `long_date`, `short_time` and `long_time`, is in layer 224
  already. Telethon's HTML parser ignores `<tg-time>` and its md parser has no
  syntax for it, so pass the entity through `formatting_entities`.
- A good fit for quota reset times.

### 2.7 Ephemeral messages

- Bot API 10.2 and 10.3: a message inside a group that only one user sees. Any
  bot may send one within 15 s of that user's callback press or ephemeral
  command; an admin bot may send one to any non-bot member at any time.
  Delivery is not guaranteed, and an ephemeral message can be replied to only
  within 15 s.
- `BotCommand.is_ephemeral` marks commands that stay invisible to other
  members. 10.3's `replace_callback_query_message` shows the ephemeral message
  in place of the message whose button was pressed.
- MTProto `ephemeral.*` methods and updates (no pts or qts), layers 228 and
  229, so Telethon 1.45 only.
- They serve groups the bot is in, not guest answers.

### 2.8 Business bots

- A user connects a bot under Settings, Chat Automation. The bot then sees the
  user's private chats and replies as the user. Since 10.0 the user needs no
  Premium.
- Replying needs the `can_reply` right and works only in private chats with an
  incoming message in the last 24 h. The Bot API draft methods take no business
  connection, and checklists can only be sent this way.
- Decision: business-mode auto-transcription is a later idea; replies sent as
  the user are out of scope.

### 2.9 Bot-to-bot

- Enabled per bot in BotFather ("Bot-to-Bot Communication Mode"). In groups a
  bot reaches another through `/command@OtherBot` or a reply, and admin bots or
  bots with privacy off see all bot messages. Since 10.0 bots can DM each other
  by username if both enable the mode. Telegram requires loop prevention.
- Decision: not used between this repo's own bots.

Out of scope here: managed bots, guardian (join-request) bots, access
whitelists, gifts and polls. The Bot API still has no chat-history method.

## 3. MTProto and Telethon

### 3.1 Mapping

Each entry gives the Bot API name, then the Telethon 1.45 raw form:

- `sendMessageDraft`: `SetTypingRequest(peer,
  SendMessageTextDraftAction(text=TextWithEntities(t, entities), random_id=D,
  can_stop=True, keep_on_stop=True), top_msg_id=...)`. `TextWithEntities` needs
  a list, not `None`.
- `sendRichMessageDraft`: `SetTypingRequest` with
  `InputSendMessageRichMessageDraftAction(rich_message, random_id, can_stop,
  keep_on_stop)`.
- `stopped_message_generation`: `UpdateUserTyping` whose `action` is
  `SendMessageStopDraftAction(random_id)`.
- `sendRichMessage`: `SendMessageRequest(peer, message='',
  rich_message=InputRichMessageMarkdown(markdown=md))`.
- `editMessageText` with `rich_message`: `EditMessageRequest(...,
  rich_message=...)`, or `EditInlineBotMessageRequest(id, rich_message=...)`
  for inline and guest messages.
- `Message.rich_message`: `Message.rich_message`, a `RichMessage` of
  PageBlocks. The full version of a partial one comes from
  `GetRichMessageRequest(peer, id)`.
- `guest_message`: `UpdateBotGuestChatQuery`.
- `answerGuestQuery`: `SetBotGuestChatResultRequest(query_id,
  InputBotInlineResult(...))`, which returns `InputBotInlineMessageID` or
  `InputBotInlineMessageID64`. A rich answer puts
  `InputBotInlineMessageRichMessage(rich_message=...)` in the result's
  `send_message`.
- `supports_guest_queries`: `User.bot_guestchat`.
- `date_time`: `MessageEntityFormattedDate`.
- The ephemeral methods: `functions.ephemeral.*`.

Interop facts, should a Bot API path ever be added: Bot API chat ids equal
Telethon's marked peer ids, Bot API message ids equal server message ids, and
both count entity offsets in UTF-16 code units. File ids are not
interchangeable.

### 3.2 Telethon versions

- **1.43.2** (2026-04-20, layer 224):
  - Has the old draft action `#376d975c` (no Stop), private-chat topics, button
    styles and `MessageEntityFormattedDate`. Whether the server still accepts
    the old draft action is [untested].
  - Lacks guest mode, rich messages, the Stop action and ephemeral messages.
  - Reads rich messages as empty text plus `MessageMediaUnsupported` [field].
  - Needs the repo's `message#95ef6f2b` shim (`uniborg/telethon_compat.py`), a
    layer leak of layer 225's `Message` that followed the userbot invoking a
    layer-225 method (`docs/deleter.md`). That the method call caused the leak
    is likely but unproven. The shim is dead code on 1.45.
- **1.44.0** (2026-06-15, layer 227):
  - A drop-in upgrade for this repo: it keeps the old button classes.
  - Adds rich send, edit and read, guest mode, `GetRichMessageRequest` and the
    generated `DeleteParticipantReactionsRequest`, all with the same
    constructor ids as layer 229.
  - Lacks the Stop action, ephemeral messages and the layer-229 keyboards, and
    its rich draft action has the old id.
  - So 1.44 is the minimum for guest mode and rich messages.
- **1.45.0** (2026-09-10, layer 229):
  - Everything above; its `api.tl` is identical to tdesktop's current schema.
  - Breaks the old button classes (section 3.3).
  - The session format is unchanged. `send_message` and `edit_message` differ
    from 1.43.2 only in docstrings and type hints, and `events` and the update
    loop are byte-identical.

Decision: go straight to **1.45.0**, after a button refactor that works on both
1.43.2 and 1.45.0. Pin the version: an unpinned `telethon` requirement installs
1.45.0 and breaks startup.

Project status: Telethon moved to Codeberg in February 2026 and the GitHub
repository is archived. v1 is in maintenance mode but still tracks new layers,
and it accepts only human-authored issues and pull requests, so agent-written
patches stay local. There is no v2 release. Install PyPI wheels, never the v1
branch head: a commit after 1.45.0 left it briefly unimportable.

### 3.3 The 1.45 button-class removal

- Layer 229 removed all 17 per-kind keyboard classes, including
  `KeyboardButtonCallback`, `KeyboardButtonRequestPeer` and
  `KeyboardButtonUrl`. Reply buttons are now `KeyboardButton(text, type:
  ButtonType*, style)`, and inline buttons are `KeyboardInlineButton(text,
  type: InlineButtonType*, style)` inside a `KeyboardInlineButtonRow`.
- `uniborg/bot_util.py` imports `KeyboardButtonCallback` at module level, and
  `uniborg/__init__.py` imports it transitively. On 1.45.0 every instance fails
  at `from uniborg import Uniborg`, and the test suite fails at collection.
- Forms that work on both versions:
  - `Button.inline(text, data)`. It returns a different class on each version,
    turns str data into bytes, rejects data over 64 bytes, and uses the text
    when data is empty.
  - `Button.text(text).button` for a plain reply button.
  - The request-peer button has no helper on either version, so it needs a
    version check: `KeyboardButtonRequestPeer(...)` before, and
    `KeyboardButton(text, ButtonTypeRequestPeer(...))` on 1.45.
  - A button's payload is `.data` before and `.type.data` on 1.45.
- 1.45's `build_reply_markup` silently drops any object that is not a real
  generated `KeyboardButton` or `KeyboardInlineButton`, so compatibility helpers
  must return generated instances.

### 3.4 Missing high-level helpers

Telethon 1.45 supports the new features at the raw-TL level only:

- `send_message`, `edit_message` and the inline result builder take no
  `rich_message`, and `Message.text` ignores it. `edit_message` routes inline
  edits to the right data center through the private
  `_borrow_exported_sender`, so a rich inline edit must copy that routing.
- There is no event builder for guest queries and no draft helper. This
  repo's are `uniborg/guest_util.py` and `uniborg/draft_stream.py`.
- Raw requests bypass this repo's patched `send_message` history recorder, so
  record outgoing messages by hand.

### 3.5 Telethon pitfalls

These hold in both 1.43.2 and 1.45.0 unless noted:

- **`random_id` is re-randomised per construction.** The draft action classes
  generate a fresh `random_id` whenever it is omitted, so every update would
  start a new draft. Pass one fixed id per answer.
- **The per-call `flood_sleep_threshold` is ignored.**
  `TelegramClient.__call__` accepts it but does not pass it on. Only
  `client.flood_sleep_threshold` (default 60 s) counts.
- **FloodWait sleeps, then re-sends the stale request.** For drafts that
  produces ghost drafts after finalize, and it stalls whatever awaited the
  call. Send drafts from a cancellable background task, with a timeout on
  each call, since cancelling the call is the only way to cancel the sleep.
- **The flood map is keyed by request type, not by peer.** After a
  FLOOD_WAIT on one peer's `SetTypingRequest`, every `SetTypingRequest` to any
  peer, typing loops included, waits first if more than 3 s remain.
- **MessageBox drops recovered pts and qts updates.** After a gap,
  `apply_difference_type` jumps to the final state and then discards every
  `other_updates` entry carrying pts or qts, plus the update that exposed the
  gap. TDLib applies them instead. Guest queries (qts) and account-level edits
  and deletes behind a gap are therefore lost, not recovered.
- **An unknown constructor fails in three ways.** Inside a pushed container,
  the whole container is dropped without an acknowledgement. Inside an RPC
  result, only that call raises, but the server has already executed it, so a
  naive retry duplicates. Inside `getDifference`, `TypeNotFoundError`
  disconnects the client and `run_until_disconnected` raises.
- **Inline edits need the exported sender for the id's data center.** Borrow
  it before the `try` block (Telethon's own `edit_message` hides a failed
  borrow behind an `UnboundLocalError` in `finally`) and hold one borrow for a
  whole stream.
- **The layer is declared once per connection** through `invokeWithLayer` plus
  `initConnection`. Wrapping single requests in a newer `InvokeWithLayer`
  switches the saved layer for the whole connection.
- **Never run two clients on one session or auth key.** Going over the
  server's parallel-session limit (1 by default) triggers
  `AUTH_KEY_DUPLICATED`, which invalidates the key. For a bot whose token is
  not at hand, only BotFather can recover it.

## 4. Workarounds analysed, and the decision

**Chosen: upgrade Telethon.** Refactor the buttons so they work on both
versions, then pin 1.45.0, with a canary instance first. It needs no bot token
and no second authorization, covers the userbot and the bots alike, and has
the lowest long-term cost. Its risks are a one-time refactor and a dependency
on Telethon keeping up with Telegram's layers.

**Rejected: a Bot API HTTP sidecar** (Telethon for updates, `api.telegram.org`
for the new methods):

- The bots log in from session files, so no token is available at runtime.
  `bots.exportBotToken` works only for managed bots.
- It adds a second authorization of the same bot. Telegram says a bot logged
  in on more than one server has "no guarantee" of receiving all updates. The
  "delivered via only one of the currently active sessions, chosen randomly"
  rule covers sessions sharing one auth key, so it does not settle this either
  way.
- The first call with a token, even `getWebhookInfo`, starts a cloud bot
  instance. Nothing closes it when idle, and it queues updates. Removing it
  takes `logOut`, which blocks logging back in to the cloud for 10 minutes.
- It cannot receive guest queries or Stop presses without polling, which would
  compete with Telethon.

**Rejected beyond a few leaf types: hand-written TL classes on 1.43.2.**
Between layers 224 and 229, 32 constructors changed id (including `message`,
`user`, `channel` and `messages.sendMessage`), 159 were added and 17 removed.
Guest mode alone nests a layer-229 `Message`, then `RichMessage`, then new
PageBlocks. A parse bug corrupts everything after it in the buffer, and one
unparseable update inside `getDifference` disconnects the client.

**Emergency bridge only: regenerating Telethon's TL from a newer `api.tl`.**
Every layer bump then needs matching hand edits to Telethon's high-level code
(`custom.Message` lists every field explicitly). If PyPI ever lags a needed
layer, try a pinned Codeberg release commit first.

**Rejected: Kurigram or aiogram.** Kurigram (a Pyrogram fork) is on layer 229
and active, and aiogram 3.31 supports Bot API 10.3. Both need their own
authorization: aiogram needs the token and starts the cloud instance; Kurigram
needs its own login, and reusing Telethon's auth key risks
`AUTH_KEY_DUPLICATED`. A full switch means rewriting a large Telethon-idiomatic
codebase, and neither fixes receiving on 1.43.2.

**Rejected: a local telegram-bot-api server.** It requires `logOut` from the
cloud server (then a 10-minute lockout), is yet another authorization that
competes for updates, and needs a C++ build plus state to operate. It offers
nothing over the upgrade for these features.

If any of these bots' tokens was ever used against `api.telegram.org`, a cloud
instance may already be running and competing for updates. Calling `logOut`
once for it ends that.

Safety nets worth having on any version, since Telegram changes layers in
place:

- skip an unknown message inside a container by its length, and still
  acknowledge it;
- on an unparseable `getDifference`, skip the window with a fresh state and
  alert, instead of disconnecting;
- re-dispatch the pts and qts updates that `getDifference` recovered, deduping
  guest queries by `query_id`;
- count "Type … not found" log lines and alert on them.

Never do:

- override Telethon's `LAYER` or wrap requests in a newer `InvokeWithLayer`;
- install Telethon from the v1 branch head;
- enable BotFather Guest Mode on an instance still on layer 224. A layer-224
  client cannot parse the guest query, and the `getDifference` that follows
  would disconnect it [untested]. Layer 227 (Telethon 1.44) is technically
  enough; this repo waits for 1.45 plus the qts re-dispatch net;
- send to a guest chat id, or retry `setBotGuestChatResult`;
- reuse a draft's `random_id` for the final message;
- send drafts from a userbot.

## 5. Open questions that need live tests

- **Guest mode:** the real answer deadline; the inline edit rate past one
  edit per 1.25 s; media download from a reference an hour later, and of voice
  notes and video; callback routing on guest answers; whether `reply_markup`
  survives in `reference_messages`; which updates arrive when the bot also has
  inline mode; whether `get_me().bot_guestchat` tracks the BotFather toggle; a
  query recovered through the re-dispatch net after a gap. Answered on the
  canary (section 2.3): the private-chat peer id, the home data center,
  immediate reference downloads, and member bots.
- **Drafts:** the FloodWait threshold at 0.5, 0.8 and 1.0 s cadence mixed with
  typing actions; whether identical heartbeats keep a draft alive; lingering
  drafts with and without the sync draft, and ghosts after finalize; whether
  the layer-224 draft action is still accepted; the rich draft size limit;
  drafts from user accounts and over business connections; iOS adoption rules
  (its source was not inspected).
- **Rich messages:** the `part` threshold; whether 32768 counts bytes or
  characters; how the server parser treats `$5 and $10`, raw HTML such as
  `<think>` or `List<String>`, unreachable image URLs and unclosed fences
  mid-stream; server acceptance of plain-to-rich edits and the rich edit rate;
  what a layer-224 bot session receives for an incoming rich message.

## 6. Primary sources

- https://core.telegram.org/bots/api-changelog
- https://core.telegram.org/bots/api
- https://core.telegram.org/bots/features
- https://core.telegram.org/bots/faq
- https://core.telegram.org/api/bots/ai
- https://core.telegram.org/api/bots/guest-mode
- https://core.telegram.org/api/config
- https://core.telegram.org/method/messages.setTyping
- https://core.telegram.org/api/entities
- https://core.telegram.org/api/updates
- https://core.telegram.org/api/datacenter
- https://core.telegram.org/api/invoking
- https://core.telegram.org/api/layers
- https://core.telegram.org/method/bots.exportBotToken
- https://core.telegram.org/method/invokeWithoutUpdates
- https://telegram.org/blog/ai-bot-revolution-11-new-features
- https://telegram.org/blog/watch-apps-and-more
- https://telegram.org/blog/communities-editor-invisible-messages
- https://t.me/s/BotNews
- https://raw.githubusercontent.com/telegramdesktop/tdesktop/dev/Telegram/SourceFiles/mtproto/scheme/api.tl
- https://raw.githubusercontent.com/tdlib/td/master/td/generate/scheme/telegram_api.tl
- https://raw.githubusercontent.com/tdlib/td/master/td/generate/scheme/td_api.tl
- https://github.com/tdlib/td (`DialogAction.cpp`, `DialogActionManager.cpp`,
  `RichMessage.cpp`, `MessagesManager.cpp`, `InlineQueriesManager.cpp`,
  `UpdatesManager.cpp`)
- https://github.com/tdlib/telegram-bot-api (`Client.cpp`, `ClientManager.cpp`,
  README)
- https://github.com/telegramdesktop/tdesktop (`history_streamed_drafts.cpp`,
  `data_session.cpp`, `history_item.cpp`, `iv_rich_page.cpp`)
- https://github.com/DrKLO/Telegram (`BotForumHelper.java`,
  `ChatActivityEnterView.java`, `RichMessageConvert.java`)
- https://codeberg.org/Lonami/Telethon
- https://pypi.org/project/Telethon/
- https://docs.telethon.dev/en/stable/misc/changelog.html
