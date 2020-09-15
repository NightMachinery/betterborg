import traceback
from telethon import events, TelegramClient
from telethon.tl.custom import Button
from uniborg.util import embed2, admin_cmd, discreet_send
from brish import z, zp, bsh, zq, CmdResult
from typing import Dict, Iterable
import json
import re
from uuid import uuid4

borg: TelegramClient = borg


p_zsh = re.compile(r"(?im)^\.z\s+((?:.|\n)*)$")

def create_key(pl):
    return f'borg_callback_{pl}'

@borg.on(events.CallbackQuery)
async def callback(event: events.callbackquery.CallbackQuery.Event):
    # We can edit the event to edit the clicked message.
    chat = await event.get_chat()
    pl = str(event.data, 'utf-8')
    # embed2()
    msg = await borg.get_messages(chat, ids=event.message_id)
    msg_id = event.message_id

    print(f'pl: {pl}')
    # await event.reply(f'pl: {pl}\n\n{event.__dict__}\n\n{event.query.__dict__}')

    m_zsh = p_zsh.match(pl)
    if pl.startswith('zsh_'):
        key = create_key(pl)
        results = list(z("""jfromkey {key}""").iter0()) # TODO inject data from event, e.g., the sender's name
        out = results[0] # contains both stdout and stderr
        jaction = results[1]
        if jaction == 'edit':
            # await event.edit(out) # this loses the buttons
            await msg.edit(out)
        elif jaction == 'toast':
            await event.answer(message=out)
        else:
            await discreet_send(event, out, msg)
    elif m_zsh:
        res: CmdResult = z(m_zsh.group(1))
        await discreet_send(event, res.outerr, msg)
    else:
        await event.reply(pl)
    await event.answer() # does nothing if we answered before


@borg.on(admin_cmd(pattern=r"(?im)^\.jjson\s+((?:.|\n)*)$"))
async def _(event: events.newmessage.NewMessage.Event):
    chat = await event.get_chat()
    match = event.pattern_match
    jj = match.group(1)
    await send_json(borg, jj, chat=chat)


async def send_json(borg: TelegramClient, json_pl: str, chat=None):
    print(f"JSON: {json_pl}")
    try:
        out_j = json.loads(json_pl)
        if out_j and not isinstance(out_j, str) and isinstance(out_j, Iterable):
            for item in out_j:
                if isinstance(item, dict):
                    chat = item.get('receiver', chat)
                    caption = item.get("tlg_content", item.get("caption", ""))
                    buttons_inline = item.get('buttons_inline', [])
                    buttons_zsh = item.get('buttons_zsh', [])

                    buttons_inline_tl = []
                    buttons_tl = None
                    for btn in buttons_inline:
                        # Note that the given `data` must be less or equal to 64 bytes. If more than 64 bytes are passed as data, ``ValueError`` is raised.
                        if len(btn) == 1:
                            buttons_inline_tl.append(Button.inline(btn[0]))
                        else:
                            buttons_inline_tl.append(
                                Button.inline(btn[0], btn[1]))
                    for btn in buttons_zsh:
                        btn_json = json.dumps(btn)
                        cmd = btn.get("cmd", "echo Empty command was inlined")
                        btn_caption = btn.get("caption", cmd)
                        jdata = btn.get("jdata", "")
                        jaction = btn.get("jaction", "reply")

                        uid = uuid4()
                        pl = f"zsh_{uid}"
                        key = create_key(pl)
                        zp("reval-ec jtokey {key} {cmd} {json_pl} {btn_json} {jdata} {jaction}")
                        buttons_inline_tl.append(Button.inline(btn_caption, pl))
                    if len(buttons_inline_tl) >= 1:
                        buttons_tl = [buttons_inline_tl]
                    await borg.send_message(chat, caption, buttons=buttons_tl)
    except:
        exc = "Julia encountered an exception. :(\n" + traceback.format_exc()
        await borg.send_message(chat, exc)

    return
    await borg.send_message(chat, 'A single button, with "clk1" as data',
                            buttons=Button.inline('Click me', b'clk1'))

    await borg.send_message(chat, 'Pick one from this grid', buttons=[
        [Button.inline('Left'), Button.inline('Right')],
        [Button.url('Check this site!', 'https://lonamiwebs.github.io')]
    ])
