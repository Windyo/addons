"""One dry-run through the CESU declaration form with Playwright.

v0.1 is an explorer: it clicks only the navigation controls needed to reach the form
(login, code check, declare, continue), fills hours and rate, captures, and stops.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page, async_playwright

from config import Options
from logic import (
    find_employee_text,
    find_period_text,
    format_rate_fr,
    nearest_minute_option,
    redact,
    split_hours,
    utc_now,
)

log = logging.getLogger("cesu.run")

BASE_URL = "https://www.cesu.urssaf.fr/info/accueil.html"
STATE_PATH = Path("/data/storage_state.json")
CAPTURE_ROOT = Path("/media/cesu")
VIEWPORT = {"width": 1440, "height": 900}
STEP_TIMEOUT_MS = 30_000

# Runs in the page: every visible input/select/textarea/button with its label and options.
FORM_MAP_JS = r"""
() => {
  const visible = el => {
    const r = el.getClientRects();
    const s = getComputedStyle(el);
    return r.length > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    const out = [];
    if (el.getAttribute('aria-label')) out.push(el.getAttribute('aria-label'));
    const by = el.getAttribute('aria-labelledby');
    if (by) by.split(/\s+/).forEach(i => { const n = document.getElementById(i); if (n) out.push(n.innerText); });
    if (el.labels) Array.from(el.labels).forEach(l => out.push(l.innerText));
    if (!el.labels || !el.labels.length) { const l = el.closest('label'); if (l) out.push(l.innerText); }
    if (el.placeholder) out.push(el.placeholder);
    if (el.tagName === 'BUTTON') out.push(el.innerText);
    if (el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(el.type)) out.push(el.value);
    return clean(out.filter(Boolean).join(' / '));
  };
  return Array.from(document.querySelectorAll('input, select, textarea, button'))
    .filter(visible)
    .map(el => {
      const o = {
        tag: el.tagName.toLowerCase(), id: el.id || null, name: el.getAttribute('name'),
        label: labelOf(el), type: el.type || null,
        disabled: !!el.disabled,
        value: el.type === 'password' ? null : (el.value === undefined ? null : el.value),
      };
      if (el.type === 'checkbox' || el.type === 'radio') o.checked = el.checked;
      if (el.tagName === 'SELECT')
        o.options = Array.from(el.options).map(op => ({value: op.value, label: clean(op.text), selected: op.selected}));
      return o;
    });
}
"""


class StepError(Exception):
    pass


class Capture:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.n = 0
        self.last_png: Path | None = None
        folder.mkdir(parents=True, exist_ok=True)

    async def shot(self, page: Page, step: str) -> Path:
        self.n += 1
        stem = f"{self.n:02d}-{step}"
        png = self.folder / f"{stem}.png"
        await page.screenshot(path=str(png), full_page=True)
        (self.folder / f"{stem}.html").write_text(await page.content(), encoding="utf-8")
        self.last_png = png
        log.info("captured %s", stem)
        return png

    def write_json(self, name: str, data) -> None:
        (self.folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _settle(page: Page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=STEP_TIMEOUT_MS)
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass  # long-polling pages never go idle; the next locator wait is the real check


async def _first_visible(page: Page, probes: dict, timeout: float) -> str | None:
    """Poll named locators; return the key of the first one that is visible."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for key, loc in probes.items():
            try:
                if await loc.first.is_visible():
                    return key
            except Exception:
                pass
        await page.wait_for_timeout(500)
    return None


def _timing(label: str, t0: float) -> None:
    log.info("step %s done in %.1f s", label, time.monotonic() - t0)


