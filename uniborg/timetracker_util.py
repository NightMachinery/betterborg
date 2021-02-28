# Somehow putting ActivityDuration in the plugin file itself resulted in error (the culprit was probably dataclass), so I am putting them here.

from __future__ import annotations
import asyncio
import textwrap
import dataclasses
from dataclasses import dataclass
from functools import total_ordering
from typing import Dict, List
import datetime
from dateutil.relativedelta import relativedelta

lock_tt = asyncio.Lock()

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
    if rd.hours:
        res += f"{rd.hours}:"
    res += f"{rd.minutes}"
    return res


@total_ordering
@dataclass()
class ActivityDuration:
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
