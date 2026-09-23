"""The zone every published date and hour in this node is expressed in.

WHY THIS IS A MODULE AND NOT TWO HELPERS

`app/mcp_config.py` carries the scar: DEFAULT_HEADER "lives here alone because
it did not, and the drift shipped" — two modules defaulted the same variable
differently and a node was configured wrong for weeks. The same shape was
available here: `report_engine` and `task_analytics` each build a day window
from local midnight and each render buckets with a naive `datetime`, and either
could have been "fixed" alone into disagreeing with the other.

WHAT THE DEFECT ACTUALLY IS, AND WHAT IT IS NOT

Measured 2026-09-23 on sg01 and gubei12, both `UTC+08:00`:

    GET /api/reports/daily  ->  "date": "2026-09-23"   bare, no zone

The pipeline is internally COHERENT — `_ts_range` opens the window at LOCAL
midnight and the buckets render LOCAL hours — and a local day is almost
certainly what an operator in +08 wants. So the values are not wrong and this
module does not change one.

What is wrong is that the wire does not say which clock they are on:

    local date is one day AHEAD of the UTC date for local 00:00–07:59
    -> 8 of 24 hours, 33.3% of every day

rho fixed the identical shape upstream by adding `timezone` beside a date
rather than by moving the date, because a bare date is a unit-less quantity.
This estate then shipped the WORSE version twice in two hours — a local time
wearing a `Z`, which declares a unit it does not have (corrected in AGORA
c3f054b4-b32c-4026-8d93-580ae84f5082 §4).

So: label, never shift. A consumer that needs UTC can convert; a consumer that
cannot see the offset cannot even know to ask.

THE OFFSET IS READ AT CALL TIME, NOT IMPORT TIME. A host that observes DST
changes offset twice a year, and a value frozen at import would be silently
wrong for half of it. These hosts do not observe DST today, which is exactly the
kind of fact that stops being true without anyone editing this file.
"""
from datetime import datetime, timezone

__all__ = ["time_basis", "fmt_offset"]


def fmt_offset(secs: int) -> str:
    """Seconds east of UTC as "+HH:MM" / "-HH:MM".

    SPLIT OUT SO IT CAN BE TESTED WITHOUT PATCHING THE CLOCK. The first version
    of its test faked `datetime.now`, the fake returned an aware value,
    `.astimezone()` converted it straight back to the host zone, and the test
    asserted `-04:00` against the host's own `+08:00`. A pure function of an
    integer needs no clock at all — and the hazard here is real: `divmod` on a
    negative rounds toward minus infinity, so the sign has to come off first.
    Half-hour and three-quarter-hour zones exist (+05:30, +05:45, -03:30), so
    the minutes are not decorative either.
    """
    sign = "+" if secs >= 0 else "-"
    hh, mm = divmod(abs(secs) // 60, 60)
    return "%s%02d:%02d" % (sign, hh, mm)


def time_basis() -> dict:
    """What clock this node's published dates and hour buckets are on.

    Everything here is derived from the host at call time; nothing is declared.
    """
    now = datetime.now().astimezone()
    off = now.utcoffset()
    secs = int(off.total_seconds()) if off is not None else 0
    utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "basis": "host local time",
        "utc_offset": fmt_offset(secs),
        "utc_offset_seconds": secs,
        "tzname": now.tzname() or "",
        # THE READER'S CHECK, AND IT IS THE POINT OF THE FIELD.
        # A consumer joining this record to a UTC timeline can compare these two
        # without knowing anything about the host. When they differ, every bare
        # date and hour in this payload is on the local side of a day boundary.
        "local_date": now.strftime("%Y-%m-%d"),
        "utc_date": utc_date,
        "differs_from_utc_date": now.strftime("%Y-%m-%d") != utc_date,
        "note": ("every bare `date`, `%Y-%m-%d` key and `%H:00` bucket in this "
                 "payload is on this clock, not UTC. Values are not shifted — "
                 "they are labelled, because a bare date is a unit-less quantity."),
    }
