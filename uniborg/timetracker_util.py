from __future__ import annotations
from brish import z
from uniborg.util import (
    embed2,
    send_files,
    force_async,
)
import logging
import re
from collections import OrderedDict
from pynight.common_iterable import to_iterable


try:
    logger = logger or logging.getLogger(__name__)
except:
    logger = logging.getLogger(__name__)
##
import asyncio

lock_tt = asyncio.Lock()
msg2act = dict()
##
timetracker_chat = -1001179162919

DAY_START = 5
activity_child_separator = (
    "_"  # I am not sure if I got the refactoring to use this perfectly
)
is_local = bool(z("isLocal"))
# is_linux = bool(z("isLinux"))

skip_acts_default = [
    "sleep",
]


##
async def send_file(file, **kwargs):
    if file:
        # await borg.send_file(timetracker_chat, file, allow_cache=False, **kwargs)
        await send_files(timetracker_chat, file, **kwargs)


##
from peewee import *
import os
from datetime import timedelta
from pathlib import Path


def timedelta_dur(end, start):
    return end - start


# Path.home().joinpath(Path("cellar"))
timetracker_db_path = Path(
    z('print -r -- "${{timetracker_dir:-$HOME/tmp}}/timetracker.db"').outrs
)
user_choices_path = Path(
    z('print -r -- "${{timetracker_dir:-$HOME/tmp}}/timetracker_user_choices"').outrs
)
os.makedirs(os.path.dirname(timetracker_db_path), exist_ok=True)
db = SqliteDatabase(timetracker_db_path)


class BaseModel(Model):
    class Meta:
        database = db


class Activity(BaseModel):
    name = CharField()
    start = DateTimeField()
    end = DateTimeField()

    def __str__(self):
        dur = timedelta_dur(self.end, self.start)
        return f"""{self.name} {timedelta_str(dur)}"""


## indexes: add manually via Datagrip (right-click on table, modify table)(adding it via peewee is not necesseray https://github.com/coleifer/peewee/issues/2360 )
# create index activity_end_index
#     on activity (end desc);
# create index activity_start_end_index
#     on activity (start desc, end desc);
##

db.close()
db.connect()  # @todo? db.close()
db.create_tables([Activity])
##

import textwrap
import dataclasses
from dataclasses import dataclass
from functools import total_ordering
from typing import Dict, List
import datetime


def timedelta_total_seconds(td: timedelta):
    return td.total_seconds()


def timedelta_str(
    td: timedelta,
    **kwargs,
):
    s = timedelta_total_seconds(td)
    return seconds_str(
        s,
        **kwargs,
    )


def gen_s(num):
    if num != 1:
        return "s"
    return ""


def seconds_str(
    s,
    only_hours=True,
    # only_hours=False,
    scale=True,
):
    res = ""
    sleep = 9.5
    if scale:
        active_h = 24 - sleep  #: 14.5 active hours in a day
    else:
        active_h = 24

    if only_hours:
        # m, _ = divmod(s, 60)
        m = int(round(s / 60.0, 0))
        h, m = divmod(m, 60)
        res = f"{h}:{m}"
    else:
        days, s = divmod(s, active_h * 3600)
        m = int(round(s / 60.0, 0))
        hours, m = divmod(m, 60)
        months, days = divmod(days, 30)
        years, months = divmod(months, 24)
        if years:
            res += f"{years} year{gen_s(years)}, "
        if months:
            res += f"{months} month{gen_s(months)}, "
        if days:
            res += f"{days} day{gen_s(days)}, "
        res += f"{hours or 0:}:"
        res += f"{m}"

    return res


def format_hours(decimal_hours):
    """Convert decimal hours into hours and minutes."""

    hours = int(decimal_hours)
    minutes = int((decimal_hours - hours) * 60)
    # return f"{hours:02d}:{minutes:02d}"
    return f"{hours:2d}:{minutes:2d}"
    #: `:2d` space-pads to two digits


