from telethon import events
from uniborg.util import embed2
import datetime
from dateutil.relativedelta import relativedelta
import json, yaml
import jsonpickle
from pathlib import Path
from brish import z
import os

activity_list_path = Path(z('print -r -- "${{attic_private_dir:-$HOME/tmp/}}/timetracker.json"').outrs) # Path.home().joinpath(Path("cellar"))
os.makedirs(os.path.dirname(activity_list_path), exist_ok=True)
activity_list = {}

def load_activity_list():
    global activity_list
    if activity_list_path.exists():
        with open(activity_list_path) as f:
            activity_list = jsonpickle.decode(f.read()) # WARNING: f should be trusted or it'll run arbitrary code

load_activity_list()

def save_activity_list():
    print(f"Saving activity list to '{activity_list_path}' ...")
    with open(activity_list_path, 'w') as f:
        f.write(jsonpickle.encode(activity_list))

@borg.on(events.NewMessage(chats=[-1001179162919], incoming=True, forwards=False))
async def _(event):
    # chat = await event.get_chat()
    # msgs = await borg.get_messages(chat, limit=1) # bots can't do get_messages
    # m0 = msgs[0]
    # m1 = msgs[1]
    m0 = event.message
    m0_text = m0.text

    today = datetime.datetime.today()
    today_date = today.strftime('%Y-%m-%d')
    today_activity_list = activity_list.get(today_date, None)
    if m0_text.lower() == "..reset":
        activity_list[today_date] = None
        return
    if m0_text.lower() == "..out":
        await event.reply(f"{today_date}:\n{activity_list_to_str(today_activity_list)}")
        save_activity_list()
        return
    if today_activity_list == None:
        activity_list.setdefault(today_date, [("Start", today, today)])
        return
    if m0_text == '.':
        m0_text = today_activity_list[-1][0]
    if m0_text.startswith('#'): # comments :D
        return
    today_activity_list.append((m0_text, today_activity_list[-1][2], today))

    ##
    # res = activity_list_to_str(today_activity_list)
    # print(res)
    # print(yaml.dump(activity_list))
    # print(json.dumps(activity_list)) # Object of type datetime is not JSON serializable

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