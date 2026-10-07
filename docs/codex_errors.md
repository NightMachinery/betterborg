# Codex network errors

Codex chat failures distinguish DNS lookup failures, TLS certificate verification
failures, proxy failures, refused connections, interrupted HTTP connections, and
timeouts while connecting, sending, reading or waiting for a connection slot.
The Telegram error includes the exception types and an appropriate next step.
For example:

```text
❌ Codex request failed.
DNS lookup failed.
Details: APIConnectionError -> ConnectError
If this persists, the bot operator should check the server's DNS resolver.
```

The OpenAI SDK's generic `Connection error.` hides the transport exception.
`uniborg.codex_util` follows its exception chain and classifies known failures.
HTTPX can preserve a DNS error only in the text of a `ConnectError`, so known
name-resolution signatures are also recognized. Unknown transport failures
retain their exception types and generic network advice.

Transport exception text, request URLs, headers and credentials are not copied
into the diagnostic. The same safe summary is logged with the model name, so a
handled Codex failure remains visible in the bot's logs. Other backend errors
and usage-limit handling retain their existing behavior.

Partial text and already-delivered images remain available after a failure.
The diagnostic adds no retries or model switches, including after partial output.

For a DNS failure, check name resolution from the bot's host, verify that
`/etc/resolv.conf` exists and resolves if it is a symlink, and check the resolver
service used by that host. A successful query directly to a public DNS server
does not prove the host's configured resolver works. For TLS or proxy errors,
check the certificate and proxy configuration in the bot's runtime environment.

If `/etc/resolv.conf` points to a file managed by `systemd-resolved`, verify both
`systemctl is-active systemd-resolved` and `systemctl is-enabled systemd-resolved`.
Starting the service alone repairs only the current boot. When this is the
intended resolver, `sudo -kA systemctl enable --now systemd-resolved` starts it
and enables startup after reboot.

After restoring DNS, dependent services may still have unresolved upstreams.
For Chrony, check `chronyc activity`; if time sources have unknown addresses,
run `sudo -kA chronyc refresh` to resolve them again. Verify that the unknown
address count clears and `chronyc tracking` reports `Leap status: Normal`.

See the [official OpenAI Python SDK error handling documentation](https://developers.openai.com/api/reference/python)
for `APIConnectionError`, `APITimeoutError` and the underlying `__cause__`.
