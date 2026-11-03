"""Pure helpers (no browser, no network): request validation, OTP extraction, formatting."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from html import unescape
from html.parser import HTMLParser

MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
OTP_RE = re.compile(r"(?<![\d])\d{6}(?![\d])")
MIN_HOURS, MAX_HOURS = 1, 200
RATE_MIN, RATE_MAX = 10, 40


class RequestError(ValueError):
    """Raised for an invalid stdin request."""


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_request(obj) -> dict:
    """Return a normalised request or raise RequestError. dry_run is echoed, never acted on."""
    if not isinstance(obj, dict):
        raise RequestError("request must be a JSON object")
    request_id = obj.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
        raise RequestError("request_id must be a non-empty string (max 100 chars)")
    month = obj.get("month")
    month = "" if month is None else str(month)  # templates may render non-strings
    if not MONTH_RE.match(month):
        raise RequestError("month must be a string formatted YYYY-MM")
    hours = obj.get("hours")
    if not _is_number(hours) or hours != int(hours) or not 1 <= hours <= MAX_HOURS:
        raise RequestError(f"hours must be a whole number, {MIN_HOURS} to {MAX_HOURS}")
    rate = obj.get("rate")
    if not _is_number(rate) or not RATE_MIN <= rate <= RATE_MAX:
        raise RequestError(f"rate must be a number between {RATE_MIN} and {RATE_MAX}")
    dry_run = obj.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise RequestError("dry_run must be a boolean")
    return {
        "request_id": request_id,
        "month": month,
        "hours": int(hours),
        "rate": float(rate),
        "dry_run": dry_run,
    }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        else:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: fall back to a crude strip
        return unescape(re.sub(r"<[^>]*>", " ", html))
    return "".join(parser.parts)


def extract_otp(plain: str | None, html: str | None = None) -> str | None:
    """First standalone 6-digit number: from text/plain, else from tag-stripped HTML."""
    if plain:
        m = OTP_RE.search(plain)
        if m:
            return m.group(0)
    if html:
        m = OTP_RE.search(html_to_text(html))
        if m:
            return m.group(0)
    return None


MONTHS_FR = ("Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août",
             "Septembre", "Octobre", "Novembre", "Décembre")


def period_label(month: str) -> str:
    """'2026-11' -> 'Novembre 2026' (the label of the "Période d'emploi" option)."""
    m = MONTH_RE.match(month)
    if not m:
        raise ValueError("month must be YYYY-MM")
    return f"{MONTHS_FR[int(m.group(2)) - 1]} {m.group(1)}"


_NUM_RE = re.compile(r"-?\d[\d\s\u00a0\u202f.]*(?:,\d+)?")


def parse_fr_number(text: str | None) -> float | None:
    """'192,00' -> 192.0, '1 234,50 €' -> 1234.5, '12.5' -> 12.5; None when there is no number."""
    m = _NUM_RE.search(text or "")
    if not m:
        return None
    raw = re.sub(r"[\s\u00a0\u202f]", "", m.group(0))
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def expected_total(hours: int, rate: float) -> float:
    """hours * rate as typed on the site (rate rounded to cents first), rounded to cents."""
    typed = Decimal(str(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float((typed * hours).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_rate_fr(rate: float) -> str:
    """16 -> '16', 16.5 -> '16,50', 16.255 -> '16,26' (comma decimal, 2 places when needed)."""
    d = Decimal(str(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d == d.to_integral_value():
        return str(int(d))
    return f"{d:.2f}".replace(".", ",")


def redact(text: str, secrets) -> str:
    """Remove every non-empty secret from text."""
    for s in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(s, "***")
    return text


def media_url_path(local_path: str) -> str:
    """/media/cesu/x.png -> /media/local/cesu/x.png (Home Assistant media-source path)."""
    if local_path.startswith("/media/"):
        return "/media/local/" + local_path[len("/media/"):]
    return local_path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OtpFormatError(ValueError):
    pass


def parse_otp(value) -> str:
    """Accept a str or an int (HA renders templates to native types); require exactly 6 digits."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OtpFormatError("otp must be a string or integer of exactly 6 digits")
    text = str(value).strip()
    if not re.fullmatch(r"\d{6}", text):
        raise OtpFormatError("otp must be exactly 6 digits")
    return text
