# @hiddenAPI the file's name 'timetracker' is used by borg._plugins and borg.load_plugin
###
from telethon import events
import telethon
import traceback
import datetime
import jdatetime
from brish import z
import re
import os
import copy
from collections import OrderedDict
from icecream import ic

# from pathlib import Path
# from peewee import *
from uniborg.util import embed2, send_files, za
import uniborg.timetracker_util as timetracker_util
from uniborg.timetracker_util import *
import json
import yaml

# from fuzzywuzzy import fuzz, process
from rapidfuzz import process, fuzz

try:
    from cfuzzyset import cFuzzySet as FuzzySet
except ImportError:
    from fuzzyset import FuzzySet

# borg.send_message(timetracker_chat, "New timetracker instance initiated")
starting_anchor = None
aliases = {
    #: The keys are regexes.
    #: Add =\b= (or =\s=) if you want the alias to break on word boundary.
    ##
    r"/o": r"out include=^(?:sa|career)($|_)|(^|_)study($|_) ",
}

subs_commands = {
    "👀": "w",
    "dot": ".",
    # "res": "..out",
    # "out": "..out",
    "🧫": "?",
    ## habits:
    "/br": ".habit 8 m=1 max=3 brush$;br$;\n.habit 8 m=1 max=2 cs1=Blues_9 cs2=PuBu_9 floss$;fl$;\n.habit 8 m=1 max=2 cs1=PuRd_9 cs2=RdPu_9 mouthwash$;",
    # "/mw": ".habit 8 m=1 max=2 mouthwash",
    ##
    #: Use parentheses for regex patterns to ensure that the last char is not interpreted as sth else: `RE:(...)`
    "/dummy": ".habit 8 m=0 max=10 dummy",
    "/s": ".habit 8 m=0 max=12 study$",
    "/u": ".habit 8 m=0 max=12 STUDY_SA_NX",
    "/ssa": r".habit 8 m=0 max=12 RE:^(?:sa|career)($|_)|(^|_)study($|_)",
    "/sa": ".habit 8 m=0 max=9 sa",
    "/x": r".habit 8 m=0 max=12 RE:(^|_)exploration($|_)",
    "/sl": ".habit 8 m=0 max=12 sleep",
    "/sls": ".habit 8 m=2 max=12 sleep",
    "/e": ".habit 8 m=0 max=2 chores_self_health_exercise; exercise$; e$;",
    "/wt": ".habit 8 m=0 max=6 cs1=Reds_9 cs2=RdPu_9 wasted",
    "/hh": ".habit 8 m=0 max=6 cs1=Reds_9 cs2=RdPu_9 halfhearted$;",
    ###
    "/d": "o m=2 r=6 treemap=0",
    "/d30": "o m=2 r=29 treemap=0",
    "/w": "o168 m=2 r=7 treemap=0",
    ##
    # @todo @futurecron0 lcm(2,3,4,7) = 84
    "/s7": "o7 m=3 r=6",
    "/s4": "o4 m=3 r=11",
    "/s3": "o3 m=3 r=14",
    "/s2": "o2 m=3 r=21",
    "/s1": "o1 m=3 r=42",
    "/sall": "/s1\n/s2\n/s3\n/s4\n/s7",
    ###
}
suffixes = {
    "-": [0, "wasted"],
    "O": [0, "outdoors"],
    "$": [1, "halfhearted"],
    "C": [1, "chores"],
    "X": [1, "exploration"],
    "V": [1, "video"],
    "L": [1, "class"],
    "P": [1, "practical"],
    "M": [1, "meeting"],
    "T": [1, "technical"],
    "Th": [1, "thinking"],
    "W": [1, "writing"],
    # "S": [1, "soallab"],
    "+": None,
}
subs = {
    "😡": "wasted",
    "wt": "wasted",
    "bundled": "wasted_overoptimization_bundling",
    "tired": "wasted_tired",
    ##
    # "wtg": "wasted_exploration_gathering",
    # "wtgh": "wasted_exploration_github",
    ##
    # "wtth": "wasted_thinking",
    "worry": "wasted_thinking_worrying",
    "hers": "wasted_thinking_hers",  # حرص
    "fantasy": "wasted_thinking_fantasy",
    ##
    "news": "wasted_news",
    "nw": "wasted_news",
    # "wtso": "wasted_social_online",
    "wtf": "wasted_social_online_forums",
    "reddit": "wasted_social_online_forums_reddit",
    "twitter": "wasted_social_online_forums_twitter",
    "hn": "wasted_news_hackernews",
    "forum": "social_online_forums",
    "stack": "social_online_forums_stackexchange",
    "lw": "social_online_forums_lesswrong",
    "irc": "social_online_forums_irc",
    "discord": "social_online_forums_discord",
    "tumblr": "entertainment_web_pictures_tumblr",
    "tiktok": "entertainment_web_video_tiktok",
    ###
    "untracked": "consciously untracked",
    "unt": "consciously untracked",
    "idk": "consciously untracked_idk",
    "mixed": "consciously untracked_mixed",
    ##
    "c": "career",
    "cx": "career_exploration",
    "apply": "career_apply",
    "ap": "career_apply",
    "work": "career_work",
    ##
    "📖": "study",
    "s": "study",
    "sx": "study_exploration",
    "ta": "study_ta",
    "ra": "study_ra",
    "soal": "study_ra_soal",
    ##
    "drive": "study_driving_car",
    ###
    "sc": "chores_self_study",
    # "sc": "study_chore",
    "scth": "chores_self_study_thinking",
    "homework": "chores_self_study_homework",
    "hw": "chores_self_study_homework",
    "scm": "chores_self_study_communication",
    "sm": "chores_self_study_management",
    "review": "chores_self_study_paper review",
    ###
    "sv": "study_video",
    "sp": "study_peripheral",  # prerequisites, etc
    "eng": "study_languages_english",
    "english": "study_languages_english",
    "speak": "study_languages_english_speaking",
    "write": "study_languages_english_writing",
    "ielts": "study_languages_english_ielts",
    "toefl": "study_languages_english_toefl",
    "tofl": "study_languages_english_toefl",
    "french": "study_languages_french",
    "german": "study_languages_german",
    "japanese": "study_languages_japanese",
    "discrete": "study_math_discrete",
    "analysis": "study_math_analysis",
    "ana": "study_math_analysis",
    "topo": "study_math_topology",
    "calc": "study_math_calculus",
    "opt": "study_math_optimization",
    "convex": "study_math_optimization_convex",
    "info": "study_math_information theory",
    "la": "study_math_linear algebra",
    "algebra": "study_math_algebra",
    "al": "study_math_algebra",
    "smanage": "study_cs_software_management",
    "agile": "study_cs_software_management_agile",
    "scrum": "study_cs_software_management_agile_scrum",
    "kanban": "study_cs_software_management_kanban",
    "automata": "study_cs_automata",
    "medicine": "study_biology_medicine",
    "os": "study_cs_os",
    "arc": "study_cs_computer architecture",
    "dd": "study_cs_digital design",
    "elec": "study_cs_digital electronics",
    "sed": "study_cs_software_design",  # sed: software engineering -> design
    "oop": "study_cs_software_design_oop",
    "func": "study_cs_software_design_functional",
    "pattern": "study_cs_software_design_patterns",
    "para": "study_cs_parallelism",
    "dist": "study_cs_distributed systems",
    "distp": "study_cs_distributed systems_practical",
    "security": "study_cs_security",
    "blockchain": "study_cs_cryptography_blockchains",
    "blc": "study_cs_cryptography_blockchains",
    "db": "study_cs_databases",
    "sql": "study_cs_databases_sql",
    # uni10
    "phys": "study_physics",
    # "p2": "study_physics_physics 2",
    # "p2v": "study_physics_physics 2_video",
    # "p4": "study_physics_physics 4",
    # "p4v": "study_physics_physics 4_video",
    "feyn": "study_physics_feynman",
    "feynman": "study_physics_feynman",
    "st": "study_math_probability and statistics",
    "stat": "study_math_probability and statistics",
    "engm": "study_math_engineering math",
    "rizmo": "study_math_engineering math",
    # "emv": "study_math_engineering math_video",
    # "rizmov": "study_math_engineering math_video",
    "sig": "study_math_signal processing",
    "dsp": "study_math_signal processing_digital",
    "fin": "study_economics_finance",
    "finv": "study_economics_finance_video",
    # "his": "study_history_history of mathematics",
    # "hisv": "study_history_history of mathematics_video",
    "git": "study_cs_version control_git",
    "pl": "study_cs_programming languages",
    "tex": "study_cs_programming languages_tex",
    "golang": "study_cs_programming languages_golang",
    "python": "study_cs_programming languages_python",
    "jvm": "study_cs_programming languages_jvm",
    "scala": "study_cs_programming languages_scala",
    "julia": "study_cs_programming languages_julia",
    "zig": "study_cs_programming languages_zig",
    "perl": "study_cs_programming languages_perl",
    "elixir": "study_cs_programming languages_elixir",
    "cc": "study_cs_programming languages_c",
    "cpp": "study_cs_programming languages_cpp",
    "haskell": "study_cs_programming languages_haskell",
    "ocaml": "study_cs_programming languages_ocaml",
    "m4": "study_cs_programming languages_m4",
    "php": "study_cs_programming languages_php",
    "prolog": "study_cs_programming languages_prolog",
    "ruby": "study_cs_programming languages_ruby",
    # "crystal": "sa_exploration_crystallang",
    "crystal": "study_cs_programming languages_crystal",
    "racket": "study_cs_programming languages_scheme_racket",
    "rkt": "study_cs_programming languages_scheme_racket",
    # "sbcl": "sa_development_commonlisp",
    # "cl": "sa_development_commonlisp",
    "commonlisp": "study_cs_programming languages_commonlisp",
    "clj": "study_cs_programming languages_clojure",
    # "clj": "sa_development_clojure",
    "prompt": "sa_development_prompt engineering",
    "web": "sa_development_web",
    "css": "sa_development_web_css",
    "django": "sa_development_web_django",
    "templating": "study_cs_programming languages_templating",
    "d3": "study_cs_visualization_d3",
    "comp": "study_cs_computation",
    "ds": "study_cs_datastructures",
    "net": "study_cs_network",
    "ai": "study_cs_ai",
    "aiv": "study_cs_ai_video",
    "ml": "study_cs_ai",
    "mlc": "study_cs_ai_ml",  #: ML Classic, ML Course
    "mlv": "study_cs_ai_video",
    "mlp": "study_cs_ai_practical",
    "deep": "study_cs_ai_deep learning",
    "dl": "study_cs_ai_deep learning",
    "ip": "study_cs_ai_image processing",
    "dataproc": "study_cs_data_procurement",
    "mli": "study_cs_ai_practical_ideation",
    "mlt": "study_cs_ai_ml_theory",
    "spml": "study_cs_ai_spml",
    "alignment": "study_cs_ai_safety",
    "mls": "study_cs_ai_safety",
    "rl": "study_cs_ai_rl",
    "plan": "study_cs_ai_gofai_planning",
    "nlp": "study_cs_ai_nlp",
    "nlpp": "study_cs_ai_nlp_practical",
    "llm": "study_cs_ai_nlp_llm",
    "ir": "study_cs_ai_nlp_information retrieval",
    "irui": "study_cs_ai_nlp_information retrieval_output ui",
    ##
    "thesis": "study_cs_ai_thesis",
    "th": "study_cs_ai_thesis",
    "thp": "study_cs_ai_thesis_practical",
    "thesisw": "study_cs_ai_thesis_writing",
    "seminar": "study_cs_ai_thesis_writing_seminar",
    "sem": "study_cs_ai_thesis_writing_seminar",

    "attr": "study_cs_ai_thesis_attribution",
    "attrs": "study_cs_ai_thesis_attribution_survey",
    "contrastive": "study_cs_ai_thesis_attribution_contrastive",
    "crs": "study_cs_ai_thesis_attribution_contrastive",
    "robustness": "study_cs_ai_thesis_robustness",
    "robust": "study_cs_ai_thesis_robustness",
    "dss": "study_cs_ai_thesis_dataset_spurious",
    "filip": "study_cs_ai_thesis_clip_finegrained",
    "medical": "study_cs_ai_thesis_medical",
    ##
    "💻": "sa",
    "system": "sa",
    "system administration": "sa",
    "sas": "sa_social",  # contributing, etc
    "sac": "sa_chores",
    "vpn": "sa_chores_vpn",
    "debug": "sa_chores_debugging",
    "bugre": "sa_chores_debugging_bug report",
    "sahw": "sa_chores_hardware",
    # "sac": "chores_self_sa",
    # "sahw": "chores_self_sa_hardware",
    # "sax": "exploration_sa",
    "sax": "sa_exploration",
    # "android": "sa_exploration_android",
    # "net": "sa_network",
    "dev": "sa_development",
    "testman": "sa_development_testing_manual",
    "sh": "sa_development_nightsh",
    "brish": "sa_development_nightsh_brish",
    "vim": "sa_development_vim",
    "emc": "sa_development_emacs",
    "orgm": "sa_development_emacs_orgmode",
    "archive": "sa_development_archival",
    "archival": "sa_development_archival",
    "borg": "sa_development_borg",
    "this": "sa_development_quantified self_timetracker",
    "android": "sa_development_android",
    "termux": "sa_development_android_termux",
    "siri": "sa_development_siri",
    "hugo": "sa_development_blog_hugo",
    "search": "sa_development_search",
    # "": "sa_development_",
    "sat": "sa_thinking & design",
    "doc": "sa_product_documentation",
    "doco": "sa_product_documentation_contrib",
    "vc": "sa_product_documentation_version control",
    "eval": "sa_product_evaluation",
    "marketing": "sa_product_marketing",
    "market": "sa_product_marketing",
    ###
    "🐑": "chores",
    "ch": "chores",
    "cho": "chores_others",
    "chos": "chores_others_society",
    "chfam": "chores_others_family",
    "chf": "chores_others_family",
    "chs": "chores_others_family_sister",
    "chr": "chores_others_family_relationship",
    "ntr": "chores_others_family_relationship_notes",
    ##
    "creative": "creative",
    "cr": "creative",

    "photography": "creative_photography",
    "camera": "creative_photography",
    "cam": "creative_photography",

    "crai": "creative_ai",
    "crllm": "creative_ai_llm",
    "crl": "creative_ai_llm",
    "crv": "creative_ai_visual",

    "vis": "creative_visual",

    "crw": "creative_writing",

    "memory": "creative_writing_memorial",
    "mem": "creative_writing_memorial",

    "fiw": "creative_writing_fiction",
    "fim": "creative_mentalWriting_fiction",
    "journal": "creative_writing_journal",
    "jrl": "creative_writing_journal",
    ##
    "org": "chores_self_organizational_digital",
    "todo": "chores_self_organizational_digital_todo",
    "in": "chores_self_organizational_digital_todo_inbox",
    "em": "chores_self_organizational_digital_todo_inbox_emails",
    "gh": "chores_self_organizational_digital_todo_inbox_github",  # @renameMe sa_chores_github
    "nt": "chores_self_organizational_digital_notes",
    "digitization": "chores_self_organizational_digital_digitization",
    "scan": "chores_self_organizational_digital_digitization",
    "tidy": "chores_self_organizational_tidying up",
    "house": "chores_self_house",
    "hclean": "chores_self_house_cleaning",
    "vacuum": "chores_self_house_cleaning_vacuum",
    ##
    "redtape": "chores_self_bureaucracy",
    "cfin": "chores_self_finance",
    "charge": "chores_self_finance_charge",
    "bills": "chores_self_finance_bills",
    "tax": "chores_self_finance_bills_tax",
    ##
    "cbuy": "chores_self_buying",
    ##
    "commute": "chores_self_commute",
    "cm": "chores_self_commute",
    "cms": "chores_self_commute_setup",
    ##
    "health": "chores_self_health",
    "healthp": "chores_self_health_pro",
    "dental": "chores_self_health_pro_dental",
    "exercise": "chores_self_health_exercise",
    "🏃🏽‍♀️": "chores_self_health_exercise",
    "e": "chores_self_health_exercise",
    "run": "chores_self_health_exercise_running",
    "step": "chores_self_health_exercise_step",
    ##
    "rest": "chores_self_rest",
    "r": "chores_self_rest",
    "glue": "chores_self_rest_glue",
    "gl": "chores_self_rest_glue",
    "sick": "chores_self_rest_sick",
    "🍽": "chores_self_rest_eat",
    "eat": "chores_self_rest_eat",
    "eating": "chores_self_rest_eat",
    "ea": "chores_self_rest_eat",
    "breakfast": "chores_self_rest_eat_breakfast",
    "lunch": "chores_self_rest_eat_lunch",
    "lu": "chores_self_rest_eat_lunch",
    "dinner": "chores_self_rest_eat_dinner",
    "di": "chores_self_rest_eat_dinner",
    "cook": "chores_self_food_cook",
    "dish": "chores_self_food_dishes",
    "clothes": "chores_self_clothes",
    "setup": "chores_self_setup",
    ##
    "brush": "chores_self_health_teeth_brush",
    "🦷": "chores_self_health_teeth_brush",
    "br": "chores_self_health_teeth_brush",
    "floss": "chores_self_health_teeth_floss",
    "fl": "chores_self_health_teeth_floss",
    "mw": "chores_self_health_teeth_mouthwash",
    ##
    "hygiene": "chores_self_hygiene",
    "nail": "chores_self_hygiene_nails",
    "bath": "chores_self_hygiene_bath",
    "🛁": "chores_self_hygiene_bath",
    "ba": "chores_self_hygiene_bath",
    "shave": "chores_self_hygiene_hair_shave",
    "hair": "chores_self_hygiene_hair_haircut",
    ##
    "sl": "sleep",  # putting this under chores will just make using the data harder, no?
    "💤": "sleep",
    "waking": "chores_self_rest_wakingup",
    "wa": "chores_self_rest_wakingup",
    ###
    "👥": "social_others",
    "soc": "social_others",
    "sov": "social_online_videocall",
    "soa": "social_online_audiocall",
    ##
    "tlg": "social_online_telegram",
    "chat": "social_online_telegram_chat",
    "tlgc": "social_online_telegram_channel",
    ##
    "insta": "social_online_instagram",
    "linkedin": "social_online_linkedin",
    "family": "social_family",
    "fam": "social_family",
    # "fams": "social_family_s",
    "famd": "social_family_discussion",
    "famfin": "social_family_finance",
    "familyothers": "social_family_others",
    "famo": "social_family_others",
    "famr": "social_family_relationship",
    ###
    "🎪": "entertainment",
    "et": "entertainment",
    "fun": "entertainment",
    ##
    "music": "entertainment_listen_music",
    "mu": "entertainment_listen_music",
    ##
    "game": "entertainment_video games",
    "vg": "entertainment_video games",
    "coop": "entertainment_video games_coop",
    ##
    "watch": "entertainment_watch",
    "youtube": "entertainment_watch_youtube",
    "yt": "entertainment_watch_youtube",
    "movies": "entertainment_watch_movies",
    "series": "entertainment_watch_series",
    "anime": "entertainment_watch_anime_series",
    "anim": "entertainment_watch_anime_series",
    "anime movies": "entertainment_watch_anime_movies",
    "amov": "entertainment_watch_anime_movies",
    "amv": "entertainment_watch_music videos_anime",
    "muv": "entertainment_watch_music videos",
    ##
    "fiction": "entertainment_fiction",
    "fi": "entertainment_fiction",
    "classics": "entertainment_fiction_classics",
    "fanfic": "entertainment_fiction_fanfiction",
    "fanfiction": "entertainment_fiction_fanfiction",
    "fic": "entertainment_fiction_fanfiction",
    "pth": "entertainment_postthinking",
    ###
    "nf": "nonfiction_reading",
    "technical": "nonfiction_technical_reading",
    "nft": "nonfiction_technical_reading",
    # "mla": "nonfiction_ml_applications",
    ##
    "nfl": "nonfiction_listening",
    "audiobook": "nonfiction_listening_audiobooks",
    "ab": "nonfiction_listening_audiobooks",
    "podcast": "nonfiction_listening_podcasts",
    "pdc": "nonfiction_listening_podcasts",
    "nftl": "nonfiction_technical_listening",
    ##
    "nfw": "nonfiction_watch",
    "docu": "nonfiction_watch_documentaries",
    "nfwl": "nonfiction_watch_lectures",
    "ttcv": "nonfiction_watch_lectures",
    "ted": "nonfiction_watch_talks",
    "nftw": "nonfiction_technical_watch",
    "lec": "nonfiction_technical_watch_lectures",
    "talk": "nonfiction_technical_watch_talks",
    ###
    "meditation": "meditation_serene",
    "med": "meditation_serene",
    "thinking": "meditation_thinking",
    "th": "meditation_thinking",
    "counsel": "meditation_thinking_counseling_llm",
    "cl": "meditation_thinking_counseling_llm",
    "clf": "meditation_thinking_counseling_friends",
    "ang": "meditation_thinking_angry",
    "thl": "meditation_thinking_loose",
    "selfie": "meditation_thinking_self inspection",
    "qs": "meditation_thinking_self inspection_quantified self",
    "sched": "meditation_thinking_scheduling",
    ##
    "go": "outdoors",
    "going out": "outdoors",
    "sit": "outdoors_sitting",
    "walk": "outdoors_walking",
    "walking": "outdoors_walking",
    "park": "outdoors_walking_park",
    ##
    "exp": "exploration",
    "expl": "exploration",
    "xbuy": "exploration_buying",
    "🌐": "exploration_targetedLearning",
    "tl": "exploration_targetedLearning",
    ##
    "gath": "exploration_gathering",  # <- outreach
    "ga": "exploration_gathering",
    # "gathmu": "exploration_gathering_music",
    "gamu": "exploration_gathering_music",
    # "gathg": "exploration_gathering_games",
    "gavg": "exploration_gathering_games",
    "gafi": "exploration_gathering_fiction",
    # "gathf": "exploration_gathering_fiction",
    "gaf": "exploration_gathering_fiction",
    "ganf": "exploration_gathering_nonfiction",
    # "gatha": "exploration_gathering_anime",
    "gaa": "exploration_gathering_anime",
    "gaanim": "exploration_gathering_anime",
    "gaanime": "exploration_gathering_anime",
    # "gathmo": "exploration_gathering_movies",
    "gamo": "exploration_gathering_movies",
    "gamov": "exploration_gathering_movies",
    "gaseries": "exploration_gathering_series",
    # "gaths": "exploration_gathering_series",
    "gas": "exploration_gathering_series",

    "nostalgia": "exploration_gathering_nostalgia",
    "nos": "exploration_gathering_nostalgia",
    ##
}