##
@total_ordering
@dataclass()
class ActivityDuration:
    # @legacyComment Somehow putting ActivityDuration in the plugin file itself resulted in error (the culprit was probably dataclass), so I am putting them here.
    name: str
    duration: timedelta = dataclasses.field(default_factory=timedelta)
    sub_acts: Dict[str, ActivityDuration] = dataclasses.field(default_factory=dict)

    total_duration: timedelta = dataclasses.field(default_factory=timedelta)
    # @property
    # def total_duration(self):
    #     res = self.duration
    #     for act in self.sub_acts:
    #         res += act.total_duration
    #     return res

    def __lt__(self, other):
        if type(other) is ActivityDuration:
            return timedelta_total_seconds(
                self.total_duration
            ) < timedelta_total_seconds(other.total_duration)
        elif type(other) is timedelta:
            return timedelta_total_seconds(
                self.total_duration
            ) < timedelta_total_seconds(other)
        else:
            return NotImplemented

    def add(self, dur: timedelta, act_chain: List[str]):
        self.total_duration += dur
        if len(act_chain) == 0:
            self.duration += dur
        else:
            # act_chain's last item should be the parent for possible perf reasons
            child = act_chain.pop()
            child_act = self.sub_acts.setdefault(child, ActivityDuration(name=child))
            child_act.add(dur, act_chain)

    def __str__(self, width=25, indent="  "):
        def adjust_name(name, width=width):
            return name + " " * max(4, width - len(name))

        res = ""
        name = self.name
        skip_me = name == "Total"  # Skip root
        my_indent = indent
        next_width = width - len(indent)
        if not skip_me:
            res += f"""{adjust_name(name)} {timedelta_str(self.total_duration)}\n"""
            if len(self.sub_acts) > 0 and timedelta_total_seconds(self.duration) > 60:
                res += f"""{my_indent}{adjust_name(".", width=next_width)} {timedelta_str(self.duration)}\n"""
        else:
            my_indent = ""
        for act in sorted(self.sub_acts.values(), reverse=True):
            res += textwrap.indent(
                act.__str__(width=(next_width), indent=indent), my_indent
            )
        return res


##
def should_skip_act_p(
    act,
    exclude=None,
    include=None,
):
    exclude = to_iterable(exclude)
    include = to_iterable(include)

    name = act.name

    if include:
        included = False
        for i in include:
            if isinstance(i, re.Pattern):
                if i.search(name):
                    included = True
                    break
            else:
                if name == i:
                    included = True
                    break

        if not included:
            return True

    for skippable in exclude:
        if isinstance(skippable, re.Pattern):
            if skippable.search(name):
                return True
        else:
            if name == skippable:
                return True

    return False


##
def activity_list_to_str_now(
    delta=datetime.timedelta(hours=24),
    received_at=None,
    **kwargs,
):
    now = received_at or datetime.datetime.today()
    low = now - delta
    return activity_list_to_str(
        low,
        now,
        **kwargs,
    )


def activity_list_to_str(
    low,
    high,
    skip_acts=None,
    include_acts=None,
):
    if skip_acts is None:
        skip_acts = skip_acts_default

    acts = Activity.select().where(
        (Activity.start.between(low, high)) | (Activity.end.between(low, high))
    )
    acts_agg = ActivityDuration("Total")
    acts_skipped = ActivityDuration("Skipped")
    for act in acts:
        act_name = act.name
        act_start = max(act.start, low)
        act_end = min(act.end, high)
        dur = timedelta_dur(act_end, act_start)
        path = list(reversed(act_name.split(activity_child_separator)))
        if should_skip_act_p(
            act,
            exclude=skip_acts,
            include=include_acts,
        ):
            acts_skipped.add(dur, path)
        else:
            acts_agg.add(dur, path)
    # ("TOTAL", total_dur),
    # we need a monospace font to justify the columns
    res = f"```\nSpanning {str(high - low)}; UNACCOUNTED {timedelta_str(timedelta_dur(high, low + acts_agg.total_duration + acts_skipped.total_duration), scale=False)}\nTotal (scaled): {timedelta_str(acts_agg.total_duration)}; Skipped {timedelta_str(acts_skipped.total_duration, scale=False)}\n"
    res += str(acts_agg)[0:3500]  # truncate it for Telegram
    return {"string": res + "\n```", "acts_agg": acts_agg, "acts_skipped": acts_skipped}


