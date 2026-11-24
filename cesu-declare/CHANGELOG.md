# Changelog

## 0.3.2

- After every run the last screenshot is copied to `/media/cesu/latest.png` (atomic replace), for a
  dashboard picture card. Media-browser deep links can't be opened from Android notifications
  (the companion app decodes `%2F` in `clickAction`), so the notification opens a dashboard instead.

## 0.3.1

- Fix: the public home page can show the logged-in header (name, *Déconnexion*, *Tableau de bord*) with
  neither *Se connecter* nor *Déclarer*; the first real run stopped there at step `login` (nothing sent).
  *Tableau de bord* now counts as logged in and leads to the dashboard.

## 0.3.0

- **Real declarations.** A request with `dry_run: false` ticks the validation checkbox and clicks
  *Enregistrer ma déclaration* once, then waits for the step *Envoyée*. Result `status: declared`.
- Before opening the form, the dashboard box *Mes dernières déclarations* is read; a month already listed
  for the employee stops the run with `status: already_declared`. A real run refuses if the box is missing.
- The recap is checked: hours, hourly rate and total must equal the request; exactly one validation checkbox
  and one enabled submit button must exist (dry runs check this too).
- Ledger `/data/submitted.json`: the month is recorded before the click; a real run for a recorded month is
  refused (`already_submitted`). An unreadable ledger refuses every real run.
- Result event adds `history_seen`, `recap_values` (hours, rate, total, net to pay, contributions, payment
  date) and `confirm_text`.

## 0.2.2

- Fix: with a stored session still valid, "Se connecter" on the public home page leads straight to the
  dashboard, and the run timed out waiting for the login form. It now accepts either the dashboard
  (skips the login) or the form.

## 0.2.1

- Fix: "Nature d'activité principale" was not found because its label holds a help icon, which stopped
  the run right after the period. Labels are now matched on their own text, so child icons don't hide them,
  and a label followed by a second line ("Total net déclaré (10 % congés payés inclus)") matches too.
- The activity is report-only: if it can't be read, the run logs a warning and continues. Its step is now
  `activity` (it was reported as `period`).
- The hourly rate field is left with Tab after typing, in case the site computes the total on blur.

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
