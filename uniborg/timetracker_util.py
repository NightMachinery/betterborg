from __future__ import annotations
from brish import z
##
import asyncio
lock_tt = asyncio.Lock()
##
DAY_START = 5
is_local = bool(z("isLocal"))
# is_linux = bool(z("isLinux"))
##
from peewee import *
import os
from dateutil.relativedelta import relativedelta
from pathlib import Path

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
from dateutil.relativedelta import relativedelta


def relativedelta_total_seconds(rd: relativedelta):
    # Used Google to convert the years and months, they are slightly more than 365 and 30 days respectively.
    return rd.years * 31540000 + rd.months * 2628000 + rd.days * 86400 + rd.hours * 3600 + rd.minutes * 60 + rd.seconds


def gen_s(num):
    if num != 1:
        return "s"
    return ""


def relativedelta_str(rd: relativedelta):
    res = ""
    rd = rd.normalized()
    # rd.weeks seems to just convert rd.days into weeks
    if rd.years:
        res += f"{rd.years} year{gen_s(rd.years)}, "
    if rd.months:
        res += f"{rd.months} month{gen_s(rd.months)}, "
    if rd.days:
        res += f"{rd.days} day{gen_s(rd.days)}, "
    res += f"{rd.hours or 0}:"
    res += f"{rd.minutes}"
    return res

@total_ordering
@dataclass()
class ActivityDuration:
    # @legacyComment Somehow putting ActivityDuration in the plugin file itself resulted in error (the culprit was probably dataclass), so I am putting them here.
    name: str
    duration: relativedelta = dataclasses.field(default_factory=relativedelta)
    sub_acts: Dict[str, ActivityDuration] = dataclasses.field(
        default_factory=dict)

    total_duration: relativedelta = dataclasses.field(
        default_factory=relativedelta)
    # @property
    # def total_duration(self):
    #     res = self.duration
    #     for act in self.sub_acts:
    #         res += act.total_duration
    #     return res

    def __lt__(self, other):
        if type(other) is ActivityDuration:
            return relativedelta_total_seconds(self.total_duration) < relativedelta_total_seconds(other.total_duration)
        elif type(other) is relativedelta:
            return relativedelta_total_seconds(self.total_duration) < relativedelta_total_seconds(other)
        else:
            return NotImplemented

    def add(self, dur: relativedelta, act_chain: List[str]):
        self.total_duration += dur
        if len(act_chain) == 0:
            self.duration += dur
        else:
            # act_chain's last item should be the parent for possible perf reasons
            child = act_chain.pop()
            child_act = self.sub_acts.setdefault(
                child, ActivityDuration(name=child))
            child_act.add(dur, act_chain)

    def __str__(self, width=25, indent="  "):
        def adjust_name(name, width=width):
            return name + " " * max(4, width - len(name))

        res = ""
        name = self.name
        skip_me = (name == "Total")  # Skip root
        my_indent = indent
        next_width = width - len(indent)
        if not skip_me:
            res += f"""{adjust_name(name)} {relativedelta_str(self.total_duration)}\n"""
            if len(self.sub_acts) > 0 and relativedelta_total_seconds(self.duration) > 60:
                res += f"""{my_indent}{adjust_name(".", width=next_width)} {relativedelta_str(self.duration)}\n"""
        else:
            my_indent = ""
        for act in sorted(self.sub_acts.values(), reverse=True):
            res += textwrap.indent(act.__str__(width=(next_width), indent=indent), my_indent)
        return res

##
def activity_list_to_str_now(delta=datetime.timedelta(hours=24), **kwargs):
    now = datetime.datetime.today()
    low = now - delta
    return activity_list_to_str(low,now, **kwargs)

def activity_list_to_str(low, high, skip_acts=["sleep"]):
    acts = Activity.select().where((Activity.start.between(low, high)) | (Activity.end.between(low, high)))
    acts_agg = ActivityDuration("Total")
    acts_skipped = ActivityDuration("Skipped")
    for act in acts:
        act_name = act.name
        act_start = max(act.start, low)
        act_end = min(act.end, high)
        dur = relativedelta(act_end, act_start)
        path = list(reversed(act_name.split('_')))
        if act_name in skip_acts:
            acts_skipped.add(dur, path)
        else:
            acts_agg.add(dur, path)
    # ("TOTAL", total_dur),
    # we need a monospace font to justify the columns
    res = f"```\nSpanning {str(high - low)}; UNACCOUNTED {relativedelta_str(relativedelta(high, low + acts_agg.total_duration + acts_skipped.total_duration))}; Skipped {relativedelta_str(acts_skipped.total_duration)}\n"
    res += str(acts_agg)
    return {'string': res + "\n```", 'acts_agg': acts_agg, 'acts_skipped': acts_skipped}

