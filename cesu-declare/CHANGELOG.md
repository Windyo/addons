# Changelog

## 0.2.0

- `hours` must now be a whole number, 1 to 200 (the site has no minutes select). The minutes logic is removed.
- New option `employee_name` (default `ALEXANDRA`): the selected "Salarié" must contain it, else the run
  stops with step `employee`.
- The period is now selected ("Période d'emploi", e.g. `2026-11` -> "Novembre 2026") before hours and
  rate, and verified after the page settles (step `period`).
- After filling, the "Total net déclaré" must equal hours x rate within 0.01 (step `total`).
- Clicks "Étape suivante" and stops on the recap step, capturing it (`recap` screenshot, html, form map).
- The "Continuer" modal is now optional.
- Result event adds `activity_seen`, `total_seen`, `recap_step`, `recap_buttons`, `recap_text`; `period_seen`
  and `employee_seen` are now the selected option texts; `screenshot` is the recap page.
- Still no code path that saves or sends a declaration (guarded by tests).

## 0.1.0

- First release: explorer / dry run only. Logs in, handles the optional email code, opens the
  declaration form, fills hours and hourly rate, captures every page, then stops.
- The email code is asked for over stdin by default (`cesu_otp_needed` event, then `{request_id, otp}`);
  IMAP is optional.
- There is no code path that submits a declaration.