def activity_list_habit_get_now(
    names,
    low=None,
    high=None,
    delta=datetime.timedelta(days=30),
    mode=0,
    fill_default=True,
    received_at=None,
    day_start=DAY_START,
    adjust_high=False,
    **kwargs,
):
    if type(names) is str:
        names = [names]

    ##
    high = high or (received_at or datetime.datetime.today())
    low = low or (high - delta)
    low = low.replace(hour=day_start, minute=0, second=0, microsecond=0)
    if adjust_high:
        high = high.replace(
            hour=(day_start - 1), minute=59, second=59, microsecond=0
        )  # high will go till previous day only, and skips the last day in range

    if high <= low:
        logger.error("high <= low")
        return

    night_passover = datetime.timedelta(hours=(day_start), seconds=0)

    def which_bucket(act: Activity):
        accept = False
        for name in names:
            if name == "STUDY_SA_NX":
                include = [
                    re.compile(r"^sa($|_)|(^|_)study($|_)"),
                ]
                exclude = [
                    re.compile(f"(^|_)exploration($|_)"),
                ]

                accept = all(re.search(p, act.name) for p in include)
                accept = accept and not any(re.search(p, act.name) for p in exclude)
                break
            elif isinstance(name, re.Pattern):
                if re.search(name, act.name):
                    accept = True
                    break
            elif isinstance(name, str):
                check_end = name.endswith("$")
                if check_end:
                    name = name[:-1]
                    if act.name.endswith(activity_child_separator + name):
                        accept = True
                        break

                if act.name == name or act.name.startswith(
                    name + activity_child_separator
                ):
                    accept = True
                    break
            else:
                raise ValueError(f"Unsupported type for name: {type(name)}")

        if accept:
            return (act.start - night_passover).date()
        else:
            return None

    buckets = activity_list_buckets_get(
        low, high, which_bucket=which_bucket, mode=mode, **kwargs
    )
    if mode == 0:
        buckets_dur = OrderedDict(
            (k, round(timedelta_total_seconds(v.total_duration) / 3600, 2))
            for k, v in buckets.items()
        )
    elif mode in (1, 2):
        buckets_dur = buckets

    if fill_default:
        interval = datetime.timedelta(days=1)
        while low <= high:
            d = (low - night_passover).date()
            if mode in (0, 1):
                buckets_dur.setdefault(d, 0)
            elif mode == 2:
                buckets_dur.setdefault(d, [])

            low += interval

    return buckets_dur


def stacked_area_get_act_roots(
    low=None,
    high=None,
    delta=None,
    repeat=30,
    interval=datetime.timedelta(days=1),
    received_at=None,
):
    delta = delta or interval * repeat

    # high = high or (datetime.datetime.today() - datetime.timedelta(days=1))
    high = high or (received_at or datetime.datetime.today())
    low = low or (high - delta)
    low = low.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
    high = high.replace(
        hour=(DAY_START - 1), minute=59, second=59, microsecond=0
    )  # high will go till previous day only, and skips the last day in range
    if high <= low:
        logger.error("stacked_area_get_act_roots: high <= low")
        return

    night_passover = datetime.timedelta(hours=(DAY_START), seconds=0)

    buckets = dict()
    while low < high:
        mid = min(high, low + interval)
        bucket = buckets.setdefault(
            (low - night_passover).date(), ActivityDuration("Total")
        )
        acts = Activity.select().where(
            (Activity.start.between(low, mid)) | (Activity.end.between(low, mid))
        )
        for act in acts:
            dur = timedelta_dur(min(act.end, mid), max(act.start, low))
            bucket.add(dur, list(reversed(act.name.split(activity_child_separator))))

        low = mid

    return buckets