subs_additional = {
    "chores_self_rest_eat_lunch_family",
    "chores_self_rest_eat_dinner_family",
}

reminders_immediate = {
    "chores_self_hygiene_bath": "Leaf; heater",
    # "sleep": "Clean your eyes",
    "sleep": "Introspect your previous day",
}


##
def load_strlist(path, default):
    try:
        with open(path, "r") as f:
            # Perhaps skip empty lines?
            return [line.strip() for line in f.readlines()]
    except FileNotFoundError:
        return default
    except:
        logger.warn(
            f"Could not load strlist from {repr(path)}:\n{traceback.format_exc()}"
        )
        return default


def save_strlist(path, strlist, force=False):
    if force or strlist:
        try:
            # 'w' for only writing (an existing file with the same name will be erased)
            with open(path, "w") as f:
                return f.write("\n".join(strlist))
        except:
            logger.warn(
                f"Could not save strlist to {repr(path)}:\n{traceback.format_exc()}"
            )
            return None


##
fuzzy_choices = None
fuzzy_choices_str = None
subs_fuzzy = None
user_choices = set()


def add_user_choice(choice):
    global fuzzy_choices, fuzzy_choices_str, subs_fuzzy, user_choices
    if not (choice in fuzzy_choices):
        fuzzy_choices.add(choice)
        user_choices.add(choice)
        fuzzy_choices_str += f"\n{choice}"
        save_fuzzy_choices()
        logger.info(f"Added user choice: {choice}")


