"""A payload carrying a bare date must say which clock it is on.

MEASURED, not supposed. `GET /api/reports/daily` on sg01, 2026-09-23:

    "date": "2026-09-23"      bare, zoneless, from a UTC+08:00 host

The values are not wrong — `_ts_range` opens the window at LOCAL midnight and
the buckets render LOCAL hours, so a local day is what the whole pipeline means,
and that is almost certainly what an operator in +08 wants. What was wrong is
that the wire never said so, and the window in which it matters is arithmetic
rather than an estimate:

    local date is one day AHEAD of the UTC date for local 00:00–07:59
    -> 8 of 24 hours, 33.3% of every day

THE TEST IS STRUCTURAL, WHICH IS THE ONLY KIND THAT SURVIVES A NEW FIELD.
Asserting `"time_basis" in result` passes forever and says nothing about the
date added next month. So it WALKS the payload, finds every string that LOOKS
like a date or an hour bucket, and requires the basis beside it — rho shipped
the same shape for dated notices in 4cd72605-e6ff-4714-b21e-3c3b79fb1bfe §2, and
their reason is the one that matters: a date added later cannot then ship
without its status.
"""
import datetime
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.timebasis import time_basis   # noqa: E402

DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOURISH = re.compile(r"^\d{2}:00$")


def dated_paths(o, path=""):
    """Every place a bare date or hour bucket appears — as a VALUE or as a KEY.

    The key case is not decoration: task_analytics buckets by day and by hour
    into dicts whose KEYS are '2026-09-23' and '14:00', so a walker that only
    looked at values would have reported a clean payload.
    """
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(k, str) and (DATEISH.match(k) or HOURISH.match(k)):
                out.append(path + "/<key " + k + ">")
            out += dated_paths(v, path + "/" + str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            out += dated_paths(v, "%s[%d]" % (path, i))
    elif isinstance(o, str) and (DATEISH.match(o) or HOURISH.match(o)):
        out.append(path)
    return out


class TestTimeBasis(unittest.TestCase):

    def test_the_basis_is_read_at_call_time_not_frozen(self):
        """A host that observes DST changes offset twice a year."""
        b = time_basis()
        live = datetime.datetime.now().astimezone().utcoffset()
        self.assertEqual(b["utc_offset_seconds"], int(live.total_seconds()))

    def test_the_readers_check_is_tested_at_BOTH_dates_not_todays(self):
        """This assertion could only fail for 8 hours in every 24, and the
        mutation run happened in the other 16.

        Hardcoding `differs_from_utc_date` to False left the first version of
        this test GREEN, because it compared the field against the LIVE clock at
        09:5x local / 01:5xZ, where local and UTC dates agree. The field is only
        false-able inside the very window it exists to announce. So the record
        is now built from VALUES and both cases are exercised.
        """
        from app.timebasis import basis_from
        same = basis_from("2026-09-23", "2026-09-23", 8 * 3600, "CST")
        self.assertFalse(same["differs_from_utc_date"])
        across = basis_from("2026-09-23", "2026-09-22", 8 * 3600, "CST")
        self.assertTrue(across["differs_from_utc_date"],
                        "the one case the field exists for")
        self.assertEqual(across["utc_offset"], "+08:00")
        self.assertRegex(same["utc_offset"], r"^[+-]\d{2}:\d{2}$")

    def test_the_adapter_is_tested_ACROSS_a_boundary_by_injecting_the_clock(self):
        """THE THIRD FIX ALSO FAILED, and this is why the clock is an argument.

        Three mutations stayed green in a row. Whether `local_date` holds the
        local date or the UTC date is OBSERVABLE only when a day boundary falls
        between them — 8 hours in 24 on a +08 host. Outside that window every
        live-clock assertion compares two equal strings, however it is phrased;
        asserting "local is UTC shifted by the offset" does not help either,
        because shifting 01:5xZ by +08:00 lands on the same date.

        No cleverness with the host clock fixes that. The clock has to be an
        input.
        """
        tz8 = datetime.timezone(datetime.timedelta(hours=8))
        # 2026-09-22T23:30Z is 2026-09-23T07:30 local — opposite sides of midnight
        utcnow = datetime.datetime(2026, 9, 22, 23, 30, tzinfo=datetime.timezone.utc)
        b = time_basis(now=utcnow.astimezone(tz8), utcnow=utcnow)
        self.assertEqual(b["local_date"], "2026-09-23")
        self.assertEqual(b["utc_date"], "2026-09-22")
        self.assertTrue(b["differs_from_utc_date"])
        self.assertEqual(b["utc_offset"], "+08:00")

    def test_the_default_clock_is_still_the_host(self):
        b = time_basis()
        self.assertEqual(b["utc_date"], datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%d"))
        self.assertEqual(b["utc_offset_seconds"],
                         int(datetime.datetime.now().astimezone()
                             .utcoffset().total_seconds()))

    # ---- the structural half -------------------------------------------------
    def test_every_dateish_value_in_a_payload_is_accompanied_by_the_basis(self):
        payload = {"date": "2026-09-23", "days": 1, "time_basis": time_basis(),
                   "hourly_activity": {"09:00": 3, "10:00": 1},
                   "daily": {"2026-09-22": {"total": 4}}}
        found = dated_paths({k: v for k, v in payload.items() if k != "time_basis"})
        self.assertTrue(found, "the walker found nothing — it cannot vouch for anything")
        self.assertIn("time_basis", payload)

    def test_the_walker_sees_dates_used_as_KEYS(self):
        """The bucket dicts key BY date and BY hour; a value-only walker misses them."""
        found = dated_paths({"daily": {"2026-09-22": {"total": 4}},
                             "hourly": {"14:00": {"total": 2}}})
        self.assertIn("/daily/<key 2026-09-22>", found)
        self.assertIn("/hourly/<key 14:00>", found)

    def test_the_walker_is_not_fooled_by_a_near_miss(self):
        self.assertEqual(dated_paths({"a": "2026-9-3", "b": "9:00", "c": "2026-09-233"}), [])


if __name__ == "__main__":
    unittest.main()
