from telethon import events
import datetime
from dateutil.relativedelta import relativedelta
from pathlib import Path
from brish import z
import os
import re
from peewee import *
from uniborg.util import embed2
from uniborg.timetracker_util import *
import json, yaml
# from fuzzywuzzy import fuzz, process
from rapidfuzz import process, fuzz
try:
    from cfuzzyset import cFuzzySet as FuzzySet
except ImportError:
    from fuzzyset import FuzzySet

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
        return f"""{self.name} {relativedelta_str(dur)}"""

db.connect() # @todo? db.close()
db.create_tables([Activity])

timetracker_chat = -1001179162919
# borg.send_message(timetracker_chat, "New timetracker instance initiated")
starting_anchor = None
subs = {
    "👀": "w",
    "dot": ".",
    # "res": "..out",
    # "out": "..out",
    "untracked": "consciously untracked",
    "unt": "consciously untracked",
    "📖": "study",
    "s": "study",
    "sc": "chores_self_study", # study chores: e.g., choosing courses
    "sv": "study_video",
    "sp": "study_peripheral", # prerequisites, etc
    ## uni10
    "p2": "study_physics_physics 2",
    "p2v": "study_physics_physics 2_video",
    "p4": "study_physics_physics 4",
    "p4v": "study_physics_physics 4_video",
    "feyn": "study_physics_feynman",
    "feynman": "study_physics_feynman",
    "st": "study_math_probability and statistics",
    "stv": "study_math_probability and statistics_video",
    "em": "study_math_engineering math",
    "rizmo": "study_math_engineering math",
    "emv": "study_math_engineering math_video",
    "rizmov": "study_math_engineering math_video",
    "his": "study_history_history of mathematics",
    "hisv": "study_history_history of mathematics_video",
    "ai": "study_cs_ai",
    "aiv": "study_cs_ai_video",
    ##
    "exercise": "chores_self_health_exercise",
    "🏃🏽‍♀️": "chores_self_health_exercise",
    "e": "chores_self_health_exercise",
    "🧫": "?",
    "💻": "sa", # System Administration
    "system": "sa",
    "system administration": "sa",
    "this": "sa_quantified self_timetracker",
    "r": "chores_self_rest",
    "rest": "chores_self_rest",
    "🍽": "chores_self_rest_eat",
    "eat": "chores_self_rest_eat",
    "eating": "chores_self_rest_eat",
    "ea": "chores_self_rest_eat",
    "brush": "chores_self_health_brush",
    "🦷": "chores_self_health_brush",
    "br": "chores_self_health_brush",
    "bath": "chores_self_hygiene_bath",
    "🛁": "chores_self_hygiene_bath",
    "ba": "chores_self_hygiene_bath",
    "sl": "sleep", # putting this under chores will just make using the data harder, no?
    "💤": "sleep",
    "👥": "social",
    "soc": "social",
    "tlg": "social_online",
    "family": "social_family",
    "fam": "social_family",
    "family others": "social_family_others",
    "famo": "social_family_others",
    "🎪": "entertainment",
    "fun": "entertainment",
    "game": "entertainment_video games",
    "vg": "entertainment_video games",
    "coop": "entertainment_video games_coop",
    "wa": "entertainment_watch",
    "movies": "entertainment_watch_movies",
    "anime": "entertainment_watch_anime",
    "anime movies": "entertainment_watch_anime_movies",
    "series": "entertainment_watch_series",
    "😡": "wasted",
    "wt": "wasted",
    "nf": "nonfiction",
    "technical": "nonfiction_technical",
    "nft": "nonfiction_technical",
    "fiction": "entertainment_fiction",
    "fi": "entertainment_fiction",
    "classics": "entertainment_fiction_classics",
    "fanfic": "entertainment_fiction_fanfiction",
    "fanfiction": "entertainment_fiction_fanfiction",
    "fic": "entertainment_fiction_fanfiction",
    "meditation": "meditation_serene",
    "med": "meditation_serene",
    "thinking": "meditation_thinking",
    "th": "meditation_thinking",
    "go": "going out",
    "🐑": "chores",
    "ch": "chores",
    "cho": "chores_others",
    "expl": "exploration",
    "🌐": "exploration_targetedLearning",
    "tl": "exploration_targetedLearning",
    "gath": "exploration_gathering"
    }

##
# levenshtein is a two-edged sword for our purposes, but I think it's ultimately more intuitive. One huge problem with levenshtein is that it punishes longer strings.
fuzzy_choices = list(subs.values()) + list(subs.keys())
fuzzy_choices_str = '\n'.join(subs.values())
subs_fuzzy = FuzzySet(fuzzy_choices, use_levenshtein=True)
def chooseAct(fuzzyChoice: str):
    ##
    # https://github.com/seatgeek/fuzzywuzzy/issues/251 : the token versions are somewhat broken
    # https://github.com/maxbachmann/RapidFuzz/issues/76
    # res = process.extractOne(fuzzyChoice, fuzzy_choices, scorer=fuzz.WRatio, processor=(lambda x: x.replace('_',' ')))[0] # fuzz.partial_ratio
    ##
    # res = subs_fuzzy.get(fuzzyChoice)
    # if res:
    #     res = res[0][1]
    ##
    res = z("fzf --filter {fuzzyChoice} | ghead -n1", cmd_stdin=fuzzy_choices_str).outrs
    if not res:
        res = subs_fuzzy.get(fuzzyChoice)
        if res:
            res = res[0][1]
    ##
    if res:
        if res in subs:
            res = subs[res]
        return res
    return fuzzyChoice
    ##
