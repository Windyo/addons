"""Fetch the URSSAF one-time code from an IMAP mailbox. Never logs the code."""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime

from config import Options
from logic import extract_otp

log = logging.getLogger("cesu.otp")
CLOCK_SKEW = timedelta(seconds=60)


class OtpTimeout(Exception):
    pass


def _connect(opt: Options) -> imaplib.IMAP4:
    if opt.imap_security == "ssl":
        conn = imaplib.IMAP4_SSL(opt.imap_host, opt.imap_port, timeout=30)
    else:
        conn = imaplib.IMAP4(opt.imap_host, opt.imap_port, timeout=30)
        ctx = ssl.create_default_context()
        if opt.imap_security == "starttls_insecure":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn.starttls(ssl_context=ctx)
    conn.login(opt.imap_user, opt.imap_password)
    return conn


def _bodies(msg: Message) -> tuple[str, str]:
    plain, html = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if ctype == "text/plain" else html).append(text)
    return "\n".join(plain), "\n".join(html)


def _message_time(msg: Message, internal: datetime | None) -> datetime | None:
    """Most recent of the Date header and INTERNALDATE (either may be missing/skewed)."""
    times = []
    if internal:
        times.append(internal)
    try:
        d = parsedate_to_datetime(msg.get("Date", ""))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        times.append(d)
    except (TypeError, ValueError):
        pass
    return max(times) if times else None


def _poll_once(opt: Options, since: datetime) -> str | None:
    cutoff = since - CLOCK_SKEW
    conn = _connect(opt)
    try:
        typ, _ = conn.select(f'"{opt.imap_folder}"')
        if typ != "OK":
            raise RuntimeError(f"cannot select IMAP folder {opt.imap_folder!r}")
        day = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = conn.uid("SEARCH", None, "SINCE", day)
        if typ != "OK" or not data or not data[0]:
            return None
        uids = data[0].split()[-30:]
        log.debug("IMAP: %d candidate message(s) since %s", len(uids), day)
        for uid in reversed(uids):  # newest first
            typ, fetched = conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[])")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            meta, raw = fetched[0]
            internal = None
            try:
                tt = imaplib.Internaldate2tuple(meta)
                if tt:
                    internal = datetime.fromtimestamp(time.mktime(tt), timezone.utc)
            except Exception:
                internal = None
            msg = email.message_from_bytes(raw)
            if opt.otp_sender_filter.lower() not in (msg.get("From", "") or "").lower():
                continue
            when = _message_time(msg, internal)
            if when is None or when < cutoff:
                continue
            plain, html = _bodies(msg)
            code = extract_otp(plain, html)
            if code:
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                return code
            log.debug("IMAP: matching message without a 6-digit code")
        return None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


async def wait_for_otp(opt: Options, since: datetime, timeout: float = 180, interval: float = 5) -> str:
    """Poll until a fresh code arrives. `since` is the UTC time just before the password is sent."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            code = await asyncio.to_thread(_poll_once, opt, since)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError, RuntimeError) as exc:
            # transient mailbox trouble: keep polling until the deadline
            log.warning("IMAP poll %d failed: %s", attempt, type(exc).__name__)
            code = None
        if code:
            log.info("OTP received after %d poll(s)", attempt)
            return code
        if time.monotonic() + interval > deadline:
            raise OtpTimeout(f"no email code from '{opt.otp_sender_filter}' within {int(timeout)} s")
        await asyncio.sleep(interval)
