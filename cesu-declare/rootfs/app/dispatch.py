"""Routing of stdin lines: new requests vs. one-time codes for a run that is waiting."""
from __future__ import annotations

import asyncio
import json
import logging

from logic import OtpFormatError, parse_otp

log = logging.getLogger("cesu.dispatch")


def classify_line(line: str):
    """Return (kind, payload): 'empty' | 'junk' | 'otp' | 'request'."""
    line = line.strip()
    if not line:
        return "empty", None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return "junk", None
    if isinstance(obj, dict) and "otp" in obj:
        return "otp", obj
    return "request", obj


class OtpBroker:
    """Hands a code received on stdin to the run that asked for it (matched on request_id)."""

    def __init__(self) -> None:
        self._waiting: dict[str, asyncio.Future] = {}

    def expect(self, request_id: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = fut
        return fut

    def release(self, request_id: str) -> None:
        self._waiting.pop(request_id, None)

    def deliver(self, obj: dict) -> str:
        """Returns 'delivered', 'bad_format' or 'ignored' (no run is waiting for that id)."""
        rid = obj.get("request_id")
        fut = self._waiting.get(rid) if isinstance(rid, str) else None
        if fut is None or fut.done():
            log.info("ignored an otp line (no run waiting for request_id %r)", rid if isinstance(rid, str) else None)
            return "ignored"
        try:
            fut.set_result(parse_otp(obj.get("otp")))
        except OtpFormatError as exc:
            fut.set_exception(exc)
            return "bad_format"
        return "delivered"
