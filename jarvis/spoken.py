"""
Rendering values the way a person says them out loud.

Everything JARVIS produces goes to a speech engine, and speech engines read
"09:20" as "zero nine twenty" and "3.2GB" as "three point two gee bee". The
persona asks for "twenty past nine" and "about three gigabytes", and the model
generally obliges — but the *context* handed to it must be in the same register,
because the model writes what it reads. Feed it an ISO timestamp and it will
start answering in ISO timestamps.

British idiom throughout: half past, quarter to, the twenty-ninth of August.
"""

from __future__ import annotations

from datetime import datetime

ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    5: "fifth",
    8: "eighth",
    9: "ninth",
    12: "twelfth",
    20: "twentieth",
    30: "thirtieth",
}
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def number(value: int) -> str:
    """Cardinal numbers up to 999. Beyond that, digits read fine aloud."""
    if value < 0:
        return f"minus {number(-value)}"
    if value < 20:
        return ONES[value]
    if value < 100:
        tens, rest = divmod(value, 10)
        return TENS[tens] + (f"-{ONES[rest]}" if rest else "")
    hundreds, rest = divmod(value, 100)
    head = f"{ONES[hundreds]} hundred"
    return f"{head} and {number(rest)}" if rest else head


def ordinal(value: int) -> str:
    """ "the twenty-ninth", for dates."""
    if value in ORDINALS:
        return ORDINALS[value]
    if value < 20:
        return ONES[value] + "th"
    tens, rest = divmod(value, 10)
    if rest == 0:
        return TENS[tens][:-1] + "ieth"
    return f"{TENS[tens]}-{ORDINALS.get(rest, ONES[rest] + 'th')}"


def part_of_day(hour: int) -> str:
    if hour < 5:
        return "in the small hours"
    if hour < 12:
        return "in the morning"
    if hour < 18:
        return "in the afternoon"
    if hour < 22:
        return "in the evening"
    return "at night"


def clock(when: datetime, *, with_part: bool = True) -> str:
    """ "twenty past nine in the evening"."""
    hour, minute = when.hour, when.minute
    spoken_hour = number(hour % 12 or 12)
    next_hour = number((hour + 1) % 12 or 12)
    if minute == 0:
        core = f"{spoken_hour} o'clock"
    elif minute == 15:
        core = f"quarter past {spoken_hour}"
    elif minute == 30:
        core = f"half past {spoken_hour}"
    elif minute == 45:
        core = f"quarter to {next_hour}"
    elif minute < 30:
        unit = "" if minute % 5 == 0 else " minutes"
        core = f"{number(minute)}{unit} past {spoken_hour}"
    else:
        left = 60 - minute
        unit = "" if left % 5 == 0 else " minutes"
        core = f"{number(left)}{unit} to {next_hour}"
    return f"{core} {part_of_day(hour)}" if with_part else core


def date(when: datetime) -> str:
    """ "Friday the twenty-ninth of August"."""
    weekday = when.strftime("%A")
    return f"{weekday} the {ordinal(when.day)} of {MONTHS[when.month - 1]}"


def moment(when: datetime) -> str:
    """The whole thing, as the persona's {now} wants it."""
    return f"{date(when)}, {clock(when)}"


def duration(seconds: float) -> str:
    """ "about forty minutes", "three hours". Vague on purpose: exact is robotic."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{number(max(seconds, 1))} seconds" if seconds != 1 else "a second"
    minutes = round(seconds / 60)
    if minutes < 60:
        return "a minute" if minutes == 1 else f"{number(minutes)} minutes"
    hours = seconds / 3600
    whole = int(hours)
    if whole >= 24:
        days = round(hours / 24)
        return "a day" if days == 1 else f"{number(days)} days"
    if abs(hours - whole) < 0.25:
        return "an hour" if whole == 1 else f"{number(whole)} hours"
    if abs(hours - whole - 0.5) < 0.25:
        return "an hour and a half" if whole == 1 else f"{number(whole)} and a half hours"
    return f"{number(whole)} hours"


def quantity(value: float, unit: str) -> str:
    """ "about three gigabytes" — rounded, because nobody says point two aloud."""
    if value >= 10:
        return f"{number(round(value))} {unit}"
    if value >= 1:
        rounded = round(value * 2) / 2
        whole = int(rounded)
        half = rounded - whole
        head = number(whole)
        if half and whole:
            return f"{head} and a half {unit}"
        return f"{head} {unit}"
    return f"{number(round(value * 100))} percent of a {unit.rstrip('s')}"