##
del_pat = re.compile(r"^\.\.?del\s*(\d*\.?\d*)$")
rename_pat = re.compile(r"^\.\.?re(?:name)?\s+(.+)$")
out_pat = re.compile(r"^(?:\.\.?)?o(?:ut)?\s*(\d*\.?\d*)$")
back_pat = re.compile(r"^(?:\.\.?)?b(?:ack)?\s*(\-?\d*\.?\d*)$")

@borg.on(events.NewMessage(chats=[timetracker_chat], forwards=False)) # incoming=True causes us to miss stuff that tsend sends by 'ourselves'.
async def process(event):
    m0 = event.message
    await process_msg(m0)

async def process_msg(m0):
    global starting_anchor

    async def edit(text: str, **kwargs):
        await borg.edit_message(m0, text, **kwargs)

    async def warn_empty():
        await m0.reply("The empty database has no last act.")

    choiceConfirmed = False
    def text_sub(text):
        nonlocal choiceConfirmed
        text = text.lower() # iOS capitalizes the first letter
        if text in subs:
            choiceConfirmed = True
            text = subs[text]
        return text

    m0_text = text_sub(m0.text)
    if m0_text.startswith('#'): # comments :D
        return "comment"
    elif m0_text == 'man':
        out = yaml.dump(subs)
        await edit(out)
        return out

    now = datetime.datetime.today()
    last_act_query = Activity.select().order_by(Activity.end.desc())

    m = del_pat.match(m0_text)
    if m:
        del_count = 0
        if m.group(1):
            del_count = Activity.delete().where(Activity.end > (now - datetime.timedelta(minutes=float(m.group(1) or 5)))).execute()
        elif last_act_query.exists():
            del_count = last_act_query.get().delete_instance()
        out = f"Deleted the last {del_count} activities"
        await edit(out)
        return out

    if m0_text.lower() == "w":
        starting_anchor = now
        out = "Anchored"
        await edit(out)
        return out

    m = out_pat.match(m0_text)
    if m:
        out = f"{activity_list_to_str(delta=datetime.timedelta(hours=float(m.group(1) or 24)))}"
        await edit(f"{out}", parse_mode="markdown")
        return out

    last_act = None
    if last_act_query.exists():
        last_act = last_act_query.get()

    m = back_pat.match(m0_text)
    if m:
        if last_act != None:
            mins = float(m.group(1) or 20)
            last_act.end -= datetime.timedelta(minutes=mins) # supports negative numbers, too ;D
            last_act.save()
            out = f"{str(last_act)} (Pushed last_act.end back by {mins} minutes)"
            await edit(out)
            return out
        else:
            await warn_empty()
            return

    m = rename_pat.match(m0_text)
    if m:
        if last_act != None:
            last_act.name = text_sub(m.group(1))
            last_act.save()
            out = f"{str(last_act)} (Renamed)"
            await edit(out)
            return out
        else:
            await warn_empty()
            return

    if m0_text == '.':
        if last_act != None:
            ## this design doesn't work too well with deleting records
            last_act.end = now
            out = f"{str(last_act)} (Updated)"
            await edit(out)
            last_act.save()
            return out
            ## @alt:
            # m0_text = last_act.name
            # choiceConfirmed = True
            ##
        else:
            await warn_empty()
            return

    if m0_text.startswith("."):
        m0_text = m0_text[1:]
    elif not choiceConfirmed:
        m0_text = chooseAct(m0_text)

    start: datetime.datetime
    if starting_anchor == None:
        if last_act == None:
            await m0.reply("The database is empty and also has no starting anchor. Create an anchor by sending 'w'.")
            return
        else:
            start = last_act.end
    else:
        start = starting_anchor
        starting_anchor = None

    act = Activity(name=m0_text, start=start, end=now)
    out = str(act)
    await edit(out)
    act.save()
    return out

def activity_list_to_str(delta=datetime.timedelta(hours=24)):
    now = datetime.datetime.today()
    low = now - delta
    acts = Activity.select().where(Activity.start > low) # @alt .between(low, high)
    acts_agg = ActivityDuration("Total")
    for act in acts:
        act_name = act.name
        act_start = act.start
        act_end = act.end
        dur = relativedelta(act_end, act_start)
        acts_agg.add(dur, list(reversed(act_name.split('_'))))
    # ("TOTAL", total_dur), 
    res = f"```\nLast {str(delta)}; UNACCOUNTED {relativedelta_str(relativedelta(now, low + acts_agg.total_duration))}\n" # we need a monospace font to justify the columns
    res += str(acts_agg)
    return res + "\n```"


