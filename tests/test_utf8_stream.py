"""Reddening test for the incremental UTF-8 stream decoder (app/hands/base.py).

The guarantee: a multi-byte character split across a chunk-read boundary is
never replaced with U+FFFD. The mutation that must turn this red: swap
utf8_stream_decoder() back to per-chunk `chunk.decode('utf-8',
errors='replace')` — the pre-2026-08-17 pattern that corrupted CJK output at
rates proportional to chunk-boundary frequency (codex 256-byte reads ≈100%,
opencode 2048-byte reads ≈29% on CJK-bearing tasks, measured 2026-08-14).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.hands.base import utf8_stream_decoder

SRC = "锂电池产业链2026年扩产节奏放缓，紫金矿业铜产量指引上调至115万吨。宁德时代Q2毛利率28.3%。"
RAW = SRC.encode("utf-8")


def _chunks(start, size):
    return [RAW[:start]] + [RAW[i:i + size] for i in range(start, len(RAW), size)]


def test_boundary_splits_never_corrupt():
    for size in (1, 2, 3, 5, 7, 256):
        for start in range(1, min(size, len(RAW))):
            dec = utf8_stream_decoder()
            out = "".join(dec.decode(c) for c in _chunks(start, size))
            out += dec.decode(b"", final=True)
            assert "�" not in out, (size, start)
            assert out == SRC, (size, start)


def test_genuine_truncation_is_still_flagged():
    dec = utf8_stream_decoder()
    out = dec.decode(RAW[:-1]) + dec.decode(b"", final=True)
    assert "�" in out  # a dangling partial sequence IS real truncation


def test_the_old_pattern_is_actually_broken():
    # Documents WHY the helper exists: the per-chunk pattern corrupts.
    corrupted = 0
    for size in (1, 2, 3):
        for start in range(1, size if size <= len(RAW) else len(RAW)):
            out = "".join(c.decode("utf-8", errors="replace") for c in _chunks(start, size))
            corrupted += "�" in out
    assert corrupted > 0


if __name__ == "__main__":
    test_boundary_splits_never_corrupt()
    test_genuine_truncation_is_still_flagged()
    test_the_old_pattern_is_actually_broken()
    print("all pass")
