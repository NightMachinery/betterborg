import traceback
from telethon import events, TelegramClient
from telethon.tl.custom import Button
from uniborg.util import embed2, admin_cmd, discreet_send
from uniborg.storage import UserStorage
from brish import z, zp, bsh, zq, CmdResult
from typing import Dict, Iterable
import json
import re
from uuid import uuid4

borg: TelegramClient = borg


p_zsh = re.compile(r"(?im)^\.z\s+((?:.|\n)*)$")
CUSTOM_DATA_PREFIX = "jjson_"
p_custom = re.compile(r"^jjson_([0-9a-f]{32})$")
_custom_store = None


def custom_data_store():
    global _custom_store
    if _custom_store is None:
        _custom_store = UserStorage(purpose="jjson_buttons")
    return _custom_store


def inline_button(label, data=None, *, callback_store=None):
    """Keep shell buttons as they are; give custom echoes their own namespace.

    Store the original data behind a short token so a 64-byte payload still
    fits Telegram's limit and buttons continue working after a restart.
    """
    button = Button.inline(label, data)
    payload = button.data.decode("utf-8")
    if payload.startswith("zsh_") or p_zsh.match(payload):
        return button
    token = uuid4()
    store = callback_store if callback_store is not None else custom_data_store()
    if not store.set(token.int, {"data": payload}):
        raise RuntimeError("Could not save custom button data")
    return Button.inline(label, f"{CUSTOM_DATA_PREFIX}{token.hex}")


def create_key(pl):
    return f"borg_callback_{pl}"


def is_own_data(data) -> bool:
    """Whether DATA is a shell button or a namespaced `.jjson` custom echo.

    Every other press belongs to the plugin that sent its button (the shell's
    Stop button and /settings panel, say). Answering or echoing it here
    would take it from that plugin whenever this one's handler ran first,
    which depends on the order the plugins load in.
    """
    if data is None:
        return False
    try:
        pl = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return pl.startswith("zsh_") or bool(p_zsh.match(pl) or p_custom.fullmatch(pl))


@borg.on(events.CallbackQuery(data=is_own_data))
async def callback(
    event: events.callbackquery.CallbackQuery.Event, *, callback_store=None
):
    pl = event.data.decode("utf-8")
    custom = p_custom.fullmatch(pl)
    if custom:
        store = callback_store if callback_store is not None else custom_data_store()
        payload = store.get(int(custom.group(1), 16)).get("data")
        if not isinstance(payload, str):
            await event.answer("This button's data is unavailable.", alert=True)
            return
        await event.reply(payload, parse_mode=None)
        await event.answer()
        return
    # We can edit the event to edit the clicked message.
    chat = await event.get_chat()
    pl = str(event.data, "utf-8")
    # embed2()
    msg = await borg.get_messages(chat, ids=event.message_id)
    msg_id = event.message_id

    print(f"pl: {pl}")
    # await event.reply(f'pl: {pl}\n\n{event.__dict__}\n\n{event.query.__dict__}')

    m_zsh = p_zsh.match(pl)
    if pl.startswith("zsh_"):
        key = create_key(pl)
        results = list(
            z("""jfromkey {key}""").iter0()
        )  # TODO inject data from event, e.g., the sender's name
        out = results[0]  # contains both stdout and stderr
        jaction = results[1]
        if jaction == "edit":
            # await event.edit(out) # this loses the buttons
            await msg.edit(out)
        elif jaction == "toast":
            await event.answer(message=out)
        else:
            await discreet_send(event, out, msg)
    elif m_zsh:
        res: CmdResult = z(m_zsh.group(1))
        await discreet_send(event, res.outerr, msg)
    await event.answer()  # does nothing if we answered before


@borg.on(admin_cmd(pattern=r"(?im)^\.jjson\s+((?:.|\n)*)$"))
async def _(event: events.newmessage.NewMessage.Event):
    chat = await event.get_chat()
    match = event.pattern_match
    jj = match.group(1)
    await send_json(borg, jj, chat=chat)


async def send_json(
    borg: TelegramClient, json_pl: str, *, chat=None, callback_store=None
):
    print(f"JSON: {json_pl}")
    try:
        out_j = json.loads(json_pl)
        if out_j and not isinstance(out_j, str) and isinstance(out_j, Iterable):
            for item in out_j:
                if isinstance(item, dict):
                    chat = item.get("receiver", chat)
                    caption = item.get("tlg_content", item.get("caption", ""))
                    buttons_inline = item.get("buttons_inline", [])
                    buttons_zsh = item.get("buttons_zsh", [])

                    buttons_inline_tl = []
                    buttons_tl = None
                    for btn in buttons_inline:
                        buttons_inline_tl.append(
                            inline_button(
                                btn[0],
                                btn[1] if len(btn) > 1 else None,
                                callback_store=callback_store,
                            )
                        )
                    for btn in buttons_zsh:
                        btn_json = json.dumps(btn)
                        cmd = btn.get("cmd", "echo Empty command was inlined")
                        btn_caption = btn.get("caption", cmd)
                        jdata = btn.get("jdata", "")
                        jaction = btn.get("jaction", "reply")

                        uid = uuid4()
                        pl = f"zsh_{uid}"
                        key = create_key(pl)
                        zp(
                            "reval-ec jtokey {key} {cmd} {json_pl} {btn_json} {jdata} {jaction}"
                        )
                        buttons_inline_tl.append(Button.inline(btn_caption, pl))
                    if len(buttons_inline_tl) >= 1:
                        buttons_tl = [buttons_inline_tl]
                    await borg.send_message(chat, caption, buttons=buttons_tl)
    except:
        exc = "Julia encountered an exception. :(\n" + traceback.format_exc()
        await borg.send_message(chat, exc)

    return
    await borg.send_message(
        chat,
        'A single button, with "clk1" as data',
        buttons=Button.inline("Click me", b"clk1"),
    )

    await borg.send_message(
        chat,
        "Pick one from this grid",
        buttons=[
            [Button.inline("Left"), Button.inline("Right")],
            [Button.url("Check this site!", "https://lonamiwebs.github.io")],
        ],
    )
