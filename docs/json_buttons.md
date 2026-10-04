# JSON buttons

The standard `buttons_test` plugin accepts `.jjson` followed by a JSON list
of messages. Each item can have `caption` (or `tlg_content`),
`buttons_inline` and `buttons_zsh`.

For example:

```text
.jjson [{"caption":"Try a button", "buttons_inline":[["Press","custom data"]]}]
```

Pressing that button replies with `custom data`. A one-element button list
uses its label as the data. Echoes are literal text, without Markdown
parsing. Payloads may contain up to 64 UTF-8 bytes.

Custom echo buttons carry a short `jjson_` token, with the original payload
saved in `UserStorage(purpose="jjson_buttons")`. This lets them survive a
restart and avoids taking callback presses from other plugins, including
Stop and settings buttons. Re-send older `.jjson` messages whose custom
buttons predate this token format. A missing saved payload gets an
"unavailable" alert.

Inline data starting with `.z CMD` or `zsh_` keeps the existing shell-button
behavior. `buttons_zsh` also keeps its command, caption and reply/edit/toast
actions. Ordinary custom payloads, even text matching another plugin's
callback data, are echoed by their token and do not execute that action.
