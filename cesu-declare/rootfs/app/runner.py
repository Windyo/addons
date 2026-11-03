"""One dry-run through the CESU declaration form with Playwright.

v0.2 is an explorer: it clicks only the navigation controls needed to reach the form and the
recap step (login, code check, declare, continue, next step), selects the period, fills hours and
rate, checks the computed total, captures the recap page, and stops. Nothing is ever sent.
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
    expected_total,
    format_rate_fr,
    parse_fr_number,
    period_label,
    redact,
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

# Tags the control that belongs to a visible label text (data-cesu-field) so Playwright can use it.
# Resolution: <label for>/wrapping label, aria-label, else the first matching control after the text.
TAG_FIELD_JS = r"""
([name, label, selector]) => {
  const norm = t => (t || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[\u2019\u2018]/g, "'").replace(/[*:]/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(label);
  const visible = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  document.querySelectorAll('[data-cesu-field="' + name + '"]').forEach(e => e.removeAttribute('data-cesu-field'));
  const matches = el => el.matches(selector);
  let found = null;
  const done = el => { if (el && visible(el)) { el.setAttribute('data-cesu-field', name); found = el; } return !!found; };
  for (const l of document.querySelectorAll('label')) {
    if (!norm(l.innerText).startsWith(want)) continue;
    if (l.control && matches(l.control) && done(l.control)) return true;
    const inner = Array.from(l.querySelectorAll(selector)).find(visible);
    if (done(inner)) return true;
  }
  for (const el of document.querySelectorAll(selector)) {
    if (norm(el.getAttribute('aria-label')).startsWith(want) && done(el)) return true;
  }
  const texts = Array.from(document.querySelectorAll('label, legend, span, div, p, th, td, dt, strong, b'))
    .filter(e => e.children.length === 0 && norm(e.innerText) === want);
  const all = Array.from(document.querySelectorAll(selector)).filter(visible);
  for (const t of texts) {
    const after = all.find(e => (t.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING) && !t.contains(e));
    if (done(after)) return true;
  }
  return false;
}
"""

# Reads the tagged control: selected option text for a select, else its value / text.
READ_FIELD_JS = r"""
el => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  if (el.tagName === 'SELECT') {
    const o = el.options[el.selectedIndex];
    return {text: o ? clean(o.text) : '', value: el.value};
  }
  const v = ('value' in el) ? el.value : el.innerText;
  return {text: clean(v), value: clean(v)};
}
"""

# Recap page: stepper, visible buttons/links of the main area, collapsed main text.
RECAP_JS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const visible = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const main = document.querySelector('main, [role="main"], #main, #contenu, #content') || document.body;
  const inChrome = el => !!el.closest('header, footer, nav, [role="banner"], [role="contentinfo"], [role="navigation"]');
  const step = document.querySelector('[class*="stepper" i], [class*="steps" i], [role="progressbar"]');
  const active = Array.from(document.querySelectorAll(
    '[aria-current], [class*="step" i][class*="active" i], [class*="step" i][class*="current" i]'))
    .filter(visible).map(e => clean(e.innerText)).filter(Boolean);
  const labels = [];
  const isBtnInput = el => el.tagName === 'INPUT' && (['button', 'image'].includes(el.type) || el.type.startsWith('sub'));
  main.querySelectorAll('button, a, input, [role="button"]').forEach(el => {
    if (!visible(el) || inChrome(el) || (el.tagName === 'INPUT' && !isBtnInput(el))) return;
    const t = clean(el.tagName === 'INPUT' ? el.value : (el.innerText || el.getAttribute('aria-label')));
    if (t && !labels.includes(t)) labels.push(t);
  });
  return {
    stepper: step ? clean(step.innerText) : '',
    active: Array.from(new Set(active)).join(' | '),
    buttons: labels,
    text: clean(main.innerText).slice(0, 2000),
  };
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


async def _field(page: Page, name: str, label: str, selector: str = "select"):
    """Locator of the control tied to `label`, or raise StepError(label)."""
    ok = await page.evaluate(TAG_FIELD_JS, [name, label, selector])
    if not ok:
        raise StepError(f"field '{label}' not found on the form")
    return page.locator(f'[data-cesu-field="{name}"]')


async def _read(page: Page, name: str, label: str, selector: str = "select") -> dict:
    return await (await _field(page, name, label, selector)).evaluate(READ_FIELD_JS)


async def run_dry(opt: Options, req: dict, state: dict, get_otp) -> dict:
    """Execute the explorer.

    `get_otp(since)` is an async callable returning the 6-digit code. `state` is updated in place
    (step, period_seen, ...) so the caller can report the last known step even if cancelled."""
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cap = Capture(CAPTURE_ROOT / run_ts)
    state.update(step="start", period_seen="", employee_seen="", activity_seen="", total_seen=None,
                 recap_step="", recap_buttons=[], recap_text="", capture=cap)
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
            modal_btn = page.get_by_role("button", name="Continuer")
            form_ready = page.locator("#nbHeure")
            if await _first_visible(page, {"modal": modal_btn, "form": form_ready}, 15) == "modal":
                await modal_btn.first.click()  # informational modal; optional
                await _settle(page)
            else:
                log.info("no 'Continuer' modal; the form is already shown")
            state["step"] = "form"
            await form_ready.wait_for(state="visible")
            await cap.shot(page, "form")
            _timing("open form", t0)

            # ---- employee, period, activity ----
            state["step"] = "employee"
            emp = await _read(page, "employee", "Salarié")
            state["employee_seen"] = emp["text"]
            log.info("employee seen: %s", emp["text"] or "(none)")
            if opt.employee_name.lower() not in emp["text"].lower():
                raise StepError(f"selected employee '{emp['text']}' does not contain '{opt.employee_name}'")

            state["step"] = "period"
            label = period_label(req["month"])
            period_sel = await _field(page, "period", "Période d'emploi")
            options = await period_sel.evaluate(
                "el => Array.from(el.options).map(o => [o.value, (o.text || '').replace(/\\s+/g, ' ').trim()])")
            match = [v for v, t in options if t.lower() == label.lower()]
            state["period_seen"] = (await _read(page, "period", "Période d'emploi"))["text"]
            if not match:
                raise StepError(f"period '{label}' not offered (options: {[t for _, t in options][:12]})")
            if state["period_seen"].lower() != label.lower():
                await period_sel.select_option(match[0])
                await _settle(page)
                await page.wait_for_timeout(1500)  # the site may redraw parts of the form
            state["period_seen"] = (await _read(page, "period", "Période d'emploi"))["text"]
            log.info("period selected: %s", state["period_seen"])
            if state["period_seen"].lower() != label.lower():
                raise StepError(f"period shows '{state['period_seen']}' after selecting '{label}'")

            activity = await _read(page, "activity", "Nature d'activité principale")
            state["activity_seen"] = activity["text"]
            log.info("activity: %s", activity["text"])
            cap.write_json("form_map.json", {
                "url": page.url,
                "title": await page.title(),
                "requested_month": req["month"],
                "period_seen": state["period_seen"],
                "employee_seen": state["employee_seen"],
                "activity_seen": state["activity_seen"],
                "elements": await page.evaluate(FORM_MAP_JS),
            })

            # ---- fill (never sent) ----
            state["step"] = "fill"
            t0 = time.monotonic()
            hours = req["hours"]
            hours_sel = page.locator("#nbHeure")
            hour_values = await hours_sel.evaluate("el => Array.from(el.options).map(o => o.value)")
            if str(hours) not in hour_values:
                raise StepError(f"no option '{hours}' in #nbHeure (options: {hour_values[:30]})")
            await hours_sel.select_option(str(hours))
            log.info("hours set to %d", hours)

            rate_txt = format_rate_fr(req["rate"])
            await page.get_by_role("textbox", name="Salaire horaire net").fill(rate_txt)
            log.info("rate set to %s", rate_txt)

            # ---- total check: the site computes it; it must equal hours * rate ----
            state["step"] = "total"
            want = expected_total(hours, req["rate"])
            deadline = time.monotonic() + 5
            total = None
            while True:
                try:
                    total_txt = (await _read(page, "total", "Total net déclaré", "input, output, textarea"))["text"]
                except StepError:  # shown as plain text rather than a field
                    body = " ".join((await page.locator("body").inner_text()).split())
                    m = re.search(r"Total net déclaré\s*:?\s*([\d\s.,]+\d)", body)
                    total_txt = m.group(1) if m else ""
                total = parse_fr_number(total_txt)
                if (total is not None and abs(total - want) <= 0.01) or time.monotonic() > deadline:
                    break
                await page.wait_for_timeout(500)
            state["total_seen"] = total
            await cap.shot(page, "form-filled")
            cap.write_json("form_map_filled.json", {"elements": await page.evaluate(FORM_MAP_JS)})
            if total is None or abs(total - want) > 0.01:
                raise StepError(f"total on the page is {total}, expected {want:.2f} ({hours} h x {rate_txt})")
            log.info("total %.2f matches %d h x %s", total, hours, rate_txt)
            _timing("fill", t0)

            # ---- next step: the recap page (the only forward click; nothing is sent) ----
            state["step"] = "recap"
            t0 = time.monotonic()
            await page.get_by_role("button", name="Étape suivante", exact=True).click()
            await _settle(page)
            await page.wait_for_timeout(1500)
            await cap.shot(page, "recap")
            cap.write_json("form_map_recap.json", {"url": page.url, "elements": await page.evaluate(FORM_MAP_JS)})
            recap = await page.evaluate(RECAP_JS)
            state["recap_step"] = redact(recap["active"] or recap["stepper"], secrets)[:300]
            state["recap_buttons"] = [redact(b, secrets) for b in recap["buttons"]][:40]
            state["recap_text"] = redact(recap["text"], secrets)[:2000]
            log.info("recap step: %s; buttons: %s", state["recap_step"], state["recap_buttons"])
            _timing("recap", t0)

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
                    "message": "recap page reached and captured; nothing sent"}
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