def load_fuzzy_choices():
    global fuzzy_choices, fuzzy_choices_str, subs_fuzzy, user_choices
    save_fuzzy_choices(force=True)
    user_choices = set(load_strlist(user_choices_path, user_choices))
    fuzzy_choices = set(list(subs.values())).union(subs_additional)  # list(subs.keys())
    user_choices = user_choices.difference(fuzzy_choices)  # remove redundant entries
    fuzzy_choices = fuzzy_choices.union(user_choices)
    fuzzy_choices_str = "\n".join(fuzzy_choices)
    ##
    subs_fuzzy = FuzzySet(fuzzy_choices, use_levenshtein=True)
    # levenshtein is a two-edged sword for our purposes, but I think it's ultimately more intuitive. One huge problem with levenshtein is that it punishes longer strings.
    ##


last_saved = datetime.datetime.today()


def save_fuzzy_choices(
    force=True,
):  # why not just save every single time? It's not like it's a bottleneck ...
    # to remove from this, first stop borg, then manually edit the file.
    global last_saved
    now = datetime.datetime.today()
    if force or (now - last_saved >= datetime.timedelta(hours=0.5)):
        # @maybe save msg2act here as well? I am holding back on this bloat until proven needed ...
        save_strlist(user_choices_path, sorted(user_choices))
        last_saved = now


