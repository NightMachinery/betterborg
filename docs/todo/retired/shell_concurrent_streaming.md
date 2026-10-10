# Retired: Concurrent Shell Commands Under Draft Streaming

A shelved design for running several shell commands (`.a`, `.af`, `.aa`) at
once in the bot's private chat while their output streams as drafts. Nothing
here is implemented. Kept so the analysis does not have to be redone.

## The problem

While a bot's draft is live, Telegram for Android turns the send button into
a Stop button (see docs/draft_streaming.md and docs/shell_streaming.md). So
under Drafts, a long `.a` keeps you from sending a second command, `.k`
included, until it ends or you press Stop. Two commands previewed as drafts
at once in one chat also share one draft, since clients keep one draft per
sender and thread.

## What already exists

- **The `/settings` panel** (`settings_handler` in
  `stdplugins/advanced_get.py`, stored by `uniborg/shell_settings.py`) sets
  the preview kind per scope: `/settings private edits` streams by edits,
  which never blocks the composer. This is the current workaround.
- **Topic placement**: in a private topic, the shell's draft preview is sent
  into that topic (`top_msg_id=topics.private_topic_id(...)`), and
  `TopicPlacementMixin` keeps replies there (docs/private_topics.md).
- **Per-thread drafts**: since clients keep one draft per sender and thread,
  drafts in different topics do not overwrite each other.
- **Auto topics**: on a bot in threaded mode, a message typed in "All" makes
  Telegram open a new topic for it, so each `.a` typed there would get its
  own topic and its own draft with no bot work.

## The open question

Does a live draft in topic A disable the Android send button only in topic
A, or also in topic B and in "All"? Untested. If the block is per topic, a
thread mode is mostly documentation and polish. If it is per chat, topics do
not help with this problem.

The test: on a threaded-mode bot, run `.a sleep 60; echo hi` in one topic,
then try to send from another topic and from "All" on Android.

## Options considered

1. **Edit streaming.** Already available through `/settings`. Never blocks,
   but loses the smoothness of drafts.
2. **Thread mode.** One topic per command (or per session), auto-titled with
   the `topic_titles` machinery. Solves the problem only if the block is per
   topic (see the open question).
3. **Draft hand-off after a timeout.** Commands under the 2 s preview delay
   show no preview anyway. Use a draft for the first N seconds (say 8); if
   the command still runs, end the draft and continue as an edited message
   with an inline Stop button, which frees the composer. Keeps drafts where
   they feel best (short commands) and never blocks for long ones. Costs one
   visible jump from draft to message and a new `ShellPrefs` field. Works
   whatever the topic test shows; this was the recommended first step.
4. **Draft Stop detaches instead of killing.** Pressing the draft's Stop
   would turn the preview into an edited message and leave the command
   running; killing would go through `.k` or the edited message's Stop
   button. The button is drawn by Telegram and labelled "Stop", so detaching
   would surprise anyone who has not learned it. Only viable as an opt-in
   setting.
5. **Guest (inline) mode from the private chat.** `@bot .a cmd` already
   works, and guest answers are inline messages, never drafts, so they never
   block. Loses the chat's settings, stops through `@bot .k`, and the prefix
   is tedious to type.
6. **Per-topic shell state.** The bigger payoff of a thread design: each
   topic a long-lived session with its own cwd and environment, like tmux
   windows. Brish workers are pooled, so this needs sticky workers or
   per-topic cwd/env replay. Real work.
7. **Client choice.** Only Android is documented to disable the send
   button; Telegram Desktop does not. Context, not a fix.

## Suggested order, if revived

1. Run the topic test above and record the result in docs/private_topics.md.
2. Implement the draft hand-off (option 3).
3. If the block is per topic, design the thread mode (option 2), then
   per-topic shell state (option 6).