def activity_list_habit_get_now(name: str, delta=datetime.timedelta(days=30), mode=0, fill_default=True):
    # _now means 'now' is 'high'
    high = datetime.datetime.today()
    low = high - delta
    low = low.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
    # aligns dates with real life, so that date changes happen at, e.g., 5 AM
    night_passover = datetime.timedelta(hours=DAY_START)

    def which_bucket(act: Activity):
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


def activity_list_buckets_get(low, high, which_bucket, mode=0, correct_overlap=True):
    acts = None
    # adding the name query here will increase performance. (Currently done in which_bucket.)
    if correct_overlap:
        acts = Activity.select().where((Activity.start.between(low, high)) | (Activity.end.between(low, high)))
    else:
        acts = Activity.select().where((Activity.start.between(low, high)))

    buckets = {}
    for act in acts:
        if correct_overlap:
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
            dur = relativedelta(act.end, act.start)
            bucket.add(dur, list(reversed(act.name.split('_'))))
        elif mode == 1:  # count mode
            bucket = buckets.setdefault(bucket_key, 0)
            buckets[bucket_key] += 1
    return buckets

### visualizations
if not is_local:
    import plotly.io as pio
    # install `xvfb` via apt
    pio.orca.config.use_xvfb = True
##
def get_acts(root: ActivityDuration, skip_acts=["sleep"]):
    # mutates its input! is not idempotent!
    acts = [root]
    if not hasattr(root, 'parent'):
        root.parent = None
    if not hasattr(root, 'shortname'):
        root.shortname = root.name

    act: ActivityDuration
    for act in root.sub_acts.values():
        act.parent = root
        if act.name in skip_acts:
            act.parent.total_duration -= act.total_duration
            continue
        act.shortname = act.name
        if act.parent.name != 'Total':
            act.name = f"{act.parent.name}_{act.name}"
        # print(f"{act.shortname} -> {act.name} via parent {act.parent.name}")
        acts += get_acts(act)
    return acts

def local_helper_visualize_plotly():
    # @local To play with stuff on local PC
    db.close()
    db.connect()
    res = activity_list_to_str_now(delta=datetime.timedelta(days=7)) # reset acts_agg
    print(res['string'])
    return visualize_plotly(res['acts_agg'])

def visualize_plotly(acts):
    all_acts = get_acts(acts)
    # print(acts_agg)
    import plotly.graph_objects as go

    ids = None
    labels = [act.name for act in all_acts]
    texts = [act.shortname for act in all_acts]
    ## Make displayed labels short (recommended):
    ids = labels
    labels = texts
    # texts = ids
    texts = [relativedelta_str(act.total_duration) for act in all_acts]
    ##
    parents = [(act.parent and act.parent.name) or "" for act in all_acts]
    # values = [relativedelta_total_seconds(act.duration) for act in all_acts]
    values = [(relativedelta_total_seconds(act.total_duration)/(3600)) for act in all_acts]
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
        branchvalues = "total",
        ids = ids,
        labels = labels,
        parents = parents,
        values = values,
        text = texts,
        texttemplate = "%{label}<br>%{value:.1f}, %{percentRoot:%}",
        # textinfo = "label+value+percent parent+percent entry+percent root",
        hovertemplate = "%{label}<br>%{value:.1f}<br>%{percentParent:.1%} of %{parent}<br>%{percentEntry:.1%} of %{entry}<br>%{percentRoot:.1%} of %{root}<extra>%{currentPath}%{label}</extra>",
        # https://community.plotly.com/t/how-to-explicitly-set-colors-for-some-sectors-in-a-treemap/51162
        # color="day",
        # color_discrete_map={'(?)':'gold', 'Study':'green', 'wasted':'black'},
    )
    fig = go.Figure(go.Treemap(**plot_opts))
    # fig.update_layout(margin = dict(t=0, l=0, r=0, b=0))
    fig.update_layout(margin = dict(t=30, l=0, r=30, b=30))
    # fig.update_layout(uniformtext=dict(minsize=6, mode='hide'))
    is_local and fig.show()
    out_links, out_files = fig_export(fig, "treemap")
    ##
    # @unresolved https://community.plotly.com/t/show-the-current-path-bar-in-sunburst-plots-just-like-treemap-plots/51155
    fig = go.Figure(go.Sunburst(**plot_opts))
    fig.update_layout(margin = dict(t=0, l=0, r=0, b=0))
    # fig.update_layout(uniformtext=dict(minsize=6, mode='hide'))
    is_local and fig.show()
    l, f = fig_export(fig, "sunburst")
    out_links += l
    out_files += f
    return out_links, out_files


def fig_export(fig, exported_name, html_export = True, png_export = True, svg_export = True, pdf_export = True, width = 600, height = 400, scale = 4):
    out_links = []
    out_files = []
    if html_export:
        exported_html = f"./plots/{exported_name}.html"
        z("ensure-dir {exported_html}")
        fig.write_html(exported_html, include_plotlyjs='cdn', include_mathjax='cdn')
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