load_fuzzy_choices()


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
# @todo migrate patterns to a grammar: /Users/evar/Base/_Code/uni/stochastic/lark_playground/lp1.py
del_pat = re.compile(r"^\.\.?del\s*(\d*\.?\d*)$")
rename_pat = re.compile(r"^\.\.?re(?:name)?\s+(.+)$")
out_pat = re.compile(
    r"^(?:\.\.?)?o(?:ut)?\s*(?P<t>\d*\.?\d*)?\s*(?:m=(?P<mode>\d+))?\s*(?:include=(?P<include_acts>\S*))?(?:exclude=(?P<skip_acts>\S*))?\s*(?:r=(?P<repeat>\d+))?\s*(?:cmap=(?P<cmap>\S+))?\s*(?:treemap=(?P<treemap>\d+))?\s*(?:(?:h(?:ours?)?=)?(?P<hours>\d+\.?\d*))?\s*$"
)
back_pat = re.compile(r"^(?:\.\.?)?b(?:ack)?\s*(?P<eq>=)?(?P<val>\-?\d*\.?\d*)$")
habit_pat = re.compile(
    r"^(?:\.\.?)?habit\s*"
    + r"(?P<t>\d*\.?\d*)?\s*"
    + r"(?:m=(?P<mode>\d+)\s*)?"
    + r"(?:max=(?P<max>\d+\.?\d*)\s*)?"
    + r"(?:cs1=(?P<cs1>\S+)\s*)?"
    + r"(?:cs2=(?P<cs2>\S+)\s*)?"
    + r"(?P<name>.+)$"
)


