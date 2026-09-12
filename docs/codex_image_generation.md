# Codex Image Generation

Betterborg exposes Codex image generation through the `.i` prefix. The prefix
is a switch for the current message only: it does not change the user's saved
model, the chat model, or any future request.

## Request selection

`.i` is recognized as a leading, whitespace-delimited token. It can be combined
with a Codex model prefix and a reasoning prefix in either order. For example,
`.i .cl draw a fox` and `.cl .i draw a fox` both request image generation with
GPT-5.6 Sol at low reasoning effort.

The model is selected in this order:

1. An explicit Codex model prefix on the current message.
2. The already selected model, when it is a Codex model.
3. GPT-5.6 Sol as the image-generation default.

An explicit non-Codex model prefix conflicts with `.i`; the request is rejected
instead of silently changing providers. Merely mentioning `.i` later in natural
language does not enable image generation.

Image generation requires the sender to pass both `codex_allowed_users` and
`codex_imagegen_allowed_users` from `~/.borg/llm_chat_config.json5`. These checks
use the same configuration snapshot for the request. Failing either policy
stops the request before the Codex backend is called.

## Prompt and tool behavior

The switch applies only to the current incoming message. Betterborg removes the
leading `.i` token before constructing the prompt. Tokens found in conversation
history, quoted replies, or ordinary prose cannot enable the tool.

The Codex request includes:

```json
{
  "type": "image_generation",
  "partial_images": 3
}
```

The model decides whether to invoke the tool. `partial_images` asks the backend
for up to three previews; the backend may return fewer. This range and behavior
are described in the [official OpenAI image-generation tool guide](https://developers.openai.com/api/docs/guides/tools-image-generation).

If the user's `googleSearch` setting is enabled, Betterborg retains the
`web_search` tool alongside image generation. Enabling `.i` does not discard
the user's search preference.

## Streaming and Telegram delivery

Codex streams preview images as
`response.image_generation_call.partial_image` events. Betterborg decodes and
validates each image immediately, then sends every preview as a separate
Telegram image replying to the current request. Previews are retained; a later
preview or final image does not edit or delete an earlier one.

Preview events are deduplicated by image item ID and preview index. Final
images, which can appear in both `response.output_item.done` and the terminal
response output, are deduplicated by image item ID. Deduplication is based on
event identity rather than bytes, so distinct image items with identical image
data are still delivered separately.

Captions distinguish previews from final images and associate them with the
current request. Text can arrive as deltas, terminal text fields, or not at all;
image-only responses are valid. Terminal text fills a missing text part but
does not duplicate text already accumulated from deltas.

Malformed base64, invalid image bytes, missing item identities, and invalid or
missing preview indices fail the request. If the backend fails, the user
cancels, or Telegram delivery fails after one or more images were sent, those
images remain in the chat. Betterborg reports the failure and does not
regenerate or retry the image request automatically. The stream and client are
closed on success, failure, and cancellation.

The stream helper has local tests for ordering, deduplication, decoding,
terminal text, failure, cancellation, and resource cleanup. The live files were
also replayed through `_send_image_to_telegram()` with Telegram calls mocked;
all three retained valid bytes, separate captions, and the triggering reply ID.
No messages were sent to a live Telegram chat.

## Live backend verification

On 2026-09-12, the implemented stream helper was exercised through the actual
ChatGPT OAuth Codex backend with `gpt-5.6-sol`, low reasoning effort, and the
prompt “orange circle on a white square.” The request enabled
`image_generation` with `partial_images: 3`.

The backend returned two previews and one final image, with no text:

- Preview 0: 536,278-byte PNG, 1254 by 1254 pixels.
- Preview 1: 638,200-byte PNG, 1254 by 1254 pixels.
- Final image: 641,813-byte PNG, 1254 by 1254 pixels.

Pillow successfully decoded and verified all three files. Visual inspection
also confirmed the orange-circle-on-white-square result. This verifies the
OAuth endpoint, event handling, preview ordering, final-image extraction, and
local image validation. Telegram delivery was verified separately with mocked
network calls, as described above.