def activity_list_buckets_get(low, high, which_bucket, mode=0, correct_overlap=True):
    acts = None
    # adding the name query here will increase performance. (Currently done in which_bucket.)
    if correct_overlap:
        acts = Activity.select().where(
            (Activity.start.between(low, high)) | (Activity.end.between(low, high))
        )
    else:
        acts = Activity.select().where((Activity.start.between(low, high)))

    buckets = OrderedDict()
    for act in acts:
        if correct_overlap:
            # @warn overlap is not corrected between the buckets themselves!
            act.start = max(act.start, low)
            act.end = min(act.end, high)
            ##
            # [Q] Is it possible to mark a model object as "unsave-able"? https://github.com/coleifer/peewee/issues/2375
            # It's not officially supported. So:
            act.save = None

        bucket_key = which_bucket(act)
        if not bucket_key:
            continue
        if mode == 0:
            bucket = buckets.setdefault(bucket_key, ActivityDuration("Total"))
            dur = timedelta_dur(act.end, act.start)
            bucket.add(dur, list(reversed(act.name.split(activity_child_separator))))
        elif mode == 1:  # count mode
            bucket = buckets.setdefault(bucket_key, 0)
            buckets[bucket_key] += 1
        elif mode == 2:  # raw act mode
            bucket = buckets.setdefault(bucket_key, [])
            bucket.append(act)

    return buckets


### visualizations
if not is_local:
    import plotly.io as pio

    # install `xvfb` via apt
    pio.orca.config.use_xvfb = True
##
import plotly.graph_objects as go

## https://plotly.com/python/discrete-color/
# from plotly.express import colors as px_colors # this takes a few seconds to load even on MBP!
cmaps = dict()
cmaps["plotly"] = [
    "#636EFA",
    "#EF553B",
    "#00CC96",
    "#AB63FA",
    "#FFA15A",
    "#19D3F3",
    "#FF6692",
    "#B6E880",
    "#FF97FF",
    "#FECB52",
]
cmaps["d3"] = [
    "#1F77B4",
    "#FF7F0E",
    "#2CA02C",
    "#D62728",
    "#9467BD",
    "#8C564B",
    "#E377C2",
    "#7F7F7F",
    "#BCBD22",
    "#17BECF",  # fake extension:
    "#1CFFCE",
    "#DEA0FD",
]
cmaps["g10"] = [
    "#3366CC",
    "#DC3912",
    "#FF9900",
    "#109618",
    "#990099",
    "#0099C6",
    "#DD4477",
    "#66AA00",
    "#B82E2E",
    "#316395",
]
cmaps["alphabet"] = [
    "#AA0DFE",
    "#3283FE",
    "#85660D",
    "#782AB6",
    "#565656",
    "#1C8356",
    "#16FF32",
    "#F7E1A0",
    "#E2E2E2",
    "#1CBE4F",
    "#C4451C",
    "#DEA0FD",
    "#FE00FA",
    "#325A9B",
    "#FEAF16",
    "#F8A19F",
    "#90AD1C",
    "#F6222E",
    "#1CFFCE",
    "#2ED9FF",
    "#B10DA1",
    "#C075A6",
    "#FC1CBF",
    "#B00068",
    "#FBE426",
    "#FA0087",
]
cmaps["light24"] = [
    "#FD3216",
    "#00FE35",
    "#6A76FC",
    "#FED4C4",
    "#FE00CE",
    "#0DF9FF",
    "#F6F926",
    "#FF9616",
    "#479B55",
    "#EEA6FB",
    "#DC587D",
    "#D626FF",
    "#6E899C",
    "#00B5F7",
    "#B68E00",
    "#C9FBE5",
    "#FF0092",
    "#22FFA7",
    "#E3EE9E",
    "#86CE00",
    "#BC7196",
    "#7E7DCD",
    "#FC6955",
    "#E48F72",
]
cmaps["pastel"] = [
    "rgb(102, 197, 204)",
    "rgb(246, 207, 113)",
    "rgb(248, 156, 116)",
    "rgb(220, 176, 242)",
    "rgb(135, 197, 95)",
    "rgb(158, 185, 243)",
    "rgb(254, 136, 177)",
    "rgb(201, 219, 116)",
    "rgb(139, 224, 164)",
    "rgb(180, 151, 231)",
    "rgb(179, 179, 179)",
]
##
# https://colorbrewer2.org/#type=qualitative&scheme=Pastel1&n=9
# https://plotly.com/python/discrete-color/
# https://medialab.github.io/iwanthue/
# https://google.github.io/palette.js/ (most colors are from here)
# https://carto.com/carto-colors/ (good preview here, to see how well the colors actually look on a map)
##
# As of Python 3.7, the standard dict preserves insertion order
categories = {
    # @hiddenDep visualize_stacked_area/get_y needs to be able to calculate the correct duration for each category.
    "Total": "rgb(255, 255, 255)",
    "career": "rgb(17, 99, 0)",
    "study_ta": "rgb(89, 0, 255)",
    "study": "rgb(102, 166, 30)",
    "study_exploration": "rgb(50, 255, 207)",
    "chores_self_study": "rgb(102, 166, 30)",
    "chores_self_health": "rgb(179, 233, 0)",
    "meditation": "rgb(93, 255, 0)",
    "chores_self_hygiene": "rgb(178, 190, 77)",
    "outdoors": "rgb(175, 141, 0)",
    "sa": "rgb(55, 126, 184)",
    "exploration": "rgb(0, 210, 213)",
    "creative": "rgb(240, 107, 255)",
    "social": "rgb(188, 128, 189)",
    "social_online": "rgb(252, 205, 229)",
    "social_exploration": "rgb(97, 82, 250)",
    "social_chores": "rgb(179, 172, 250)",
    "chores": "rgb(255, 243, 185)",
    "chores_self_rest": "rgb(255, 237, 111)",
    "chores_others": "rgb(221, 255, 173)",
    "chores_self_commute": "rgb(170, 28, 59)",
    "wasted": "rgb(255, 0, 41)",
    # 'outdoors' : 'rgb(0, 255, 185)',
    "exploration_gathering_fiction": "rgb(255, 100, 100)",
    "entertainment": "rgb(251, 128, 114)",
    "nonfiction": "rgb(255, 127, 0)",
    "nonfiction_technical": "rgb(132, 166, 30)",
    # 'consciously untracked' : 'rgb(247, 249, 226)',
    "consciously untracked": "rgb(0,0,0)",
    # 'sleep' : 'rgb(255,255,255)',
    # 'sleep' : 'rgb(221, 243, 250)',
    "sleep": "rgb(142, 200, 239)",
    # '' : 'rgb()',
}