async def run_dry(opt: Options, req: dict, state: dict, get_otp) -> dict:
    """Execute the explorer.

    `get_otp(since)` is an async callable returning the 6-digit code. `state` is updated in place
    (step, period_seen, ...) so the caller can report the last known step even if cancelled."""
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cap = Capture(CAPTURE_ROOT / run_ts)
    state.update(step="start", period_seen="", employee_seen="", capture=cap)
    secrets = opt.secrets
    log.info("run %s: month=%s hours=%s rate=%s (dry run)", req["request_id"], req["month"], req["hours"], req["rate"])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        page: Page | None = None
        try:
            ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36"
            ctx_args = dict(
                locale="fr-FR", timezone_id="Europe/Paris", viewport=VIEWPORT, user_agent=ua,
                extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
            )
            if STATE_PATH.exists():
                ctx_args["storage_state"] = str(STATE_PATH)
                log.info("reusing stored browser session")
            context = await browser.new_context(**ctx_args)
            context.set_default_timeout(STEP_TIMEOUT_MS)
            page = await context.new_page()

            # ---- home / login ----
            state["step"] = "home"
            t0 = time.monotonic()
            await page.goto(BASE_URL)
            await _settle(page)
            await cap.shot(page, "home")
            _timing("home", t0)

            state["step"] = "login"
            t0 = time.monotonic()
            declare_link = page.get_by_role("link", name="Déclarer")
            login_link = page.get_by_role("link", name="Se connecter")
            seen = await _first_visible(page, {"in": declare_link, "out": login_link}, 20)
            if seen == "in":
                log.info("already logged in (stored session)")
            elif seen == "out":
                await login_link.first.click()
                await _settle(page)
                await cap.shot(page, "login-form")
                await page.get_by_role("textbox", name="Identifiant").fill(opt.cesu_login)
                await page.get_by_role("textbox", name="Mot de passe").fill(opt.cesu_password)
                since = utc_now()  # taken just before pressing Enter on the password
                await page.get_by_role("textbox", name="Mot de passe").press("Enter")
                await _settle(page)

                otp_box = page.get_by_role("textbox", name="______")
                outcome = await _first_visible(page, {"in": declare_link, "otp": otp_box}, 45)
                if outcome is None:
                    alerts = page.get_by_role("alert")
                    txt = ""
                    if await alerts.count():
                        txt = redact(" ".join((await alerts.first.inner_text()).split()), secrets)[:200]
                    await cap.shot(page, "login-no-progress")
                    raise StepError("neither the code page nor the logged-in page appeared after login"
                                    + (f" (page alert: {txt})" if txt else ""))
                if outcome == "otp":
                    state["step"] = "otp"
                    await cap.shot(page, "otp-page")
                    cap.write_json("form_map_otp.json", await page.evaluate(FORM_MAP_JS))
                    code = await get_otp(since)
                    secrets = secrets + [code]
                    await otp_box.first.fill(code)
                    remember = page.get_by_role("checkbox", name=re.compile("m[ée]moris|souvien|appareil|navigateur", re.I))
                    if await remember.count() == 1 and await remember.is_visible():
                        await remember.check()
                        log.info("ticked the 'remember this device' checkbox")
                    await page.get_by_role("button", name="Vérifier").click()
                    await _settle(page)
                    if await _first_visible(page, {"in": declare_link}, 45) != "in":
                        await cap.shot(page, "otp-failed")
                        if await otp_box.first.is_visible():
                            state["step"] = "otp_rejected"
                            raise StepError("otp_rejected: the site did not accept the code")
                        raise StepError("code entered but the logged-in page did not appear")
            else:
                raise StepError("home page shows neither 'Se connecter' nor 'Déclarer'")
            _timing("login", t0)
            await cap.shot(page, "logged-in")
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(STATE_PATH))
            os.chmod(STATE_PATH, 0o600)

            # ---- open the form ----
            state["step"] = "declare"
            t0 = time.monotonic()
            await declare_link.first.click()
            await _settle(page)
            await cap.shot(page, "declare")
            cap.write_json("form_map_declare.json", await page.evaluate(FORM_MAP_JS))

            state["step"] = "continue"
            await page.get_by_role("button", name="Continuer").first.click()
            await _settle(page)
            state["step"] = "form"
            await page.locator("#nbHeure").wait_for(state="visible")
            await cap.shot(page, "form")
            _timing("open form", t0)

            body = await page.locator("body").inner_text()
            state["period_seen"] = find_period_text(body)
            state["employee_seen"] = find_employee_text(body)
            log.info("period seen: %s", state["period_seen"] or "(none)")
            log.info("employee seen: %s", state["employee_seen"] or "(none)")
            cap.write_json("form_map.json", {
                "url": page.url,
                "title": await page.title(),
                "requested_month": req["month"],
                "period_seen": state["period_seen"],
                "employee_seen": state["employee_seen"],
                "elements": await page.evaluate(FORM_MAP_JS),
            })

            # ---- fill (never sent) ----
            state["step"] = "fill"
            t0 = time.monotonic()
            whole, minutes = split_hours(req["hours"])
            hours_sel = page.locator("#nbHeure")
            hour_values = await hours_sel.evaluate("el => Array.from(el.options).map(o => o.value)")
            if str(whole) not in hour_values:
                raise StepError(f"no option '{whole}' in #nbHeure (options: {hour_values[:30]})")
            await hours_sel.select_option(str(whole))
            log.info("hours set to %d", whole)

            minute_sel = page.locator('select[id*="minute" i], select[name*="minute" i]')
            if await minute_sel.count() > 0:
                opts = await minute_sel.first.evaluate(
                    "el => Array.from(el.options).map(o => [o.value, (o.text || '').trim()])")
                chosen = nearest_minute_option([(v, t) for v, t in opts], minutes)
                if chosen is None:
                    log.warning("minutes select has no numeric option; left untouched")
                else:
                    await minute_sel.first.select_option(chosen)
                    log.info("minutes wanted %.2f, chose option %r of %s", minutes, chosen, [o[0] for o in opts])
            elif minutes:
                log.warning("hours has %.2f remaining minutes but no minutes select was found", minutes)
            else:
                log.info("no minutes select found (none needed)")

            rate_txt = format_rate_fr(req["rate"])
            await page.get_by_role("textbox", name="Salaire horaire net").fill(rate_txt)
            log.info("rate set to %s", rate_txt)
            await cap.shot(page, "form-filled")
            cap.write_json("form_map_filled.json", {"elements": await page.evaluate(FORM_MAP_JS)})
            _timing("fill", t0)

            # ---- stop: nothing is sent ----
            state["step"] = "done"
            try:
                await context.storage_state(path=str(STATE_PATH))
                logout = page.get_by_role("link", name="Se déconnecter")
                if await logout.count() == 1 and await logout.is_visible():
                    await logout.click()
                    await _settle(page)
                    log.info("logged out")
                else:
                    log.info("no unambiguous logout link; closing the browser")
            except Exception as exc:
                log.warning("logout skipped: %s", type(exc).__name__)
            return {"status": "dry_run_ok", "step": "done",
                    "message": "form filled and captured; nothing sent"}
        except Exception as exc:
            step = state["step"]
            msg = redact(f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}", secrets)[:300]
            log.error("failed at step %s: %s", step, msg)
            if page is not None:
                try:
                    await cap.shot(page, "error")
                except Exception as shot_exc:
                    log.error("could not capture the error page: %s", type(shot_exc).__name__)
            return {"status": "error", "step": step, "message": msg}
        finally:
            await browser.close()
