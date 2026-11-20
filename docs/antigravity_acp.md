# Google Antigravity in Betterborg

The private pilot runs the official Google Antigravity ACP agent (1.5.0).
Each approved bot admin signs in with their personal Google account and uses
that account's Antigravity plan allowance. Gemini chat uses the models Google
exposes to that account. Hydrogen, Neon, and Carbon are in `/setModel`.
[Google's ACP guidance](https://discuss.ai.google.dev/t/conflicting-guidance-on-using-t3-code-with-personal-google-ai-subscriptions/197131/3)
[Google's model availability](https://antigravity.google/docs/models)

1. In the DM for Chat, Transcribe, or Imagine, choose **Google Antigravity**
   in the provider menu or select `/antigravity_login` from the command menu.
2. Open **Sign in with Google**. After consent, copy the full browser address
   beginning with `http://127.0.0.1:` and paste it into the same DM that gave
   you the link. The page may show a connection error. The bot forwards the
   address to the waiting agent and confirms the connection in the sign-in
   message.
3. The bot where you signed in selects Antigravity. Each of the other bots
   can use the same Google account as soon as you choose Antigravity from
   its own provider menu.

Use `/antigravity_usage` in Chat to view the allowance Google reports. This
queries Google's quota endpoint without a model turn.

- **Chat** uses the model you select. Send `.i` followed by a prompt to
  request an image through Antigravity. Allowlisted admins can use it in
  DMs, groups, and guest chats. `ANTIGRAVITY_CHAT_GROUPS` and
  `ANTIGRAVITY_CHAT_GUESTS` in [the Chat plugin](../llm_chat_plugins/llm_chat.py)
  toggle group and guest use independently.
- **Transcribe** uses Gemini 3.8 Flash for audio, the sound from video,
  and OCR for JPEG, PNG, and WebP. The total inline media limit is 12 MiB.
  Use `/antigravity` to select it directly in Transcribe.
  Allowlisted admins can also request a transcription in groups and via a
  guest mention. `ANTIGRAVITY_STT_GROUPS` and `ANTIGRAVITY_STT_GUESTS` in
  [the Transcribe plugin](../stt_plugins/stt.py) toggle those separately.
- **Imagine** uses Antigravity's native image tool. Select it with
  `/antigravity` in its DM. In groups, admins use `/imagine` followed by a prompt. In guest
  chats, mention Imagine with a prompt to receive one image. Use the
  `ANTIGRAVITY_IMAGINE_GROUPS` and `ANTIGRAVITY_IMAGINE_GUESTS` switches in
  [the Imagine plugin](../image_gen_plugins/image_gen.py) to control those
  entry points. Telegram's Guest Mode must also be enabled for the bot.

Betterborg stores Google's reusable grant in a private profile for the signed-in
Telegram account. `/antigravity_logout` removes the local grant and switches
Chat back to AI Studio. The other bots will ask you to reconnect when you
next use Antigravity. You can also revoke access in your Google account
settings. AI Studio and Vertex can still be selected in `/geminiProvider`.
Live mode uses AI Studio. Automatic voice replies
and LLM-generated titles are skipped for Antigravity turns.

## Worker operation

The three bots share one worker slot. If it is busy, Transcribe shows that it
is waiting and starts automatically when a slot becomes available. Use `/stop`
to cancel a waiting transcription. The wait expires after 15 minutes.
The bot distinguishes a missing link from a shared worker that could not
restore a saved sign-in. Shared-server errors carry a request reference, and
rate-limit errors show a retry time when Google supplies one.
Under systemd, each operation uses a fresh sandbox with an empty workspace and
home directory. The native agent starts with only the signing-in user's Google
grant. The worker copies that one file between temporary configuration and
the persistent profile to save sign-in, refresh, and logout. When restoring
a saved grant, it writes a temporary ACP setting that selects personal OAuth.
Workspace rules,
installed skills, and past sessions stay outside the agent's context.
Google's own instructions remain part of the pinned distribution.
Its private network has only loopback and can make
HTTPS CONNECT requests through a proxy for a fixed set of Google hosts.
The host side rejects private and loopback destinations. The bot relays the
authorization response to the original agent inside that same worker and
discards it from the chat history used for generation.

Chat's Google Search toggle in `/tools` controls `search_web` on text turns.
Image requests permit `generate_image`. Transcription requests have no tools.
The ACP client offers no filesystem or terminal capabilities and
provides no MCP servers. Shared API keys and the service's fallback routes do
not apply to Antigravity requests.

### Systemd service

Set up Betterborg from a reviewed checkout on a Linux x86-64 VM with systemd,
Landlock ABI 3 or newer, Python 3.11 or newer, and `curl`. From the
Betterborg checkout, run the installer as root and pass the Unix account
that runs the three bots.

```sh
sudo /bin/sh deployment/antigravity/install.sh BOT_UNIX_USER
```

On a fresh VM, the installer downloads the pinned ACP release from Google
and verifies the archive and extracted binaries. It reuses matching binaries
from an earlier systemd or Docker setup. On a VM without access to Google's
download server, pass the path to the official ZIP as the second argument.
The same SHA-256 checks apply. [ACP registry manifest](https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json)

The installer creates any missing systemd configuration directories and the
dedicated worker account. It starts the Unix socket and checks that the
pinned agent initializes from the bot account. Worker startup errors are
available in the systemd journal. Use the same checkout command after a
worker upgrade. [Deployment checks](deployment.md)

The systemd worker uses a private network namespace, a read-only system,
bounded temporary storage, a syscall filter, and CPU, RAM and process
limits. The trusted wrapper launches the native agent under a Landlock
allowlist for its temporary workspace and configuration. OAuth returns enter the same
worker that initiated the sign-in. The Google proxy remains in Betterborg.

The installer enables systemd selection for bot revisions that support it.
If the installed service is unavailable, requests fail closed. Docker is
the backend on hosts without the installer configuration. For a rollback,
remove `/etc/betterborg-antigravity/enabled` as root and restart the three
bots together.

Switching from Docker to systemd requires a one-time `/antigravity_login`
in any of the three bots. The old Docker sign-in is stored in the bot's
profile and the new worker keeps its credentials under a dedicated Unix
account. A saved Antigravity selection can be used immediately after the
sign-in confirmation.

### Shared server

The Antigravity Go project ships the two installers for this setup. Run
`deployment/worker/install.sh` as root from that project's checkout to
install or refresh the worker. Run `deployment/install.sh` as the service
account to install the Go server. The worker keeps its existing socket and
Google profile locations during the upgrade.

Chat, Transcribe, and Imagine can send turns and quota requests to a shared
Antigravity server over a Unix socket. The server starts the isolated workers
and reports queue position. Telegram still performs the admin check before
the request is sent. The same owner can use the existing Telegram sign-in.
Configure the imported operator profile to match that owner's ID.

- `borg_antigravity_gateway_owner` is the Telegram admin ID routed to the server.
- `borg_antigravity_gateway_socket` is the absolute Unix socket path.
- `borg_antigravity_gateway_token_file` is the absolute path to the private project token.
- `borg_antigravity_gateway_account` selects the operator alias (default `main`).

You can also use `~/.config/betterborg/antigravity-gateway.json` with keys
`owner`, `socket`, `token_file`, and `account`. Betterborg reads it when
starting an Antigravity request, so switching that account needs no bot
restart once this adapter is deployed.

### Docker backend

Install `agy_acp_server.par` and `localharness_external` from the Linux x86-64
release in `~/.local/lib/betterborg/antigravity-acp/1.5.0/`. Verify the
registry's SHA-256 before extracting and keep both files executable. Pull the
pinned Python image as the bot operator.

```sh
docker pull python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
```

### Worker startup benchmark

Median across nine interleaved launches per configuration on Linux x86-64.
The Antigravity 1.5.0 package and Docker image were warm. Each run started with
a disposable profile and ended after the ACP initialization response. Memory
is proportional set size (PSS), which splits shared memory across the
processes that map it.

- Native launch under an unprivileged UID took 3.85 s, 3.80 CPU seconds, and
  297 MiB of PSS. This measures the agent after starting under the UID.
- A transient systemd service with Landlock filesystem restrictions, TCP
  sockets blocked, and cgroup limits took 3.95 s, 3.82 CPU seconds, and
  297 MiB of PSS.
- The deployed Docker launcher took 4.64 s, 4.30 CPU seconds, and 332 MiB of
  PSS.

Measurements include the worker and launcher processes at
initialization. Shared daemons, root-owned runtime helpers, and peak memory
during authenticated model turns fall outside these measurements. Antigravity
itself accounted for about 281 MiB in each configuration.

The lightweight benchmark used a transient user-level service and
blocked all IP sockets. The production unit adds a separate Unix account
and private network, with the same restricted Google proxy used by Docker.
[systemd sandbox options](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

Before expanding beyond the pilot, measure peak memory and profile growth
and verify each target workflow across restart and unlink. Google documents personal OAuth for ACP
clients. A wider hosted rollout needs its own review of the service terms.
[Google's personal terms](https://antigravity.google/terms)