# incoming=True causes us to miss stuff that tsend sends by 'ourselves'.
@borg.on(events.NewMessage(chats=[timetracker_chat], forwards=False))
async def process(event):
    m0 = event.message
    return await process_msg(m0)


async def reload_tt_prepare():
    save_fuzzy_choices(force=True)
    db.close()
    db.connect()


async def reload_tt():
    # calls reload_tt_prepare itself
    await borg.reload_plugin("timetracker")


async def process_msg(*args, **kwargs):
    await lock_tt.acquire()
    try:
        return await _process_msg(*args, **kwargs)
    finally:
        lock_tt.release()


async def _process_msg(
    m0, text_input=False, reload_on_failure=True, out="", received_at=None
):
    global starting_anchor

    m0_id = m0.id

    def set_msg_act(some_act):
        msg2act[m0_id] = some_act.id

    async def edit(text: str, truncate=True, **kwargs):
        try:
            # if not text: # might be sending files
            # return

            text_raw = text
            if len(text) > 4000:
                if truncate:
                    text = f"{text[:4000]}\n\n..."
                else:
                    text = text[:4000]

            await borg.edit_message(m0, text, **kwargs)
            if not truncate:
                await reply(
                    text_raw[4000:]
                )  # kwargs should not apply to a mere text message

        except telethon.errors.rpcerrorlist.MessageNotModifiedError:
            pass

    async def reply(text: str, **kwargs):
        if not text:  # files are send via send_file
            return

        text = text.strip()
        if len(text) > 4000:
            await m0.reply(text[:4000], **kwargs)
            await reply(text[4000:])  # kwargs should not apply to a mere text message
        else:
            await m0.reply(text, **kwargs)

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
        nonlocal delayed_actions  # @redundant as we do not assign to it

        text = text.strip()
        if not text:
            choiceConfirmed = True
            return out

        ###
        #: @badDesign @todo3 these suffixes are only relevant for adding new acts and renaming them, but they are acted on globally ...

        delayed_actions_ = []
        delayed_actions_special_ = []

        sorted_suffixes = sorted(suffixes.keys(), key=len, reverse=True)
        #: Sort suffixes by length in descending order

        while len(text) >= 2:
            #: Find matching suffix, checking longest first
            matching_suffix = None
            for suffix in sorted_suffixes:
                if text.endswith(suffix) and len(text[:-len(suffix)]) >= 1:
                    matching_suffix = suffix
                    break

            if not matching_suffix:
                break

            action = suffixes[matching_suffix]
            if action:
                delayed_actions_.append(action)
            else:
                delayed_actions_special_.append(matching_suffix)

            text = text[:-len(matching_suffix)]
            if not text:
                choiceConfirmed = True
                return out

        #: Reverse and add the actions to the main lists.
        #: This way, using suffixes remains intuitive for the user.
        delayed_actions.extend(reversed(delayed_actions_))
        delayed_actions_special.extend(reversed(delayed_actions_special_))
        del delayed_actions_
        del delayed_actions_special_
        ###
        if not text.startswith("."):
            text = text.lower()  #: iOS capitalizes the first letter

        for alias in aliases:
            m = re.match(alias, text)
            if m:
                length_of_match = m.end() - m.start()
                text = f"{aliases[alias]}{text[length_of_match:]}"

        if text in subs:
            choiceConfirmed = True
            text = subs[text]

        if text in subs_additional:
            choiceConfirmed = True

        if text in subs_commands:
            choiceConfirmed = True
            text = subs_commands[text]

        return text

    def text_sub_finalize(text):
        nonlocal choiceConfirmed
        nonlocal delayed_actions

        if text.startswith("."):  # allows explicit escape from further processing
            text = text[1:]
            add_user_choice(text)
        elif not choiceConfirmed:
            if not (activity_child_separator in text):
                text = text.replace(" ", activity_child_separator)

            tokens = list(text.split(activity_child_separator))
            if len(tokens) > 1:
                tokens[0] = text_sub_full(tokens[0])
                text = activity_child_separator.join(tokens)
                add_user_choice(text)
            else:
                text = chooseAct(text)
        for action in delayed_actions:
            mode, c = action
            if mode == 0:
                pre = f"{c}{activity_child_separator}"
                if not text.startswith(pre):
                    text = f"{pre}{text}"
            elif mode == 1:
                post = f"{activity_child_separator}{c}"
                if not text.endswith(post):
                    text += post
        return text

    def text_sub_full(text, reset_delayed_actions=True):
        nonlocal choiceConfirmed
        nonlocal delayed_actions

        tmp = choiceConfirmed  # out of caution
        choiceConfirmed = False
        if reset_delayed_actions:
            tmp2 = delayed_actions
            delayed_actions = []
            # @warn delayed_actions_special currently does not reset because I don't think it matters

        res = text_sub_finalize(text_sub(text))
        choiceConfirmed = tmp
        if reset_delayed_actions:
            delayed_actions = tmp2

        return res

    try:
        if text_input == False:  # not None, but explicit False
            text_input = m0.text
        elif not text_input:
            return out
        if text_input.startswith(
            "#"
        ):  # if the input starts with a comment, discard whole input
            return out

        rep_id = m0.reply_to_msg_id
        if rep_id:
            act_id = msg2act.get(rep_id, None)
            if not act_id:
                out_add(
                    f"The message you replied to did not have its id stored in msg2act."
                )
                await edit(out)
                return out
            else:
                q = Activity.select().where(
                    Activity.id == act_id
                )  # this can still be a new record if the record we are trying to get was the last one when it was deleted, as the ids just increment from the last one and are not unique when deletion is concerned
                if q.exists():
                    act_replied_to = q.get()
                    if (
                        not received_at
                    ):  # the first command of a replied_to message will not have this set, but the subsequent commands will reuse the one we set for the first.
                        received_at = copy.copy(act_replied_to.end)

                        # last_act = act_replied_to # the last_act will be set correctly by received_at; We can't set it explicitly, as it can't be passed through 'multi_commands'.
                else:
                    out_add(
                        f"The message you replied to has had its associated act deleted!"
                    )
                    await edit(out)
                    return out

        async def multi_commands(text_input):
            nonlocal out
            text_inputs = text_input.split("\n")
            if len(text_inputs) > 1:
                for text_input in text_inputs:
                    out = await _process_msg(
                        m0,
                        text_input=text_input,
                        reload_on_failure=reload_on_failure,
                        out=out,
                        received_at=received_at,
                    )
                return True, out
            return False, False

        done, res = await multi_commands(text_input)
        if done:
            return res

        persian_exclusive_chars = list(
            "ضصثقفغعهخحجچشسیبلاتنمکگظطزرذدپوؤئيإأآة»«؛كٓژٰ\u200cٔء؟٬٫﷼٪×،ـ۱۲۳۴۵۶۷۸۹۰"
        )
        m0_text_raw = text_input
        if any(c in m0_text_raw for c in persian_exclusive_chars):
            m0_text_raw = z("per2en", cmd_stdin=m0_text_raw).outrs
        m0_text = text_sub(m0_text_raw)
        done, res = await multi_commands(m0_text)
        if done:
            return res
        print(f"TT got: {repr(text_input)} -> {repr(m0_text)}")
        if (
            not text_input or text_input.startswith("#") or text_input.isspace()
        ):  # comments :D
            # out_add("comment")
            return out
        elif m0_text == "man":
            out_add(
                yaml.dump(suffixes)
                + "\n"
                + yaml.dump(subs_commands)
                + "\n"
                + yaml.dump(subs)
                + "\n"
                + yaml.dump(list(subs_additional))
                # + "\n"
                # + yaml.dump(sorted(user_choices))
            )
            await edit(out, truncate=False)
            return out
        elif m0_text == ".l":
            await reload_tt()
            out_add("reloaded")
            return out
        elif m0_text == ".error":
            raise Exception(".error invoked")
            return "@impossible"

        if not received_at:  # None, "" are both acceptable as null
            received_at = datetime.datetime.today()
        else:
            print(f"_process_msg: received_at={received_at}")
            pass

        last_act = None
        if last_act:
            pass
        else:
            # last_act_query = Activity.select().order_by(Activity.end.desc())
            last_act_query = (
                Activity.select()
                .where(Activity.end <= received_at)
                .order_by(Activity.end.desc())
            )
            last_act = None
            if last_act_query.exists():
                last_act = last_act_query.get()

        if m0_text in (".show", ".sh"):
            out_add(f"last_act: {last_act}")
            await edit(out)
            return out

        m = del_pat.match(m0_text)
        if m:
            del_count = 0
            if m.group(1):
                out_add(z("backup-file {timetracker_db_path}").assert_zero.outrs)
                # test -z ${{functions[backup-file]}} ||
                ##
                cutoff = received_at - datetime.timedelta(
                    minutes=float(m.group(1) or 5)
                )
                ##
                # (Activity.end > cutoff) |
                del_count = (
                    Activity.delete().where(
                        (Activity.start > cutoff) & (Activity.start <= received_at)
                    )
                    # .where((Activity.start > cutoff))
                    .execute()
                )
                ##
                out_add(
                    f"Deleted the last {del_count} activities (cutoff={cutoff}, received_at={received_at}"
                )
            elif last_act:
                out_add(f"Deleted the last act: {last_act}")
                del_count = last_act.delete_instance()
                if del_count != 1:  # @impossible
                    out_add(f"ERROR: Deletion has failed. Deleted {del_count}.")

            await edit(out)
            return out

        if m0_text == "w":
            starting_anchor = received_at
            out_add(f"Anchored to {starting_anchor}")
            await edit(out)
            return out

        if m0_text == "debugme":
            Activity.delete().where(Activity.name == "dummy").execute()
            Activity(
                name="dummy",
                start=(received_at - datetime.timedelta(days=6 * 30, hours=7)),
                end=(received_at - datetime.timedelta(days=6 * 30)),
            ).save()
            Activity(
                name="dummy",
                start=(received_at - datetime.timedelta(days=1 * 30, hours=3)),
                end=(received_at - datetime.timedelta(days=1 * 30)),
            ).save()
            Activity(
                name="dummy",
                start=(received_at - datetime.timedelta(days=10 * 30, hours=10)),
                end=(received_at - datetime.timedelta(days=10 * 30)),
            ).save()
            out_add("DEBUG COMMAND")
            await edit(out)
            return out

        m = out_pat.match(m0_text)
        if m:
            output_mode = int(m.group("mode") or 1)
            treemap_enabled = bool(int(m.group("treemap") or 1))
            repeat = int(m.group("repeat") or 0)
            cmap = m.group("cmap")

            include_acts = m.group("include_acts")
            if include_acts is not None:
                if include_acts:
                    include_acts = [re.compile(include_acts)]
                else:
                    include_acts = []

            skip_acts = m.group("skip_acts")
            if skip_acts is not None:
                if skip_acts:
                    skip_acts = [re.compile(skip_acts)]
                else:
                    skip_acts = []

            hours = m.group("t")
            if not hours:
                hours = m.group("hours")
                #: This allows us to input this argument in two different positions.

            res = None

            async def send_plots(out_links, out_files):
                out_links = "\n".join(out_links)
                out_add(out_links, prefix="\n")
                await edit(f"{out}", parse_mode="markdown")
                ##
                if False:  # send as album
                    await send_file(out_files)
                else:
                    for f in out_files:
                        await send_file(f)
                ##

            async def report(
                hours=None,
                output_mode=1,
                received_at=None,
                title=None,
                skip_acts=None,
                include_acts=None,
            ):
                if skip_acts is None:
                    skip_acts = timetracker_util.skip_acts_default
                else:
                    out_add(f"skip_acts: {skip_acts}")

                if include_acts:
                    out_add(f"include_acts: {include_acts}")

                if not received_at:
                    out_add("report: received_at is empty")
                    await edit(f"{out}", parse_mode="markdown")
                    return

                if output_mode in (3,):
                    out_add("Generating stacked area plots ...")
                    await edit(f"{out}", parse_mode="markdown")
                    days = float(hours or 7)
                    a = stacked_area_get_act_roots(
                        repeat=(repeat or 20),
                        interval=datetime.timedelta(days=days),
                        received_at=received_at,
                    )
                    try:
                        lock_tt.release()
                        out_links, out_files = await visualize_stacked_area(
                            a, days=days, cmap=cmap
                        )
                        await send_plots(out_links, out_files)
                    finally:
                        await lock_tt.acquire()

                if hours:
                    res = activity_list_to_str_now(
                        delta=datetime.timedelta(hours=float(hours)),
                        received_at=received_at,
                        skip_acts=skip_acts,
                        include_acts=include_acts,
                    )
                else:
                    low = received_at.replace(
                        hour=DAY_START, minute=0, second=0, microsecond=0
                    )
                    if low > received_at:
                        low = low - datetime.timedelta(days=1)
                    res = activity_list_to_str(
                        low,
                        received_at,
                        skip_acts=skip_acts,
                        include_acts=include_acts,
                    )
                    if timedelta_total_seconds(res["acts_agg"].total_duration) == 0:
                        out_add("report: acts_agg is zero.")
                        await edit(f"{out}", parse_mode="markdown")
                        return

                if output_mode in (0, 1):
                    out_add(res["string"])
                    await edit(f"{out}", parse_mode="markdown")

                if output_mode in (1, 2):
                    out_add(f"Generating plots ...", prefix="\n")
                    await edit(f"{out}", parse_mode="markdown")

                    try:
                        lock_tt.release()
                        out_links, out_files = await visualize_plotly(
                            res["acts_agg"],
                            title=title,
                            treemap=treemap_enabled,
                            skip_acts=skip_acts,
                            include_acts=include_acts,
                        )
                        await send_plots(out_links, out_files)
                    finally:
                        await lock_tt.acquire()

            fake_received_at = received_at
            for i in range(0, repeat + 1):
                title = None
                if repeat > 0 and not (output_mode in (3,)):
                    title = f"Reporting (repeat={i}, hours={hours}, received_at={fake_received_at}):"

                if i > 0:
                    out_add(title)
                    # await reply(title)

                await report(
                    hours=hours,
                    output_mode=output_mode,
                    received_at=fake_received_at,
                    title=title,
                    skip_acts=skip_acts,
                    include_acts=include_acts,
                )
                if output_mode in (3,):
                    break

                fake_received_at = fake_received_at - datetime.timedelta(
                    hours=float(hours or 24)
                )
                fake_received_at = fake_received_at.replace(
                    hour=(DAY_START - 1), minute=59, second=59, microsecond=0
                )

            return out

        ic(m0_text)
        m = habit_pat.match(m0_text)
        if m:
            habit_name = m.group("name")
            habit_mode = int(m.group("mode") or 0)

            if habit_name.startswith("RE:"):
                habit_name = habit_name[3:]

                out_add(f"regex: {habit_name}")

                habit_name = [re.compile(habit_name)]
            else:
                habit_name = habit_name.split(";")
                habit_name = [
                    name.strip() for name in habit_name if name and not name.isspace()
                ]
                # habit_name = [text_sub_full(name) for name in habit_name]
                out_add(f"{'; '.join(habit_name)}")

            habit_max = int(m.group("max") or 0)
            habit_delta = datetime.timedelta(days=float(m.group("t") or 30))  # days
            correct_overlap = True
            day_start = DAY_START
            negative_previous_year = True
            colorscheme1 = "BuGn_9"
            colorscheme2 = "Blues_9"
            if habit_mode == 2:
                correct_overlap = False
                day_start = 15
                negative_previous_year = False
                # colorscheme1 = 'PuRd_9'
                # colorscheme2 = 'PuBu_9'
                ##
                colorscheme1 = "PuRd_9"
                colorscheme2 = "YlGnBu_9"
                ##
                neutral_start = 23
                neutral_start = 24 + 0
                # neutral_start = 24 + 3

            colorscheme1 = m.group("cs1") or colorscheme1
            colorscheme2 = m.group("cs2") or colorscheme2
            habit_data = activity_list_habit_get_now(
                habit_name,
                delta=habit_delta,
                mode=habit_mode,
                day_start=day_start,
                correct_overlap=correct_overlap,
                received_at=received_at,
            )

            def raw_acts_to_start_offset(habit_data):
                tmp = habit_data
                habit_data = dict()
                for date, act_list in tmp.items():
                    if len(act_list) == 0:
                        start_offset = 0
                    else:
                        s = act_list[0].start
                        s = s - s.replace(hour=0, minute=0, second=0, microsecond=0)
                        s = s.total_seconds() / 3600.0
                        if s < day_start:
                            s += 24
                        start_offset = s - neutral_start

                    habit_data[date] = round(start_offset, 1)

                return habit_data

            if habit_mode == 2:
                habit_data = raw_acts_to_start_offset(habit_data)

            ##
            jalali_p = True
            habit_data_str = []
            for gregorian_date, value in habit_data.items():
                jalali_date = jdatetime.date.fromgregorian(date=gregorian_date)

                if jalali_p:
                    date_str = f"""{jalali_date.strftime("%a, %d %b %Y")}"""
                else:
                    date_str = f"""{gregorian_date.strftime("%a, %d %b %Y")}"""

                value_str = format_hours(value)

                habit_data_str.append(f"{date_str}: {value_str}")

            habit_data_str = "\n".join(reversed(habit_data_str))
            # ic(habit_data_str)
            #: somehow, we do not need to add newlines before/after =```=s.
            out_add(f"```{habit_data_str}```")
            ##

            def mean(numbers):
                numbers = list(numbers)
                return float(sum(numbers)) / max(len(numbers), 1)

            habit_data_with_current_day = OrderedDict(habit_data)  #: copies
            del habit_data_with_current_day[min(habit_data.keys())]
            #: pops the oldest item
            #: We have 8 days for a duration of week as we want to exclude the current day. But for =with_current_day=, we do NOT want to do that, so we pop the extra item.
            # ic(habit_data, habit_data_with_current_day)

            average_with_current_day = mean(
                v for k, v in habit_data_with_current_day.items()
            )

            habit_data.pop(received_at.date(), None)
            #: This removes the current day from =habit_data=.

            average = mean(v for k, v in habit_data.items())
            out_add(
                f"average: {format_hours(average)}\naverage (including today): {format_hours(average_with_current_day)}",
                prefix="\n",
            )
            await edit(out)
            ##
            # ~1 day(s) left empty as a buffer
            habit_delta = datetime.timedelta(days=364)
            habit_data = activity_list_habit_get_now(
                habit_name,
                delta=habit_delta,
                mode=habit_mode,
                day_start=day_start,
                correct_overlap=correct_overlap,
                fill_default=False,
                received_at=received_at,
            )
            if habit_mode == 2:
                habit_data = raw_acts_to_start_offset(habit_data)

            img = z("gmktemp --suffix .png").outrs
            resolution = 100
            # * we can increase habit_max by 1.2 to be able to still show overwork, but perhaps each habit should that manually
            # * calendarheatmap is designed to handle a single year. Using this `year=received_at.year` hack, we can render the previous year's progress as well. (Might get us into trouble after 366-day years, but probably not.)
            plot_data = {
                str(k.replace(year=received_at.year)): (
                    1
                    if (not negative_previous_year or k.year == received_at.year)
                    else -1
                )
                * int(max(-resolution, min(resolution, resolution * (v / habit_max))))
                for k, v in habit_data.items()
            }
            plot_data_json = json.dumps(plot_data)
            # await reply(plot_data_json)
            try:
                lock_tt.release()
                # await reply("lock released")
                res = await za(
                    "calendarheatmap -maxcount {resolution} -colorscale {colorscheme1} -colorscalealt {colorscheme2} -highlight-today '#00ff9d' > {img}",
                    cmd_stdin=plot_data_json,
                )
                if res:
                    await send_file(img)
                else:
                    await reply(
                        f"Creating heatmap failed with {res.retcode}:\n\n{res.outerr}"
                    )
                return out
            finally:
                await lock_tt.acquire()

        m = back_pat.match(m0_text)
        if m:
            if last_act != None:
                eq_mode = m.group("eq")
                mins = float(m.group("val") or 20)
                mins_td = datetime.timedelta(minutes=mins)
                if eq_mode:
                    last_act.end = last_act.start + mins_td
                    res = (
                        f"{str(last_act)} (Set last_act.end to last_act.start + {mins})"
                    )
                else:
                    # supports negative numbers, too ;D
                    last_act.end -= mins_td
                    res = (
                        f"{str(last_act)} (Pushed last_act.end back by {mins} minutes)"
                    )

                if last_act.end < last_act.start:
                    out_add(f"Canceled: {res}")
                    await edit(out)
                    return out
                last_act.save()
                set_msg_act(last_act)
                out_add(res)
                await edit(out)
                return out
            else:
                await warn_empty()
                return

        m = rename_pat.match(m0_text)
        if m:
            if last_act != None:
                last_act.name = text_sub_full(m.group(1), reset_delayed_actions=False)
                last_act.save()
                set_msg_act(last_act)
                out_add(f"{str(last_act)} (Renamed)")
                await edit(out)
                await process_reminders(last_act.name)
                return out
            else:
                await warn_empty()
                return

        async def update_to_now():
            amount = received_at - last_act.end
            last_act.end = received_at
            last_act.save()
            set_msg_act(last_act)
            out_add(
                f"{str(last_act)} (Updated by {int(round(amount.total_seconds()/60.0, 0))} minutes)"
            )
            await edit(out)
            await process_reminders(last_act.name)
            return out

        if m0_text == ".":
            if last_act != None:
                return await update_to_now()
            else:
                await warn_empty()
                return

        if m0_text == "..":
            # @perf @todo2 this is slow, do it natively
            out_add(z("borg-tt-last 10").outerr)
            await edit(out)
            return out

        m0_text = text_sub_finalize(m0_text)

        start: datetime.datetime
        if "+" in delayed_actions_special:
            start = received_at
            # @warn unless we update last_act_query to also sort by start date, or add an epsilon to either the new act or last_act, the next call to last_act_query might return either of them (theoretically). In practice, it seems last_act is always returned and this zero-timed new act gets ignored. This is pretty much what we want, except it makes it hard to correct errors with `.del` etc.
            if last_act != None:
                await update_to_now()

        else:
            if starting_anchor == None:
                if last_act == None:
                    await m0.reply(
                        "The database is empty and also has no starting anchor. Create an anchor by sending 'w'."
                    )
                    return
                else:
                    start = last_act.end
            else:
                start = starting_anchor
                starting_anchor = None

        act = Activity(name=m0_text, start=start, end=received_at)
        act.save()
        set_msg_act(act)
        out_add(str(act))
        await edit(out)
        await process_reminders(act.name)
        return out
    except:
        err = "\nJulia encountered an exception. :(\n" + traceback.format_exc()
        logger.error(err)
        out_add(err)
        if reload_on_failure:
            out_add("Reloading ...\n")
            await edit(out, truncate=False)
            await reload_tt()
            return await borg._plugins["timetracker"]._process_msg(
                m0,
                reload_on_failure=False,
                text_input=text_input,
                out=out,
                received_at=received_at,
            )
        else:
            await edit(out)
            return out
