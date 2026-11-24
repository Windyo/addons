"""Months this add-on clicked "Enregistrer ma déclaration" for, in /data/submitted.json.

The month is written before the click, so a crash or an unrecognised answer still blocks a second
declaration. Only "dialog" (a dialog appeared and nothing more was clicked) allows another try."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path("/data/submitted.json")
RETRY_OK = {"dialog"}


def load(path: Path = LEDGER_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def blocking_entry(month: str, path: Path = LEDGER_PATH) -> dict | None:
    """The ledger entry that forbids a real run for `month`, or None. An unreadable ledger blocks."""
    try:
        entry = load(path).get(month)
    except (OSError, ValueError) as exc:
        return {"outcome": f"ledger unreadable ({type(exc).__name__})", "at": ""}
    if isinstance(entry, dict) and entry.get("outcome") not in RETRY_OK:
        return entry
    return None


def record(month: str, outcome: str, path: Path = LEDGER_PATH, **extra) -> None:
    """Write (atomically, fsynced) the outcome for `month`. Raises if it cannot be written."""
    data = load(path)
    data[month] = {"outcome": outcome, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
