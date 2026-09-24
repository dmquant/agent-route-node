"""A sweep where SOME files could not be read must not exit clean.

rho RHO-227 (8a180a47-1136-4195-92f4-90d558d10acf) asked whether any sweep here
counts a failed probe and then judges only the all-failed case. This one did:
an EMPTY root returned 2, but a root with one unreadable file and no hits
printed a warning and then "No saved refusals" with exit 0.

The asymmetry is rho's: a failed read cannot manufacture a hit, so a partial
sweep with hits still exits 1; it can hide one, so without hits it exits 2.
"""
import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location("sweep_refusals", "scripts/sweep_refusals.py")
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)

REFUSAL = '{"error":"X-Admin-Token or X-API-Key (viewer:read) required","credential_presented":false}'


def _run(root, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sweep_refusals.py", str(root)])
    return sw.main()


def _unreadable(path):
    path.write_text("# anything\n")
    os.chmod(path, 0)


def test_control_a_readable_clean_root_exits_0(tmp_path, monkeypatch):
    (tmp_path / "good.md").write_text("# a real document\n")
    assert _run(tmp_path, monkeypatch) == 0


def test_a_partial_sweep_with_no_hits_is_not_clean(tmp_path, monkeypatch, capsys):
    (tmp_path / "good.md").write_text("# a real document\n")
    _unreadable(tmp_path / "locked.md")
    try:
        assert os.access(tmp_path / "locked.md", os.R_OK) is False  # control: really unreadable
        assert _run(tmp_path, monkeypatch) == 2
        assert "NOT A CLEAN RESULT" in capsys.readouterr().out
    finally:
        os.chmod(tmp_path / "locked.md", 0o600)


def test_a_hit_survives_a_partial_sweep(tmp_path, monkeypatch):
    (tmp_path / "refused.md").write_text(REFUSAL)
    _unreadable(tmp_path / "locked.md")
    try:
        assert _run(tmp_path, monkeypatch) == 1
    finally:
        os.chmod(tmp_path / "locked.md", 0o600)