##
def get_acts(
    root: ActivityDuration,
    skip_acts=None,
    include_acts=None,
    dict_mode=False,
):
    if skip_acts is None:
        skip_acts = skip_acts_default

    # mutates its input! is not idempotent!
    if dict_mode == False:
        acts = [root]
    elif dict_mode == True:
        dict_mode = dict()
        dict_mode[root.name] = root

    if not hasattr(root, "parent"):
        root.parent = None

    if not hasattr(root, "shortname"):
        root.shortname = root.name

    act: ActivityDuration
    for act in root.sub_acts.values():
        act.parent = root

        act.shortname = act.name
        if act.parent.name != "Total":
            act.name = f"{act.parent.name}_{act.name}"

        if should_skip_act_p(
            act,
            exclude=skip_acts,
            include=include_acts,
        ):
            act.parent.total_duration -= act.total_duration
            continue

        # print(f"{act.shortname} -> {act.name} via parent {act.parent.name}")
        if dict_mode:
            dict_mode[act.name] = act
            get_acts(
                act,
                skip_acts=skip_acts,
                include_acts=include_acts,
                dict_mode=dict_mode,
            )
        else:
            acts += get_acts(
                act,
                skip_acts=skip_acts,
                include_acts=include_acts,
                dict_mode=dict_mode,
            )

    if dict_mode:
        return dict_mode
    else:
        return acts


def local_helper_visualize_plotly():
    # @local To play with stuff on local PC
    db.close()
    db.connect()
    res = activity_list_to_str_now(delta=datetime.timedelta(days=7))  # reset acts_agg
    print(res["string"])
    return visualize_plotly(res["acts_agg"])


