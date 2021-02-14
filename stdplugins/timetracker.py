from telethon import events
from uniborg.util import embed2
import datetime
from dateutil.relativedelta import relativedelta
import json, yaml

data = {}

@borg.on(events.NewMessage(chats=[-1001179162919], incoming=True, forwards=False))
async def _(event):
    chat = await event.get_chat()
    # msgs = await borg.get_messages(chat, limit=1) # bots can't do get_messages
    # m0 = msgs[0]
    # m1 = msgs[1]
    m0 = event.message
    m0_text = m0.text

    today = datetime.datetime.today()
    today_date = today.strftime('%Y-%m-%d')
    today_data = data.get(today_date, None)
    if m0_text.lower() == "..reset":
        data[today_date] = None
        return
    if m0_text.lower() == "..out":
        await event.reply(f"{today_date}:\n{activity_list_to_str(today_data)}")
        return
    if today_data == None:
        data.setdefault(today_date, [("Start", today, today)])
        return
    if m0_text == '.':
        m0_text = today_data[-1][0]
    if m0_text.startswith('#'): # comments :D
        return
    today_data.append((m0_text, today_data[-1][2], today))

    ##
    # res = activity_list_to_str(today_data)
    # print(res)
    # print(yaml.dump(data))
    # print(json.dumps(data)) # Object of type datetime is not JSON serializable

def activity_list_to_str(d):
    res = ""
    acts = {}
    if not d == None:
        for e in d:
            if e[0] == "Start":
                continue
            dur = relativedelta(e[2], e[1])
            if acts.get(e[0], None) == None:
                acts[e[0]] = dur
            else:
                acts[e[0]] += dur
    for name, dur in acts.items():
        res += f"\n{name}> {dur.hours}:{dur.minutes}"
    return res