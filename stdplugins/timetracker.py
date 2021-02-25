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
import json
import yaml
# from fuzzywuzzy import fuzz, process
from rapidfuzz import process, fuzz
try:
    from cfuzzyset import cFuzzySet as FuzzySet
except ImportError:
    from fuzzyset import FuzzySet

# Path.home().joinpath(Path("cellar"))
db_path = Path(
    z('print -r -- "${{attic_private_dir:-$HOME/tmp}}/timetracker.db"').outrs)
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


db.connect()  # @todo? db.close()
db.create_tables([Activity])

timetracker_chat = -1001179162919
# borg.send_message(timetracker_chat, "New timetracker instance initiated")
starting_anchor = None
subs_commands = {
    "👀": "w",
    "dot": ".",
    # "res": "..out",
    # "out": "..out",
    "🧫": "?",
    # habits:
    "/br": ".habit 7 m=1 max=3 brush",
    "/dummy": ".habit 7 m=0 max=10 .dummy",
    "/s": ".habit 7 m=0 max=9 study",
    "/sa": ".habit 7 m=0 max=9 sa",
    "/sl": ".habit 7 m=0 max=12 sleep",
    "/e": ".habit 7 m=0 max=2 exercise",
    "/w": ".habit 7 m=0 max=12 wasted",
    ###
}
subs = {
    "😡": "wasted",
    "wt": "wasted",
    "wtg": "wasted_exploration_gathering",
    "wtgh": "wasted_exploration_github",
    "nos": "wasted_exploration_gathering_nostalgia",
    "worry": "wasted_thinking_worrying",
    "fantasy": "wasted_thinking_fantasy",
    "news": "wasted_news",
    ##
    "untracked": "consciously untracked",
    "unt": "consciously untracked",
    ##
    "📖": "study",
    "s": "study",
    "sc": "chores_self_study",  # study chores: e.g., choosing courses
    "sv": "study_video",
    "sp": "study_peripheral",  # prerequisites, etc
    # uni10
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
    "fin": "study_economics_finance",
    "finv": "study_economics_finance_video",
    # "his": "study_history_history of mathematics",
    # "hisv": "study_history_history of mathematics_video",
    "ai": "study_cs_ai",
    "aiv": "study_cs_ai_video",
    ##
    "💻": "sa",
    "system": "sa",
    "system administration": "sa",
    "sac": "chores_self_sa",
    "sax": "exploration_sa",
    "dev": "sa_development",
    "testman": "sa_development_testing_manual",
    "this": "sa_development_quantified self_timetracker",
    "doc": "sa_product_documentation",
    "eval": "sa_product_evaluation",
    ##
    "🐑": "chores",
    "ch": "chores",
    "cho": "chores_others",
    "exercise": "chores_self_health_exercise",
    "🏃🏽‍♀️": "chores_self_health_exercise",
    "e": "chores_self_health_exercise",
    "r": "chores_self_rest",
    "rest": "chores_self_rest",
    "🍽": "chores_self_rest_eat",
    "eat": "chores_self_rest_eat",
    "eating": "chores_self_rest_eat",
    "ea": "chores_self_rest_eat",
    "brush": "chores_self_health_brush",
    "🦷": "chores_self_health_brush",
    "br": "chores_self_health_brush",
    "floss": "chores_self_health_brush_floss",
    "fl": "chores_self_health_brush_floss",
    "bath": "chores_self_hygiene_bath",
    "🛁": "chores_self_hygiene_bath",
    "ba": "chores_self_hygiene_bath",
    "sl": "sleep",  # putting this under chores will just make using the data harder, no?
    "💤": "sleep",
    ##
    "👥": "social",
    "soc": "social",
    "tlg": "social_online",
    "family": "social_family",
    "fam": "social_family",
    "family others": "social_family_others",
    "famo": "social_family_others",
    ###
    "🎪": "entertainment",
    "fun": "entertainment",
    "game": "entertainment_video games",
    "music": "entertainment_listen_music",
    "vg": "entertainment_video games",
    "coop": "entertainment_video games_coop",
    "wa": "entertainment_watch",
    "movies": "entertainment_watch_movies",
    "anime": "entertainment_watch_anime",
    "anime movies": "entertainment_watch_anime_movies",
    "series": "entertainment_watch_series",
    ##
    "fiction": "entertainment_fiction",
    "fi": "entertainment_fiction",
    "classics": "entertainment_fiction_classics",
    "fanfic": "entertainment_fiction_fanfiction",
    "fanfiction": "entertainment_fiction_fanfiction",
    "fic": "entertainment_fiction_fanfiction",
    ###
    "nf": "nonfiction_reading",
    "technical": "nonfiction_technical_reading",
    "nft": "nonfiction_technical_reading",
    ##
    "docu": "nonfiction_watch_documentaries",
    "lec": "nonfiction_technical_watch_lectures",
    "talk": "nonfiction_watch_talks",
    ###
    "meditation": "meditation_serene",
    "med": "meditation_serene",
    "thinking": "meditation_thinking",
    "th": "meditation_thinking",
    ##
    "go": "going out",
    ##
    "expl": "exploration",
    "🌐": "exploration_targetedLearning",
    "tl": "exploration_targetedLearning",
    "gath": "exploration_gathering",
    "gathmusic": "exploration_gathering_music"
    ##
}

