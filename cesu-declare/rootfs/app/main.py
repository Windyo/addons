"""CESU Declare add-on, v0.2.0: dry-run explorer. Long-running; reads JSON lines on stdin."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import load_options
from dispatch import OtpBroker, classify_line
from logic import RequestError, media_url_path, redact, validate_request
from otp import wait_for_otp

log = logging.getLogger("cesu")
EVENT_URL_BASE = "http://supervisor/core/api/events/"
RUN_TIMEOUT_S = 900
OTP_ASK_TIMEOUT_S = 300
RETENTION_DAYS = 90
CAPTURE_ROOT = Path("/media/cesu")


class RedactFilter(logging.Filter):
    def __init__(self, secrets) -> None:
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        return True


def setup_logging(level: str, secrets) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    handler.addFilter(RedactFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if level == "debug" else logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def cleanup_old_runs(root: Path = CAPTURE_ROOT, days: int = RETENTION_DAYS) -> None:
    if not root.is_dir():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for d in root.iterdir():
        if not (d.is_dir() and re.fullmatch(r"\d{8}T\d{6}Z", d.name)):
            continue
        when = datetime.strptime(d.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if when < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            log.info("deleted old capture folder %s", d.name)


def post_event(data: dict, event: str = "cesu_declare_result") -> None:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    req = urllib.request.Request(
        EVENT_URL_BASE + event, data=json.dumps(data).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info("event %s sent: status=%s http=%s", event, data.get("status"), resp.status)
    except Exception as exc:
        log.error("could not send the result event: %s", type(exc).__name__)


def result(req: dict | None, status: str, step: str, message: str, **extra) -> dict:
    req = req or {}
    return {
        "request_id": req.get("request_id", ""),
        "month": req.get("month", ""),
        "hours": req.get("hours"),
        "rate": req.get("rate"),
        "dry_run": req.get("dry_run", True),
        "status": status,
        "step": step,
        "message": message,
        "period_seen": extra.get("period_seen", ""),
        "employee_seen": extra.get("employee_seen", ""),
        "activity_seen": extra.get("activity_seen", ""),
        "total_seen": extra.get("total_seen"),
        "recap_step": extra.get("recap_step", ""),
        "recap_buttons": extra.get("recap_buttons", []),
        "recap_text": extra.get("recap_text", ""),
        "screenshot": extra.get("screenshot", ""),
    }


async def ask_for_otp(broker: OtpBroker, req: dict) -> str:
    """Ask mode: fire cesu_otp_needed, then wait for {"request_id", "otp"} on stdin."""
    fut = broker.expect(req["request_id"])
    try:
        await asyncio.to_thread(post_event, {
            "request_id": req["request_id"], "month": req["month"], "timeout_s": OTP_ASK_TIMEOUT_S,
        }, "cesu_otp_needed")
        log.info("waiting up to %d s for the code on stdin", OTP_ASK_TIMEOUT_S)
        return await asyncio.wait_for(fut, OTP_ASK_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise RuntimeError(f"no code received on stdin within {OTP_ASK_TIMEOUT_S} s") from None
    finally:
        broker.release(req["request_id"])


async def handle_run(opt, req: dict, broker: OtpBroker) -> None:
    from runner import run_dry  # imported lazily so a bad browser install only fails runs

    async def get_otp(since):
        if opt.imap_mode:
            return await wait_for_otp(opt, since)
        return await ask_for_otp(broker, req)

    state: dict = {"step": "start"}
    try:
        outcome = await asyncio.wait_for(run_dry(opt, req, state, get_otp), RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        outcome = {"status": "error", "step": state["step"], "message": f"run exceeded {RUN_TIMEOUT_S} s"}
    except Exception as exc:  # e.g. browser failed to launch
        msg = redact(f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}", opt.secrets)[:300]
        log.error("run crashed at step %s: %s", state["step"], msg)
        outcome = {"status": "error", "step": state["step"], "message": msg}
    cap = state.get("capture")
    shot = ""
    if cap is not None and cap.last_png is not None:
        shot = media_url_path(str(cap.last_png))
    data = result(req, outcome["status"], outcome["step"], outcome["message"],
                  period_seen=state.get("period_seen", ""), employee_seen=state.get("employee_seen", ""),
                  activity_seen=state.get("activity_seen", ""), total_seen=state.get("total_seen"),
                  recap_step=state.get("recap_step", ""), recap_buttons=state.get("recap_buttons", []),
                  recap_text=state.get("recap_text", ""), screenshot=shot)
    await asyncio.to_thread(post_event, data)


def split_json_objects(buf: str) -> tuple[list[str], str]:
    """Cut complete JSON values out of buf. Returns (texts, rest).

    hassio.addon_stdin does not guarantee a trailing newline, so messages are delimited by
    JSON structure, not by lines. Garbage before a value is returned as its own junk text."""
    out: list[str] = []
    dec = json.JSONDecoder()
    i = 0
    while True:
        while i < len(buf) and buf[i].isspace():
            i += 1
        if i >= len(buf):
            return out, ""
        try:
            _, end = dec.raw_decode(buf, i)
        except json.JSONDecodeError as err:
            if buf[i] == "{" and (err.pos >= len(buf) or err.msg.startswith("Unterminated string")):
                return out, buf[i:]  # incomplete object: wait for more bytes
            nxt = buf.find("{", i + 1)
            if nxt == -1:
                out.append(buf[i:])
                return out, ""
            out.append(buf[i:nxt])
            i = nxt
            continue
        out.append(buf[i:end])
        i = end


async def stdin_lines():
    """Yield one JSON message (as text) at a time without blocking the loop."""
    loop = asyncio.get_running_loop()
    buf = ""
    try:
        reader = asyncio.StreamReader(limit=1 << 20)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        read = lambda: reader.read(65536)  # noqa: E731
    except (ValueError, OSError, NotImplementedError):
        # stdin is not a pipe (tty/file): fall back to a blocking reader in a thread
        read = lambda: loop.run_in_executor(None, lambda: sys.stdin.buffer.read1(65536))  # noqa: E731
    while True:
        raw = await read()
        if not raw:
            if buf.strip():
                yield buf
            return
        buf += raw.decode("utf-8", errors="replace")
        if len(buf) > 1 << 20:
            log.warning("discarding 1 MB of unparsable stdin")
            buf = ""
            continue
        texts, buf = split_json_objects(buf)
        for t in texts:
            yield t


def _echo_fields(obj) -> dict:
    d = obj if isinstance(obj, dict) else {}
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
    text = lambda v: v if isinstance(v, str) else ""  # noqa: E731
    return {"request_id": text(d.get("request_id")), "month": text(d.get("month")),
            "hours": num(d.get("hours")), "rate": num(d.get("rate")),
            "dry_run": d.get("dry_run") if isinstance(d.get("dry_run"), bool) else True}


async def dispatch_line(line: str, opt, broker: OtpBroker, current: asyncio.Task | None) -> asyncio.Task | None:
    """Route one stdin line. Returns the (possibly new) current run task."""
    kind, obj = classify_line(line)
    if kind == "empty":
        return current
    if kind == "junk":
        log.warning("ignored a non-JSON stdin line")
        await asyncio.to_thread(post_event, result(None, "invalid", "request", "stdin line is not valid JSON"))
        return current
    if kind == "otp":
        outcome = broker.deliver(obj)  # never log the value
        if outcome == "bad_format":
            log.warning("received an otp line in the wrong format")
        return current
    try:
        req = validate_request(obj)
    except RequestError as exc:
        log.warning("invalid request: %s", exc)
        await asyncio.to_thread(post_event, result(_echo_fields(obj), "invalid", "request", str(exc)))
        return current
    if current is not None and not current.done():
        log.warning("busy: rejected request %s", req["request_id"])
        await asyncio.to_thread(post_event, result(req, "busy", "request", "another run is in progress"))
        return current
    return asyncio.create_task(handle_run(opt, req, broker))


async def main() -> None:
    opt = load_options()
    setup_logging(opt.log_level, opt.secrets)
    log.info("CESU Declare v0.2.0 started (dry run only; no declaration is ever sent)")
    log.info("code source: %s", f"IMAP {opt.imap_host}" if opt.imap_mode else "ask (stdin)")
    cleanup_old_runs()
    broker = OtpBroker()
    current: asyncio.Task | None = None
    async for line in stdin_lines():
        current = await dispatch_line(line, opt, broker, current)
    log.warning("stdin closed; waiting for the container to stop")
    if current is not None:
        await current
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