@force_async
def visualize_plotly(
    acts,
    title=None,
    treemap=True,
    sunburst=True,
    icicle=True,
    skip_acts=None,  #: @ignored
    include_acts=None,  #: @ignored
):
    # @warn this is not async, and it takes rather long to complete
    ##
    out_links = []
    out_files = []
    if timedelta_total_seconds(acts.total_duration) == 0:
        return out_links, out_files

    all_acts = get_acts(
        acts,
        skip_acts=[],
        include_acts=[],
        #: =get_acts= will NOT pick up =chores_study= by =_study$= as =chores= will NOT match and its children will not be considered at all.
        # skip_acts=skip_acts,
        # include_acts=include_acts,
    )
    acts_agg = all_acts[0]
    # print(acts_agg)

    ids = [act.name for act in all_acts]
    labels = [
        f"{act.shortname} {(timedelta_total_seconds(act.total_duration)*100/timedelta_total_seconds(acts_agg.total_duration)):.1f}%"
        for act in all_acts
    ]
    texts = [timedelta_str(act.total_duration) for act in all_acts]
    ##
    parents = [(act.parent and act.parent.name) or "" for act in all_acts]
    values = [
        (timedelta_total_seconds(act.total_duration) / (3600)) for act in all_acts
    ]
    # Do NOT round the values. Plotly expects them to sum correctly or something.

    ## Test out input values:
    # lim=19
    # labels = labels[:lim]
    # parents = parents[:lim]
    # values = values[:lim]

    # print(labels)
    # print(texts)
    # print(parents)
    # print(values)
    ##

    # https://plotly.com/python/treemaps/
    # https://plotly.com/python/reference/treemap/
    plot_opts = dict(
        branchvalues="total",
        ids=ids,
        labels=labels,
        parents=parents,
        values=values,
        text=texts,
        # %{value:.1f}
        texttemplate="%{label}<br>%{text}<br>%{percentParent:.1%} of %{parent}<br>%{percentEntry:.1%} of %{entry}<br>%{percentRoot:.1%} of %{root}",
        # textinfo = "label+value+percent parent+percent entry+percent root",
        # %{currentPath}%{label}
        hovertemplate="%{label}<br>%{text}<br>%{percentParent:.1%} of %{parent}<br>%{percentEntry:.1%} of %{entry}<br>%{percentRoot:.1%} of %{root}<extra>%{id}</extra>",
        # https://community.plotly.com/t/how-to-explicitly-set-colors-for-some-sectors-in-a-treemap/51162
        # color="day",
        # color_discrete_map={'(?)':'gold', 'Study':'green', 'wasted':'black'},
    )

    ## colors @unresolved/noLongerNeeded https://community.plotly.com/t/how-to-explicitly-set-colors-for-some-sectors-in-a-treemap/51162
    def choose_color(i, act, parent_names=None, cmap=cmaps["alphabet"]):
        if parent_names == None:
            parent_names = act.name.split(activity_child_separator)

        longname = activity_child_separator.join(parent_names)
        if longname in categories:
            return categories[longname]

        parent_names = parent_names[:-1]
        if len(parent_names) >= 1:
            return choose_color(i, act, parent_names)
        else:
            return cmap[
                abs((hash(longname) % len(cmap)))
            ]  # abs is redundant: Python modulo operator always return the remainder having the same sign as the divisor.

    cs = [choose_color(i, act) for i, act in enumerate(all_acts)]

    if treemap:
        fig = go.Figure(go.Treemap(**plot_opts))
        # fig.update_layout(margin = dict(t=0, l=0, r=0, b=0))
        fig.update_layout(margin=dict(t=30, l=0, r=30, b=30))
        # fig.update_layout(uniformtext=dict(minsize=6, mode='hide'))
        fig.update_traces(marker_colors=cs)
        fig.update_traces(marker_depthfade=True)
        if title:
            fig.update_layout(title_text=title)
            fig.update_layout(title_font_size=11)
            # fig.update_layout(title_x=0.1)

        is_local and fig.show()
        l, f = fig_export(
            fig, "treemap", width=400, height=400, svg_export=False, pdf_export=False
        )
        out_links += l  # is list
        out_files += f

    ##
    if sunburst:
        # @unresolved https://community.plotly.com/t/show-the-current-path-bar-in-sunburst-plots-just-like-treemap-plots/51155

        plot_opts["labels"] = [act.shortname for act in all_acts]
        plot_opts["texttemplate"] = "%{label}<br>%{text}, %{percentRoot:.1%}"

        fig = go.Figure(go.Sunburst(**plot_opts))

        fig.update_layout(margin=dict(t=0, l=0, r=0, b=0))
        # fig.update_layout(uniformtext=dict(minsize=6, mode='hide'))

        fig.update_traces(marker_colors=cs)

        if title:
            fig.update_layout(title_text=title)
            fig.update_layout(title_font_size=11)
            fig.update_layout(title_y=0.99)

        is_local and fig.show()
        l, f = fig_export(
            fig, "sunburst", width=400, height=400, svg_export=False, pdf_export=False
        )
        out_links += l  # is list
        out_files += f

    if icicle:
        # https://plotly.com/python/icicle-charts/
        ##
        # @warn previous changes to plot_opts are inherited by us (copy plot_opts if you don't like this; currently, it's not a bad thing.)
        ##
        fig = go.Figure(go.Icicle(**plot_opts))
        fig.update_layout(margin=dict(t=30, l=0, r=30, b=30))
        fig.update_traces(marker_colors=cs)
        # fig.update_traces(marker_depthfade=True)
        if title:
            fig.update_layout(title_text=title)
            fig.update_layout(title_font_size=11)
            # fig.update_layout(title_x=0.1)

        is_local and fig.show()
        l, f = fig_export(
            fig,
            "icicle",
            width=400,
            height=400,
            svg_export=False,
            pdf_export=False,
            html_export=True,
        )
        out_links += l  # is list
        out_files += f

    return out_links, out_files