##
# levenshtein is a two-edged sword for our purposes, but I think it's ultimately more intuitive. One huge problem with levenshtein is that it punishes longer strings.
fuzzy_choices = set(list(subs.values()) + list(subs.keys()))
# yes, this is just a subset of fuzzy_choices
fuzzy_choices_str = '\n'.join(set(subs.values()))
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
    res = z("fzf --filter {fuzzyChoice} | ghead -n1",
            cmd_stdin=fuzzy_choices_str).outrs
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
habit_pat = re.compile(
    r"^(?:\.\.?)?habit\s*(?P<t>\d*\.?\d*)?\s+(?:m=(?P<mode>\d+)\s+)?(?:max=(?P<max>\d+\.?\d*)\s+)?(?P<name>.+)$")


# incoming=True causes us to miss stuff that tsend sends by 'ourselves'.
@borg.on(events.NewMessage(chats=[timetracker_chat], forwards=False))
async def process(event):
    m0 = event.message
    await process_msg(m0)


async def process_msg(m0):
    global starting_anchor

    async def edit(text: str, **kwargs):
        if len(text) > 4000:
            text = f"{text[:4000]}\n\n..."
        await borg.edit_message(m0, text, **kwargs)

    async def reply(text: str, **kwargs):
        await m0.reply(text, **kwargs)

    async def send_file(file, **kwargs):
        await borg.send_file(timetracker_chat, file, allow_cache=False, **kwargs)

    async def warn_empty():
        await m0.reply("The empty database has no last act.")

    choiceConfirmed = False

    def text_sub(text):
        nonlocal choiceConfirmed
        text = text.lower()  # iOS capitalizes the first letter
        if text in subs:
            choiceConfirmed = True
            text = subs[text]
        if text in subs_commands:
            choiceConfirmed = True
            text = subs_commands[text]
        if not choiceConfirmed and not text.startswith("."):
            tokens = list(text.split('_'))
            if len(tokens) > 1:
                tokens[0] = text_sub_full(tokens[0])
                choiceConfirmed = True
                text = '_'.join(tokens)
        return text

    def text_sub_finalize(text):
        nonlocal choiceConfirmed
        if text.startswith("."):
            text = text[1:]
        elif not choiceConfirmed:
            text = chooseAct(text)
        return text

    def text_sub_full(text):
        nonlocal choiceConfirmed
        tmp = choiceConfirmed  # out of caution
        choiceConfirmed = False
        res = text_sub_finalize(text_sub(text))
        choiceConfirmed = tmp
        return res

    m0_text = text_sub(m0.text)
    print(f"TT got (raw): {repr(m0.text)}")
    # print(f"TT got: {repr(m0_text)}")
    if not m0.text or m0.text.startswith('#') or m0.text.isspace():  # comments :D
        return "comment"
    elif m0_text == 'man':
        out = yaml.dump(subs_commands) + '\n' + yaml.dump(subs)
        await edit(out)
        return out

    now = datetime.datetime.today()
    last_act_query = Activity.select().order_by(Activity.end.desc())

    m = del_pat.match(m0_text)
    if m:
        del_count = 0
        if m.group(1):
            del_count = Activity.delete().where(Activity.end > (
                now - datetime.timedelta(minutes=float(m.group(1) or 5)))).execute()
        elif last_act_query.exists():
            del_count = last_act_query.get().delete_instance()
        out = f"Deleted the last {del_count} activities"
        await edit(out)
        return out

    if m0_text == "w":
        starting_anchor = now
        out = "Anchored"
        await edit(out)
        return out

    if m0_text == 'debugme':
        Activity.delete().where(Activity.name == 'dummy').execute()
        Activity(name="dummy", start=(now - datetime.timedelta(days=6*30,
                                                               hours=7)), end=(now - datetime.timedelta(days=6*30))).save()
        Activity(name="dummy", start=(now - datetime.timedelta(days=1*30,
                                                               hours=3)), end=(now - datetime.timedelta(days=1*30))).save()
        Activity(name="dummy", start=(now - datetime.timedelta(days=10*30,
                                                               hours=10)), end=(now - datetime.timedelta(days=10*30))).save()
        out = "DEBUG COMMAND"
        await edit(out)
        return out

    m = out_pat.match(m0_text)
    if m:
        hours = m.group(1)
        if hours:
            out = activity_list_to_str_now(delta=datetime.timedelta(hours=float(hours)))
        else:
            low = now.replace(hour=5, minute=0, second=0)
            if low > now:
                low = low - datetime.timedelta(days=1)
            out = activity_list_to_str(low, now)
        await edit(f"{out}", parse_mode="markdown")
        return out

    m = habit_pat.match(m0_text)
    if m:
        habit_name = m.group('name')
        habit_name = text_sub_full(habit_name)
        habit_mode = int(m.group('mode') or 0)
        habit_max = int(m.group('max') or 0)
        habit_delta = datetime.timedelta(
            days=float(m.group('t') or 30))  # days
        habit_data = activity_list_habit_get_now(
            habit_name, delta=habit_delta, mode=habit_mode)
        out = f"{habit_name}\n\n{yaml.dump(habit_data)}"
        habit_data.pop(now.date(), None)
        def mean(numbers):
            numbers = list(numbers)
            return float(sum(numbers)) / max(len(numbers), 1)
        average = mean(v for k, v in habit_data.items())
        out += f"\n\naverage: {round(average, 1)}"
        await edit(out)
        ##
        now = datetime.datetime.now()
        # ~1 day(s) left empty as a buffer
        habit_delta = datetime.timedelta(days=364)
        habit_data = activity_list_habit_get_now(
            habit_name, delta=habit_delta, mode=habit_mode, fill_default=False)
        img = z("gmktemp --suffix .png").outrs
        resolution = 100
        # * we can increase habit_max by 1.2 to be able to still show overwork, but perhaps each habit should that manually
        # * calendarheatmap is designed to handle a single year. Using this `year=now.year` hack, we can render the previous year's progress as well. (Might get us into trouble after 366-day years, but probably not.)
        plot_data = {str(k.replace(year=now.year)): (1 if k.year == now.year else -1) * int(
            min(resolution, resolution * (v/habit_max))) for k, v in habit_data.items()}
        plot_data_json = json.dumps(plot_data)
        # await reply(plot_data_json)
        res = z(
            "calendarheatmap -maxcount {resolution} -colorscale BuGn_9 -colorscalealt Blues_9 -highlight-today '#00ff9d' > {img}", cmd_stdin=plot_data_json)
        if res:
            await send_file(img)
        else:
            await reply(f"Creating heatmap failed with {res.retcode}:\n\n{z.outerr}")
        return out

    last_act = None
    if last_act_query.exists():
        last_act = last_act_query.get()

    m = back_pat.match(m0_text)
    if m:
        if last_act != None:
            mins = float(m.group(1) or 20)
            # supports negative numbers, too ;D
            last_act.end -= datetime.timedelta(minutes=mins)
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
            # this design doesn't work too well with deleting records
            last_act.end = now
            out = f"{str(last_act)} (Updated)"
            await edit(out)
            last_act.save()
            return out
            # @alt:
            # m0_text = last_act.name
            # choiceConfirmed = True
            ##
        else:
            await warn_empty()
            return

    if m0_text == '..':
        # @perf @todo2 this is slow, do it natively
        out = z('borg-tt-last').outerr
        await edit(out)
        return out

    m0_text = text_sub_finalize(m0_text)

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


