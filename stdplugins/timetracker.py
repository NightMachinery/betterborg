from telethon import events
from uniborg.util import embed2
import datetime
from dateutil.relativedelta import relativedelta
import json, yaml
from pathlib import Path
from brish import z
import os
import re
import peewee

from peewee import *

db_path = Path(z('print -r -- "${{attic_private_dir:-$HOME/tmp}}/timetracker.db"').outrs) # Path.home().joinpath(Path("cellar"))
os.makedirs(os.path.dirname(db_path), exist_ok=True)
db = SqliteDatabase(db_path)

class BaseModel(Model):
    class Meta:
        database = db

class Activity(BaseModel):
    name = CharField()
    start = DateTimeField()
    end = DateTimeField()

    def __str__(self):
        dur = relativedelta(self.end, self.start)
        return f"""{self.name} {dur.hours}:{dur.minutes}"""

db.connect() # @todo? db.close()
db.create_tables([Activity])

timetracker_chat = -1001179162919
# borg.send_message(timetracker_chat, "New timetracker instance initiated")
starting_anchor = None
subs = {
    "👀": "w",
    "res": "..out",
    # "out": "..out",
    "📖": "study",
    "s": "study",
    "sv": "study_video",
    "🏃🏽‍♀️": "exercise",
    "e": "exercise",
    "🧫": "?",
    "💻": "sa",
    "🍽": "eat",
    "ea": "eat",
    "🦷": "brush",
    "br": "brush",
    "🛁": "bath",
    "b": "bath",
    "👥": "social",
    "soc": "social",
    "tlg": "social_online",
    "fam": "family",
    "🎪": "entertainment",
    "fun": "entertainment",
    "🌐": "web",
    "😡": "wasted",
    "wt": "wasted",
    "r": "rest",
    "nf": "nonfiction",
    "nft": "nonfiction_technical",
    "fi": "fiction",
    "med": "meditation",
    "th": "thinking",
    "go": "going out",
    "🐑": "chores",
    "ch": "chores",
    "expl": "exploration",
    "gath": "exploration_gathering"
    }

del_pat = re.compile(r"^\.\.del\s*(\d*\.?\d*)")
out_pat = re.compile(r"^(?:\.\.)?out\s*(\d*\.?\d*)")
back_pat = re.compile(r"^(?:\.\.)?back\s*(\-?\d*\.?\d*)")

@borg.on(events.NewMessage(chats=[timetracker_chat], forwards=False)) # incoming=True causes us to miss stuff that tsend sends by 'ourselves'.
async def _(event):
    global starting_anchor

    m0 = event.message
    m0_text = m0.text.lower() # iOS capitalizes the first letter
    if m0_text in subs:
        m0_text = subs[m0_text]

    if m0_text.startswith('#'): # comments :D
        return

    now = datetime.datetime.today()
    last_act_query = Activity.select().order_by(Activity.end.desc())

    m = del_pat.match(m0_text)
    if m:
        del_count = 0
        if m.group(1):
            del_count = Activity.delete().where(Activity.end > (now - datetime.timedelta(minutes=float(m.group(1) or 5)))).execute()
        elif last_act_query.exists():
            del_count = last_act_query.get().delete_instance()
        # starting_anchor = None
        # await event.reply(f"Deleted the last {del_count} activities, and reseted the starting anchor.")
        await borg.edit_message(m0, f"Deleted the last {del_count} activities")
        return

    if m0_text.lower() == "w":
        starting_anchor = now
        await borg.edit_message(m0, "Anchored")
        return

    m = out_pat.match(m0_text)
    if m:
        await borg.edit_message(m0, f"{activity_list_to_str(delta=datetime.timedelta(hours=float(m.group(1) or 24)))}", parse_mode="markdown")
        return

    start: datetime.datetime
    last_act = None
    if starting_anchor == None:
        if not last_act_query.exists():
            await event.reply("The database is empty and also has no starting anchor. Create an anchor by sending 'w'.")
            return
        else:
            last_act = last_act_query.get()
            start = last_act.end
    else:
        start = starting_anchor
        starting_anchor = None

    m = back_pat.match(m0_text)
    if m:
        if last_act != None:
            mins = float(m.group(1) or 20)
            last_act.end -= datetime.timedelta(minutes=mins) # supports negative numbers, too ;D
            last_act.save()
            await borg.edit_message(m0, f"{m0_text} (Pushed last_act.end back by {mins} minutes)")
            return
        else:
            await event.reply("Empty database has no last act.")
            return

    if m0_text == '.':
        if last_act != None:
            ## this design doesn't work too well with deleting records
            last_act.end = now
            await borg.edit_message(m0, f"{str(last_act)} (Updated)")
            last_act.save()
            return
            ## @alt:
            # m0_text = last_act.name
            ##
        else:
            await event.reply("Empty database has no last act.")
            return

    act = Activity(name=m0_text, start=start, end=now)
    await borg.edit_message(m0, f"{str(act)}")
    act.save()

def activity_list_to_str(delta=datetime.timedelta(hours=24)):
    low = datetime.datetime.today() - delta
    res = f"```\nLast {str(delta)}:" # we need a monospace font to justify the columns
    acts = Activity.select().where(Activity.start > low) # @alt .between(low, high)
    acts_agg = {}
    for act in acts:
        act_name = act.name
        act_start = act.start
        act_end = act.end
        dur = relativedelta(act_end, act_start)
        if not act_name in acts_agg:
            acts_agg[act_name] = dur
        else:
            acts_agg[act_name] += dur

    for name, dur in acts_agg.items():
        # @bug emojis break the text justification because they are inherently not monospace
        res += f"""\n    {name + " " * max(0, 20 - len(name))} {dur.hours}:{dur.minutes}"""
    return res + "\n```"
