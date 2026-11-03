"""Add-on options from /data/options.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

OPTIONS_PATH = os.environ.get("CESU_OPTIONS", "/data/options.json")


@dataclass(frozen=True)
class Options:
    cesu_login: str
    cesu_password: str
    imap_host: str
    imap_port: int
    imap_security: str
    imap_user: str
    imap_password: str
    imap_folder: str
    otp_sender_filter: str
    log_level: str

    @property
    def imap_mode(self) -> bool:
        return bool(self.imap_host)

    @property
    def secrets(self) -> list[str]:
        return [self.cesu_login, self.cesu_password, self.imap_user, self.imap_password]


def load_options(path: str = OPTIONS_PATH) -> Options:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    def text(key: str, default: str = "") -> str:
        value = raw.get(key)
        return default if value is None or value == "" else str(value)

    return Options(
        cesu_login=text("cesu_login", ""),
        cesu_password=text("cesu_password", ""),
        imap_host=text("imap_host", ""),
        imap_port=int(text("imap_port", "993")),
        imap_security=text("imap_security", "ssl"),
        imap_user=text("imap_user", ""),
        imap_password=text("imap_password", ""),
        imap_folder=text("imap_folder", "INBOX"),
        otp_sender_filter=text("otp_sender_filter", "urssaf"),
        log_level=text("log_level", "info"),
    )
