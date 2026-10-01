# Callback Answers

A button press (a callback query) is answered once: with a toast (a short
text that fades), an alert (a text the user must dismiss), or nothing, which
just stops the client's spinner. This page explains why toasts sent after an
edit used to vanish, and the wrapper that fixes it.

## The trap in Telethon

`CallbackQuery.Event.answer(message=None, cache_time=0, *, url=None,
alert=False)` sends `SetBotCallbackAnswerRequest`, then sets `_answered`, and
returns without sending anything on every later call.

`edit`, `delete`, `respond` and `reply` on the same event each start a
background task running a bare `self.answer()` before their own request. That
task answers the press with no text while the edit is in flight, so in

```python
await event.edit(text, buttons=rows)
await event.answer("Saved.")
```

the toast never shows. A failed edit (say, `MessageNotModifiedError`) has
already scheduled the task too. This is the same in Telethon 1.43.2 and
1.45.0 (`telethon/events/callbackquery.py`).

The alert keyword is `alert=True`. Telethon has no `show_alert`; passing it
raises `TypeError`, which leaves the press unanswered.

## The wrapper

`uniborg.callback_util.hold_bare_answers` decorates a CallbackQuery handler.
While the handler runs, it replaces the event's `answer`:

- A bare call (no arguments) is held back. Telethon's automatic answer is one,
  and so is a handler's own `event.answer()`.
- A call with arguments (a toast, an alert, a URL) goes straight to Telegram.
- If nothing with arguments went out, the held answer is sent when the handler
  returns or raises, or after `FALLBACK_ANSWER_SECONDS` (5 s), whichever comes
  first. So every press is answered, including presses no branch handles.

Since Telethon calls `self.answer()`, which finds the replacement on the
instance, its automatic answers go through the same gate.

Trade-offs:

- A bare `event.answer()` no longer stops the spinner at once; it waits for
  the handler (at most the fallback delay). A handler that starts slow work
  should answer with text first ("Working…"), as `_answer_from_luna_reserve`
  does.
- A toast sent after the fallback fired is dropped, as before.

## Where it is used

- `llm_chat_plugins/llm_chat.py`: `callback_handler`, the plugin's one
  CallbackQuery entry point. The tests call it directly, so they go through
  the wrapper too.
- `image_gen_plugins/image_gen.py`: `callback_handler`.
- `tts_plugins/tts_bot.py`: `voice_callback_handler` and
  `model_callback_handler`.

Wrap any new CallbackQuery handler the same way. Its tests can use a real
`events.CallbackQuery.Event` over a recording client, as
`tests/test_callback_util.py` does; mocked events accept any keyword and never
answer on their own, so they hide both problems above.
