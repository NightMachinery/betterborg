from dateutil.relativedelta import relativedelta


def relativedelta_total_seconds(rd: relativedelta):
    # Used Google to convert the years and months, they are slightly more than 365 and 30 days respectively.
    return rd.years * 31540000 + rd.months * 2628000 + rd.days * 86400 + rd.hours * 3600 + rd.minutes * 60 + rd.seconds

def relativedelta_str(rd: relativedelta, **kwargs):
    rd = rd.normalized()
    s = relativedelta_total_seconds(rd)
    return seconds_str(s, **kwargs)