def activity_list_to_str_now(delta=datetime.timedelta(hours=24)):
    now = datetime.datetime.today()
    low = now - delta
    return activity_list_to_str(low,now)

def activity_list_to_str(low, high):
    acts = Activity.select().where((Activity.start.between(low, high)) | (Activity.end.between(low, high)))
    acts_agg = ActivityDuration("Total")
    for act in acts:
        act_name = act.name
        act_start = max(act.start, low)
        act_end = min(act.end, high)
        dur = relativedelta(act_end, act_start)
        acts_agg.add(dur, list(reversed(act_name.split('_'))))
    # ("TOTAL", total_dur),
    # we need a monospace font to justify the columns
    res = f"```\nSpanning {str(high - low)}; UNACCOUNTED {relativedelta_str(relativedelta(high, low + acts_agg.total_duration))}\n"
    res += str(acts_agg)
    return res + "\n```"


def activity_list_habit_get_now(name: str, delta=datetime.timedelta(days=30), mode=0, fill_default=True):
    # _now means 'now' is 'high'
    high = datetime.datetime.today()
    low = high - delta

    # aligns dates with real life, so that date changes happen at, e.g., 5 AM
    night_passover = datetime.timedelta(hours=5)

    def which_bucket(act):
        if act.name == name or act.name.startswith(name + '_'):
            return (act.start - night_passover).date()
        return None

    buckets = activity_list_buckets_get(
        low, high, which_bucket=which_bucket, mode=mode)
    if mode == 0:
        buckets_dur = {k: round(relativedelta_total_seconds(
            v.total_duration) / 3600, 2) for k, v in buckets.items()}
    elif mode == 1:
        buckets_dur = buckets

    if fill_default:
        interval = datetime.timedelta(days=1)
        while low <= high:
            buckets_dur.setdefault(low.date(), 0)
            low += interval

    return buckets_dur


def activity_list_buckets_get(low, high, which_bucket, mode=0):
    acts = Activity.select().where(Activity.start.between(low, high))
    buckets = {}
    for act in acts:
        bucket_key = which_bucket(act)
        if not bucket_key:
            continue
        if mode == 0:
            bucket = buckets.setdefault(bucket_key, ActivityDuration("Total"))
            dur = relativedelta(act.end, act.start)
            bucket.add(dur, list(reversed(act.name.split('_'))))
        elif mode == 1:  # count mode
            bucket = buckets.setdefault(bucket_key, 0)
            buckets[bucket_key] += 1
    return buckets