def get_sub_act_total_duration(act, sub_act_name: str, days):
    sub_act = act.sub_acts.get(sub_act_name, None)
    if sub_act:
        return round(
            (timedelta_total_seconds(sub_act.total_duration) / 3600.0) / days, 1
        )
    else:
        return 0


@force_async
def visualize_stacked_area(dated_act_roots, days=1, cmap=None):
    # @warn if an act such as 'untracked' starts before a day and ends after that day, then the day will look empty to our aggregation algorithm. I can't think of any easy solutions for this.
    ##
    out_links = []
    out_files = []

    fig = go.Figure()

    act_roots = dated_act_roots.values()
    xs = list(dated_act_roots.keys())

    # good: alphabet, d3, pastel
    # mediocre: light24
    if not cmap or cmap == "s":
        cmap = None
    else:
        cmap = cmaps[cmap or "alphabet" or "d3"]
        cmap_len = len(cmap)

    ##
    act_roots_all = [get_acts(act, dict_mode=True, skip_acts=[]) for act in act_roots]

    def get_dur(act_root_all, category):
        if category in act_root_all:
            return (
                timedelta_total_seconds(act_root_all[category].total_duration) / 3600.0
            ) / days
        else:
            return 0

    def get_y(act_root_all, category):
        sub_categories = [
            sub_category
            for sub_category in categories.keys()
            if sub_category.startswith(f"{category}_")
        ]
        if len(sub_categories) == 0:
            return get_dur(act_root_all, category)
        else:
            return get_dur(act_root_all, category) - sum(
                get_y(act_root_all, sub_category) for sub_category in sub_categories
            )

    def get_ys(category):
        return [get_y(act_root_all, category) for act_root_all in act_roots_all]

    i = 0
    for category, color in categories.items():
        if category in ("Total",):
            continue

        if cmap:
            if type(cmap) is dict:
                color = cmap[category]
            else:
                color = cmap[(i % cmap_len)]

        fig.add_trace(
            go.Scatter(
                # https://plotly.com/python/reference/scatter/
                ##
                # legendrank controls the ordering
                ##
                legendgroup=category.split(activity_child_separator)[0],
                ##
                name=category,
                x=xs,
                # y=[get_sub_act_total_duration(act, category, days) for act in act_roots],
                y=get_ys(category),
                hoverinfo="x+y",
                mode="lines",
                line=dict(
                    width=0.5,
                    color=color,
                ),
                stackgroup="one",
                # groupnorm='percent' # sets the normalization for the sum of the stackgroup
            )
        )
        i += 1

    # https://github.com/plotly/plotly.py/issues/2922#issuecomment-922461952
    # I am trying to fit a large legend on a small image; Setting the font size to a small value doesn't help, as the freed up space just goes to become vertical margin.
    fig.update_layout(
        yaxis_range=(
            0,
            24,
        ),  # since we are only including a subset of all the sub acts of the root, we should always be <= 24 hours
        # yaxis={'tickfont_size':8}, # sets the font size of the y axis, the default is good
        ## the legend (the color guide): https://plotly.com/python/reference/layout/#layout-showlegend
        legend=dict(
            # orientation = "h",
            ##
            # y          = 1.03,
            # yanchor    = "middle",
            ##
            # traceorder = "grouped",
            # traceorder = "grouped+reversed",
            # traceorder = "reversed",
            traceorder="normal",
            ##
            font=dict(
                # family = "sans-serif",
                size=11,
                # color  = "black"
            ),
            # itemwidth=30, # An int or float in the interval [30, inf]
            tracegroupgap=0,  # the amount of vertical space (in px) between legend groups.
            # title = dict(
            #     font = dict(
            #         size = 5,
            #         color = "green"
            #     )
        ),
    )
    fig.update_layout(margin=dict(t=0, l=0, r=0, b=0))
    l, f = fig_export(
        fig, "stacked_area", width=700, height=400, svg_export=False, pdf_export=False
    )
    out_links += l  # is list
    out_files += f

    return out_links, out_files


