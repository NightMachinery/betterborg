# Concurrent chat delivery

The chat bot owns one Telegram scheduler per client. Private-chat sends and
edits share a one-second pace across all topics of that chat. Topic edits
share a one-second pace across the client. Final answers take priority over
placeholders, optional streaming edits, and background topic metadata.

An explicit Telegram flood rejection records the required cooldown for that
request type across topics. Slow-mode waits apply only to the affected chat.
Required operations wait and retry the same request, preserving its random ID;
they do not abandon an answer when the wait exceeds Telethon's old 60-second
automatic-sleep threshold. Ambiguous network failures are not retried.

Streaming edits do not wait in this queue. During congestion, they are skipped
while the model continues generating. The complete answer remains available
for required final delivery. Final text, chunk, and file failures propagate
instead of being silently treated as success. Existing callers of the shared
message helper retain best-effort delivery unless they opt into
`strict_delivery=True`.

`/stop` covers the whole request, including waiting for its placeholder and
waiting to deliver its final answer. A cancelled request drops its queued
operations and does not flush an unsent draft answer during cleanup.

The scheduler is enabled only for the chat bot. It sits inside outgoing-trigger
protection and topic placement, preserving those checks on every operation.
Guest answers retain their at-most-once transport; typing/draft calls and
callback acknowledgments retain their bounded behavior. Other bot instances
keep their existing Telethon flood handling.

The queue is in memory. Restarting the process loses pending work; the bot does
not automatically replay old requests. UTC diagnostics record request types,
priorities, cooldown seconds, successful delivery and terminal exception types,
without recording message text or credentials. No live flood test is needed:
the scheduler regressions use synthetic RPC failures and a controlled clock.
