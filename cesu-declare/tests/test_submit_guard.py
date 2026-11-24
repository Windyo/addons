"""v0.3 safety net: the submit click exists once, only on the real branch, after the ledger write."""
import re
from pathlib import Path

import pytest

import ledger
from logic import RequestError, validate_request

APP = Path(__file__).resolve().parent.parent / "rootfs" / "app"
RUNNER = (APP / "runner.py").read_text(encoding="utf-8")


def test_submit_button_located_once():
    assert 'SUBMIT_NAME = "Enregistrer ma déclaration"' in RUNNER
    assert RUNNER.count("name=SUBMIT_NAME") == 1
    assert len(re.findall(r'name="Enregistrer', RUNNER)) == 0


def test_every_click_is_known():
    clicks = [line.strip() for line in RUNNER.splitlines() if ".click(" in line]
    assert clicks == [
        "await logout.click()",  # _logout
        "await dashboard_link.first.click()",
        "await login_link.first.click()",
        'await page.get_by_role("button", name="Vérifier").click()',
        "await declare_link.first.click()",
        "await modal_btn.first.click()  # informational modal; optional",
        'await page.get_by_role("button", name="Étape suivante", exact=True).click()',
        'await consent.evaluate("el => { if (!el.checked) el.click(); }")  # styled, hidden input',
        "await submit.click()",
    ]


def test_submit_only_on_real_branch_after_ledger():
    assert 'real = req["dry_run"] is False' in RUNNER
    dry_stop = RUNNER.index("if not real:\n")
    record = RUNNER.index('ledger.record(req["month"], "clicked"')
    click = RUNNER.index("await submit.click()")
    assert dry_stop < record < click
    assert 'return {"status": "dry_run_ok"' in RUNNER[dry_stop:record]


@pytest.mark.parametrize("value", ["false", "False", 0, None, []])
def test_dry_run_must_be_a_real_boolean(value):
    with pytest.raises(RequestError):
        validate_request({"request_id": "a", "month": "2026-11", "hours": 11, "rate": 16, "dry_run": value})


def test_dry_run_defaults_to_true():
    assert validate_request({"request_id": "a", "month": "2026-11", "hours": 11, "rate": 16})["dry_run"] is True


def test_ledger_blocks_after_click(tmp_path):
    p = tmp_path / "submitted.json"
    assert ledger.blocking_entry("2026-11", p) is None
    ledger.record("2026-11", "clicked", p, request_id="r1")
    assert ledger.blocking_entry("2026-11", p)["outcome"] == "clicked"
    assert ledger.blocking_entry("2026-12", p) is None
    for outcome in ("declared", "alert", "unknown"):
        ledger.record("2026-11", outcome, p)
        assert ledger.blocking_entry("2026-11", p)["outcome"] == outcome


def test_ledger_dialog_allows_retry(tmp_path):
    p = tmp_path / "submitted.json"
    ledger.record("2026-11", "dialog", p)
    assert ledger.blocking_entry("2026-11", p) is None


def test_corrupt_ledger_blocks(tmp_path):
    p = tmp_path / "submitted.json"
    p.write_text("{not json", encoding="utf-8")
    assert "unreadable" in ledger.blocking_entry("2026-11", p)["outcome"]
    with pytest.raises(ValueError):
        ledger.record("2026-11", "clicked", p)  # so the click never happens