@force_async
def generate_colors_legend():
    """Generate a legend image showing all activity categories with their colors."""
    import plotly.graph_objects as go

    # Get categories, excluding "Total"
    cats = [(name, color) for name, color in categories.items() if name != "Total"]

    # Prepare data for display
    names = [name for name, _ in cats]
    colors_list = [color for _, color in cats]

    # Create a table-like visualization
    fig = go.Figure(
        data=[
            go.Table(
                columnwidth=[300, 100],
                header=dict(
                    values=["<b>Activity</b>", "<b>Color</b>"],
                    fill_color="lightgray",
                    align="left",
                    font=dict(size=14, color="black"),
                ),
                cells=dict(
                    values=[names, ["█████" for _ in colors_list]],
                    fill_color=["white", colors_list],
                    align="left",
                    font=dict(size=12, color=["black", colors_list]),
                    height=25,
                ),
            )
        ]
    )

    fig.update_layout(
        # title=dict(
        #     text="<b>Activity Categories Legend</b>",
        #     x=0.5,
        #     xanchor="center",
        #     font=dict(size=18),
        # ),
        # margin=dict(t=60, l=20, r=20, b=20),
        margin=dict(t=20, l=20, r=20, b=20),
        height=max(500, 70 + len(cats) * 25),
    )

    # Export the figure
    img_path = z("gmktemp --suffix .png").outrs
    fig.write_image(
        img_path,
        width=300,
        height=fig.layout.height,
        scale=2,
    )

    return img_path


def fig_export(
    fig,
    exported_name,
    html_export=True,
    png_export=True,
    svg_export=True,
    pdf_export=True,
    width=600,
    height=400,
    scale=4,
):
    out_links = []
    out_files = []
    if html_export:
        exported_html = f"./plots/{exported_name}.html"
        z("ensure-dir {exported_html}")
        fig.write_html(exported_html, include_plotlyjs="cdn", include_mathjax="cdn")
        # fig.write_html("./plots/exported_full.html")
        z("isDarwin && open {exported_html}")
        is_local or out_links.append(z("jdl-private {exported_html}").outrs)
    if png_export:
        exported_png = f"./plots/{exported_name}.png"
        z("ensure-dir {exported_png}")
        fig.write_image(exported_png, width=width, height=height, scale=scale)
        out_files.append(exported_png)
    if svg_export:
        # svg needs small sizes
        exported_svg = f"./plots/{exported_name}.svg"
        z("ensure-dir {exported_svg}")
        fig.write_image(exported_svg, width=width, height=height, scale=1)
        out_files.append(exported_svg)
    if pdf_export:
        exported_pdf = f"./plots/{exported_name}.pdf"
        z("ensure-dir {exported_pdf}")
        fig.write_image(exported_pdf, width=width, height=height, scale=1)
        out_files.append(exported_pdf)
    return out_links, out_files


###
