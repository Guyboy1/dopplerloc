"""When was the recording made? Local date/time (+ time zone) -> UTC windows for the recording START.

Rules (enforced in jobs.py):
  * continuous recording, one Doppler curve  -> an EXACT start time (to the second) is required
  * continuous recording, two or more curves -> a day (or up to 7 days) is enough; a time of day only helps
  * recording with CUTS                      -> start time (to the minute) here, then the real clock time of every
                                                curve is asked for once the curves are found
"""
import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_TOL_TYPED = 5.0       # seconds of clock uncertainty for a typed time
DEFAULT_TOL_METADATA = 2.0    # ... for a time read from iPhone metadata


def parse_hms(s):
    """'HH:MM' or 'HH:MM:SS' -> (h, m, s, had_seconds)."""
    parts = [int(x) for x in str(s).strip().split(":")]
    if len(parts) not in (2, 3) or not (0 <= parts[0] < 24 and 0 <= parts[1] < 60):
        raise ValueError(f"time '{s}' must look like 14:05 or 14:05:30")
    sec = parts[2] if len(parts) == 3 else 0
    if not 0 <= sec < 60:
        raise ValueError(f"time '{s}' has invalid seconds")
    return parts[0], parts[1], sec, len(parts) == 3


@dataclass
class Timeframe:
    year: int
    month: int
    tz: str
    day: Optional[int] = None
    span_days: int = 1               # search this many days starting at `day` (max 7)
    start: Optional[str] = None      # "HH:MM"  time-of-day range (continuous recordings only)
    end: Optional[str] = None
    exact: Optional[str] = None      # "HH:MM[:SS]" exact moment the recording STARTED
    tolerance_s: float = DEFAULT_TOL_TYPED
    cuts: bool = False               # the recording was edited / has cuts

    def validate(self):
        if not (1900 < self.year < 2200 and 1 <= self.month <= 12):
            raise ValueError("year and month are required")
        if self.day is None:
            raise ValueError("choose the day (the month alone is too broad to search)")
        if not 1 <= self.day <= calendar.monthrange(self.year, self.month)[1]:
            raise ValueError("day is not in that month")
        if not 1 <= int(self.span_days) <= 7:
            raise ValueError("you can search 1 to 7 days")
        if self.cuts and not self.exact:
            raise ValueError("a recording with cuts needs its start time (to the minute) - the real clock time of each "
                             "curve is asked for after the curves are found")
        if self.exact and (self.start or self.end):
            raise ValueError("give either an exact start time or a time-of-day range, not both")
        if bool(self.start) != bool(self.end):
            raise ValueError("give both a start and an end time, or an exact time")
        if self.tolerance_s < 0:
            raise ValueError("clock tolerance cannot be negative")

    @property
    def is_exact(self):
        return bool(self.exact)

    @property
    def effective_tolerance(self):
        """Typed minutes only (no seconds) mean +-30 s at best."""
        if self.exact and not parse_hms(self.exact)[3]:
            return max(self.tolerance_s, 30.0)
        return self.tolerance_s

    def windows(self):
        """[(utc_start, utc_end)] possible START of the recording, one per local day searched (epoch seconds)."""
        self.validate()
        zone = ZoneInfo(self.tz)

        def at(day_offset, hms="00:00:00"):
            h, m, s, _ = parse_hms(hms)
            d = datetime(self.year, self.month, self.day, h, m, s, tzinfo=zone) + timedelta(days=day_offset)
            return d.astimezone(timezone.utc).timestamp()

        if self.exact:
            t = at(0, self.exact)
            return [(t, t)]
        out = []
        for k in range(int(self.span_days)):
            if self.start and self.end:
                a, b = at(k, self.start), at(k, self.end)
                if b <= a:
                    b = at(k + 1, self.end)
            else:
                a, b = at(k), at(k + 1)
            out.append((a, b))
        return out

    def t0_window(self, window):
        """Possible recording start, widened by the clock tolerance (exact) or a small slack (ranges)."""
        a, b = window
        if self.exact:
            return (a - self.effective_tolerance, b + self.effective_tolerance)
        return (a - 30.0, b)


def local_str(epoch, tz):
    return datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S")


def parse_local(text, tz, default_date):
    """'HH:MM[:SS]' (on default_date=(y,m,d)) or 'YYYY-MM-DD HH:MM[:SS]' -> epoch seconds (UTC)."""
    text = text.strip()
    if " " in text:
        d, t = text.split(" ", 1)
        y, mo, da = (int(x) for x in d.split("-"))
    else:
        (y, mo, da), t = default_date, text
    h, m, s, _ = parse_hms(t)
    return datetime(y, mo, da, h, m, s, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).timestamp()


def describe(windows):
    hours = sum(b - a for a, b in windows) / 3600
    return f"{len(windows)} window(s), {hours:.1f} h in total" if hours else f"{len(windows)} exact start time"
