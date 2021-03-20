from telethon import events
import telethon
import traceback
import datetime
from brish import z
import re
import os
from dateutil.relativedelta import relativedelta
# from pathlib import Path
# from peewee import *
from uniborg.util import embed2, send_files
from uniborg.timetracker_util import *
import json
import yaml
# from fuzzywuzzy import fuzz, process
from rapidfuzz import process, fuzz
try:
    from cfuzzyset import cFuzzySet as FuzzySet
except ImportError:
    from fuzzyset import FuzzySet

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
    "/br": ".habit 8 m=1 max=3 brush\n.habit 8 m=1 max=2 floss\n.habit 8 m=1 max=2 mouthwash",
    # "/mw": ".habit 8 m=1 max=2 mouthwash",
    "/dummy": ".habit 8 m=0 max=10 .dummy",
    "/s": ".habit 8 m=0 max=9 study",
    "/sa": ".habit 8 m=0 max=9 sa",
    "/sl": ".habit 8 m=0 max=12 sleep",
    "/e": ".habit 8 m=0 max=2 exercise",
    "/wt": ".habit 8 m=0 max=12 wasted",
    ###
}
suffixes = {
    '-': [0, "wasted"],
    '$': [1, "halfhearted"],
    '+': None,
}
subs = {
    "😡": "wasted",
    "wt": "wasted",
    "bundled": "wasted_overoptimization_bundling",
    "tired": "wasted_tired",
    ##
    # "wtg": "wasted_exploration_gathering",
    # "wtgh": "wasted_exploration_github",
    "nos": "wasted_exploration_gathering_nostalgia",
    ##
    # "wtth": "wasted_thinking",
    "worry": "wasted_thinking_worrying",
    "fantasy": "wasted_thinking_fantasy",
    "selfie": "wasted_thinking_self inspection",
    ##
    "news": "wasted_news",
    # "wtso": "wasted_social_online",
    "wtf": "wasted_social_online_forums",
    "reddit": "wasted_social_online_forums",
    "hn": "wasted_news_hackernews",
    ###
    "untracked": "consciously untracked",
    "unt": "consciously untracked",
    "idk": "consciously untracked_idk",
    "mixed": "consciously untracked_mixed",
    ##
    "📖": "study",
    "s": "study",
    ##
    # study chores: e.g., choosing courses
    "sc": "chores_self_study",
    # "sc": "study_chore",
    ##
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
    ##
    "sac": "sa_chores",
    "sacgh": "sa_chores_github",
    "hw": "sa_chores_hardware",
    # "sac": "chores_self_sa",
    # "sacgh": "chores_self_sa_github",
    # "hw": "chores_self_sa_hardware",
    ##
    # "sax": "exploration_sa",
    "sax": "sa_exploration",
    "dev": "sa_development",
    "sat": "sa_thinking & design",
    "siri": "sa_development_siri",
    "testman": "sa_development_testing_manual",
    "this": "sa_development_quantified self_timetracker",
    "doc": "sa_product_documentation",
    "eval": "sa_product_evaluation",
    ###
    "🐑": "chores",
    "ch": "chores",
    "cho": "chores_others",
    "chfam": "chores_others_family",
    ##
    "cm": "chores_self_commute",
    ##
    "exercise": "chores_self_health_exercise",
    "🏃🏽‍♀️": "chores_self_health_exercise",
    "e": "chores_self_health_exercise",
    ##
    "r": "chores_self_rest",
    "rest": "chores_self_rest",
    "glue": "chores_self_rest_glue",
    "🍽": "chores_self_rest_eat",
    "eat": "chores_self_rest_eat",
    "eating": "chores_self_rest_eat",
    "ea": "chores_self_rest_eat",
    "breakfast": "chores_self_rest_eat_breakfast",
    "lunch": "chores_self_rest_eat_lunch",
    "dinner": "chores_self_rest_eat_dinner",
    ##
    "brush": "chores_self_health_teeth_brush",
    "🦷": "chores_self_health_teeth_brush",
    "br": "chores_self_health_teeth_brush",
    "floss": "chores_self_health_teeth_floss",
    "fl": "chores_self_health_teeth_floss",
    "mw": "chores_self_health_teeth_mouthwash",
    ##
    "bath": "chores_self_hygiene_bath",
    "🛁": "chores_self_hygiene_bath",
    "ba": "chores_self_hygiene_bath",
    "shave": "chores_self_hygiene_hair_shave",
    "hair": "chores_self_hygiene_hair_haircut",
    ##
    "sl": "sleep",  # putting this under chores will just make using the data harder, no?
    "💤": "sleep",
    "waking": "chores_self_rest_wakingup",
    ###
    "👥": "social",
    "soc": "social",
    "tlg": "social_online_telegram",
    "family": "social_family",
    "fam": "social_family",
    "fams": "social_family_s",
    "famfin": "social_family_finance",
    "bills": "social_family_finance_bills",
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
    "xbuy": "exploration_buying",
    "🌐": "exploration_targetedLearning",
    "tl": "exploration_targetedLearning",
    "gath": "exploration_gathering",
    "gathmusic": "exploration_gathering_music"
    ##
}
reminders_immediate = {
    "chores_self_hygiene_bath": "Turn off the heater",
    "sleep": "Clean your eyes",
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
    return await process_msg(m0)


async def reload_tt():
    db.close()
    db.connect()
    await borg.reload_plugin("timetracker")


async def process_msg(*args, **kwargs):
    async with lock_tt:
        return await _process_msg(*args, **kwargs)

async def _process_msg(m0, text_input=False, reload_on_failure=True, out=""):
    global starting_anchor

    async def edit(text: str, **kwargs):
        try:
            if len(text) > 4000:
                text = f"{text[:4000]}\n\n..."
            await borg.edit_message(m0, text, **kwargs)
        except telethon.errors.rpcerrorlist.MessageNotModifiedError:
            pass

    async def reply(text: str, **kwargs):
        await m0.reply(text, **kwargs)

    async def send_file(file, **kwargs):
        if file:
            # await borg.send_file(timetracker_chat, file, allow_cache=False, **kwargs)
            await send_files(timetracker_chat, file, **kwargs)

    async def warn_empty():
        await m0.reply("The empty database has no last act.")

    async def process_reminders(text):
        if text in reminders_immediate:
            rem = reminders_immediate[text]
            out_add(rem, prefix="\n🌈 ")
            await edit(out)
            # await reply(rem)

    choiceConfirmed = False
    delayed_actions = []
    delayed_actions_special = []
    def out_add(text, prefix="\n\n"):
        nonlocal out
        if text:
            if out:
                out += prefix + text
            else:
                out = text

    def text_sub(text):
        nonlocal choiceConfirmed
        nonlocal delayed_actions # @redundant as we do not assign to it

        if not text:
            choiceConfirmed = True
            return out

        while text[-1] in suffixes:
            suffix = text[-1]
            action = suffixes[suffix]
            if action:
                delayed_actions.append(action)
            else:
                delayed_actions_special.append(suffix)
            text = text[:-1]
            if not text:
                choiceConfirmed = True
                return out

        text = text.lower()  # iOS capitalizes the first letter
        if text in subs:
            choiceConfirmed = True
            text = subs[text]
        if text in subs_commands:
            choiceConfirmed = True
            text = subs_commands[text]
        ## MOVED to text_sub_finalize
        # if not choiceConfirmed:
        #     if not text.startswith("."):
        #         tokens = list(text.split('_'))
        #         if len(tokens) > 1:
        #             tokens[0] = text_sub_full(tokens[0])
        #             choiceConfirmed = True
        #             text = '_'.join(tokens)
        ##
        return text

    def text_sub_finalize(text):
        nonlocal choiceConfirmed
        nonlocal delayed_actions

        if text.startswith("."):
            text = text[1:]
        elif not choiceConfirmed:
            tokens = list(text.split('_'))
            if len(tokens) > 1:
                tokens[0] = text_sub_full(tokens[0])
                text = '_'.join(tokens)
            else:
                text = chooseAct(text)
        for action in delayed_actions:
            mode, c = action
            if mode == 0:
                pre = f"{c}_"
                if not text.startswith(pre):
                    text = f"{pre}{text}"
            elif mode == 1:
                post = f"_{c}"
                if not text.endswith(post):
                    text += post
        return text

    def text_sub_full(text):
        nonlocal choiceConfirmed
        nonlocal delayed_actions

        tmp = choiceConfirmed  # out of caution
        choiceConfirmed = False
        tmp2 = delayed_actions
        delayed_actions = []
        # @warn delayed_actions_special currently not reset because I don't think it matters
        res = text_sub_finalize(text_sub(text))
        choiceConfirmed = tmp
        delayed_actions = tmp2
        return res

    try:
            if text_input == False: # not None, but explicit False
                text_input = m0.text
            elif not text_input:
                return out
            if text_input.startswith('#'): # if the input starts with a comment, discard whole input
                return out
            async def multi_commands(text_input):
                nonlocal out
                text_inputs = text_input.split("\n")
                if len(text_inputs) > 1:
                    for text_input in text_inputs:
                        out = await _process_msg(m0, text_input=text_input, reload_on_failure=reload_on_failure, out=out)
                    return True, out
                return False, False

            done, res = await multi_commands(text_input)
            if done:
                return res
            m0_text_raw = z('per2en', cmd_stdin=text_input).outrs
            m0_text = text_sub(m0_text_raw)
            done, res = await multi_commands(m0_text)
            if done:
                return res
            print(f"TT got: {repr(text_input)} -> {repr(m0_text)}")
            if not text_input or text_input.startswith('#') or text_input.isspace():  # comments :D
                # out_add("comment")
                return out
            elif m0_text == 'man':
                out_add(yaml.dump(suffixes) + '\n' + yaml.dump(subs_commands) + '\n' + yaml.dump(subs))
                await edit(out)
                return out
            elif m0_text == '.l':
                await reload_tt()
                out_add("reloaded")
                return out
            elif m0_text == '.error':
                raise Exception(".error invoked")
                return "@impossible"

            now = datetime.datetime.today()
            last_act_query = Activity.select().order_by(Activity.end.desc())
            last_act = None
            if last_act_query.exists():
                last_act = last_act_query.get()

            m = del_pat.match(m0_text)
            if m:
                del_count = 0
                if m.group(1):
                    cutoff = (now - datetime.timedelta(minutes=float(m.group(1) or 5)))
                    del_count = Activity.delete().where((Activity.end > cutoff) | (Activity.start > cutoff)).execute()
                    out_add(f"Deleted the last {del_count} activities")
                elif last_act:
                    out_add(f"Deleted the last act: {last_act}")
                    del_count = last_act.delete_instance()
                    if del_count != 1: # @impossible
                        out_add(f"ERROR: Deletion has failed. Deleted {del_count}.")
                await edit(out)
                return out

            if m0_text == "w":
                starting_anchor = now
                out_add("Anchored")
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
                out_add("DEBUG COMMAND")
                await edit(out)
                return out

            m = out_pat.match(m0_text)
            if m:
                hours = m.group(1)
                res = None
                if hours:
                    res = activity_list_to_str_now(delta=datetime.timedelta(hours=float(hours)))
                else:
                    low = now.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
                    if low > now:
                        low = low - datetime.timedelta(days=1)
                    res = activity_list_to_str(low, now)
                out_add(res['string'])
                await edit(f"{out}", parse_mode="markdown")
                out_links, out_files = visualize_plotly(res['acts_agg'])
                out_links = '\n'.join(out_links)
                out_add(out_links)
                await edit(f"{out}", parse_mode="markdown")
                ##
                if False: # send as album
                    await send_file(out_files)
                else:
                    for f in out_files:
                        await send_file(f)
                ##
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
                out_add(f"\n{habit_name}\n\n{yaml.dump(habit_data)}")
                habit_data.pop(now.date(), None)
                def mean(numbers):
                    numbers = list(numbers)
                    return float(sum(numbers)) / max(len(numbers), 1)
                average = mean(v for k, v in habit_data.items())
                out_add(f"average: {round(average, 1)}", prefix="\n")
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

            m = back_pat.match(m0_text)
            if m:
                if last_act != None:
                    mins = float(m.group(1) or 20)
                    # supports negative numbers, too ;D
                    last_act.end -= datetime.timedelta(minutes=mins)
                    last_act.save()
                    out_add(f"{str(last_act)} (Pushed last_act.end back by {mins} minutes)")
                    await edit(out)
                    return out
                else:
                    await warn_empty()
                    return

            m = rename_pat.match(m0_text)
            if m:
                if last_act != None:
                    last_act.name = text_sub_full(m.group(1))
                    last_act.save()
                    out_add(f"{str(last_act)} (Renamed)")
                    await edit(out)
                    await process_reminders(last_act.name)
                    return out
                else:
                    await warn_empty()
                    return

            if m0_text == '.':
                if last_act != None:
                    # this design doesn't work too well with deleting records
                    last_act.end = now
                    last_act.save()
                    out_add(f"{str(last_act)} (Updated)")
                    await edit(out)
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
                out_add(z('borg-tt-last 10').outerr)
                await edit(out)
                return out

            m0_text = text_sub_finalize(m0_text)

            start: datetime.datetime
            if '+' in delayed_actions_special:
                start = now
                # @warn unless we update last_act_query to also sort by start date, or add an epsilon to either the new act or last_act, the next call to last_act_query might return either of them (theoretically). In practice, it seems last_act is always returned and this zero-timed new act gets ignored. This is pretty much what we want, except it makes hard to correct errors with `.del` etc.
                if last_act != None: # @duplicatedCode
                    last_act.end = now
                    last_act.save()
                    out_add(f"{str(last_act)} (Updated)")
            else:
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
            act.save()
            out_add(str(act))
            await edit(out)
            await process_reminders(act.name)
            return out
    except:
        out_add("\nJulia encountered an exception. :(\n" + traceback.format_exc())
        if reload_on_failure:
            out_add("Reloading ...\n")
            await edit(out)
            await reload_tt()
            return await borg._plugins["timetracker"]._process_msg(m0, reload_on_failure=False, text_input=text_input, out=out)
        else:
            await edit(out)
            return out
