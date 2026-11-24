# CESU Declare

Automates the monthly CESU (URSSAF, France) salary declaration of a household employee on
<https://www.cesu.urssaf.fr> with a headless Chromium (Playwright).

## What a run does

For each request it logs in (stored session, else password and the one-time email code), reads the
dashboard box *Mes dernières déclarations*, opens the declaration form, checks the employee, **selects the
period**, fills the whole hours and the net hourly rate, checks the computed total, moves to the **recap
step** with *Étape suivante* and checks the recap.

- **Dry run** (`dry_run` true or absent, the default): stops on the recap. Nothing is sent.
- **Real declaration** (`dry_run: false`, exactly the JSON boolean): ticks *Je valide ces informations et
  autorise le Cesu à présenter un ordre de prélèvement…*, writes the month to the ledger, clicks
  **Enregistrer ma déclaration** once and waits up to 60 s for the step *Envoyée* (or a "a bien été
  enregistrée"-type message). It never clicks it twice and never retries by itself.

Checks, each one stops the run with its own `step` before anything is sent:

- `history`: if *Mes dernières déclarations* lists the month for `employee_name`, the run stops with
  `status: already_declared` (dry runs too). A real run also stops if that box can't be found.
- `employee`: the selected "Salarié" must contain the `employee_name` option (case-insensitive).
- `period`: `month` is mapped to the French label ("2026-11" -> "Novembre 2026") and selected in
  "Période d'emploi" before anything else is filled; the selected text is re-read after the page settles.
- `total`: "Total net déclaré" must equal hours x rate (rate rounded to cents) within 0.01.
- `recap`: the recap's *Heures effectuées*, *Salaire horaire net* and *Total net déclaré* must equal the
  request, and exactly one validation checkbox and one enabled *Enregistrer ma déclaration* button must exist.

### Ledger: never twice

`/data/submitted.json` holds every month the add-on clicked *Enregistrer ma déclaration* for. The month is
written **before** the click, so a crash, a timeout or an unclear answer still counts. A real request for a
month in the ledger is refused (`step: already_submitted`; `status: already_declared` if it was confirmed,
else `invalid`). The only exception is `dialog` (a dialog appeared after the click and nothing more was
clicked). An unreadable ledger refuses every real run. If a run ends unconfirmed, check *Mes déclarations*
on the site and finish by hand if needed.

## Options

| Option | Default | Meaning |
|---|---|---|
| `cesu_login` / `cesu_password` | | Employer account on cesu.urssaf.fr. |
| `imap_host` | empty | **Empty = ask mode** (default, see below). Set it to read the code from a mailbox. |
| `imap_port` | `993` | IMAP port. |
| `imap_security` | `ssl` | `ssl`, `starttls`, or `starttls_insecure` (STARTTLS accepting a self-signed certificate). |
| `imap_user` / `imap_password` | | Mailbox credentials. |
| `imap_folder` | `INBOX` | Folder to search. |
| `otp_sender_filter` | `urssaf` | Only messages whose From contains this (case-insensitive) count. |
| `employee_name` | `ALEXANDRA` | Text the selected "Salarié" must contain (case-insensitive). |
| `log_level` | `info` | `info` or `debug`. Passwords and codes are never logged. |

## One-time code

The session is stored in `/data/storage_state.json` and reused, so the code page is often skipped. When
it appears, the code comes from one of two sources.

### Ask mode (default, `imap_host` empty)

1. The add-on fires the event `cesu_otp_needed` with `{request_id, month, timeout_s: 300}`.
2. Your automation notifies you (phone) and you reply by sending the code on stdin, within 300 s:

```yaml
action: hassio.addon_stdin
data:
  addon: 8634c18d_cesu_declare
  input: '{"request_id": "{{ request_id }}", "otp": "{{ code }}"}'
```

`otp` may be a string or an integer; it must be exactly 6 digits (a code with a leading zero must be
sent as a string). A line with another `request_id` is ignored. A malformed code or a timeout ends the
run with `status: error`, `step: otp`. If the site refuses the code, the result is `status: error`,
`step: otp_rejected`. There is no retry loop.

### IMAP mode (`imap_host` set)

After sending the password the add-on polls the mailbox every 5 s for up to 180 s for a message whose
From contains `otp_sender_filter`, dated after that moment (60 s clock skew allowed). It takes the first
standalone 6-digit number from the text part, else from the HTML with tags stripped, and marks the
message as read.

A practical setup is a filtered forward (From URSSAF) from your main mailbox to a throwaway mailbox that
only this add-on reads.

With **Proton Mail Bridge**, point `imap_host` at the machine running the Bridge, use its IMAP port
(1143 by default), `imap_security: starttls_insecure` (the Bridge certificate is self-signed), and the
bridge-specific username and password shown in the Bridge app, not your Proton login.

## Starting a run

```yaml
action: hassio.addon_stdin
data:
  addon: 8634c18d_cesu_declare
  input: '{"request_id": "2026-10-001", "month": "2026-10", "hours": 12, "rate": 16.5, "dry_run": true}'
```

| Field | Rule |
|---|---|
| `request_id` | non-empty string, up to 100 characters |
| `month` | `YYYY-MM` |
| `hours` | **whole number**, 1 to 200 (the site only has whole hours); goes in `#nbHeure` |
| `rate` | number, `10 <= rate <= 40`; typed in French format (`16` or `16,50`) |
| `dry_run` | JSON boolean, default `true`. Only `false` declares for real; any non-boolean is refused |

One run at a time: a request sent during a run gets `status: busy`.

## Captures

Everything is written to `/media/cesu/<UTC timestamp>/` (Media in Home Assistant, `media-source://media_source/local/cesu/...`):
`NN-<step>.png` (full page) and `NN-<step>.html` for every step, plus `form_map.json` (every visible
input, select, textarea and button with id, name, label, type, value and select options, plus the period,
employee and activity selected), `form_map_declare.json`, `form_map_otp.json`, `form_map_filled.json` and
`form_map_recap.json`. In a dry run the last screenshot, `NN-recap.png`, is the recap page; a real run adds
`NN-sent.png` (or `NN-dialog`, `NN-submit-alert`, `NN-unconfirmed`).
On failure an `NN-error.png/.html` is added. Folders older than 90 days are deleted at start.
After every run the last screenshot is also copied to `/media/cesu/latest.png`
(`media-source://media_source/local/cesu/latest.png`), so a dashboard picture card can show it.

Screenshots show the employer's account pages. Treat the folder as private.

## Events

Result, `cesu_declare_result`:

```json
{"request_id": "2026-10-001", "month": "2026-10", "hours": 12, "rate": 16.5, "dry_run": true,
 "status": "dry_run_ok", "step": "done", "message": "recap page reached and captured; nothing sent",
 "period_seen": "Octobre 2026", "employee_seen": "ALEXANDRA ...",
 "activity_seen": "Entretien de la maison et travaux ménagers", "total_seen": 198.0,
 "recap_step": "2 Enregistrer", "recap_buttons": ["Étape précédente", "..."],
 "recap_text": "main text of the recap page, whitespace collapsed, at most 2000 characters",
 "history_seen": "Octobre 2026 ALEXANDRA GANTIER Septembre 2026 ALEXANDRA GANTIER ...",
 "recap_values": {"hours": 11.0, "rate": 16.0, "total": 176.0, "net_to_pay": 176.0,
                  "contributions": 133.01, "paid_on": "30/11/2026"},
 "confirm_text": "",
 "screenshot": "/media/local/cesu/20261103T101500Z/09-recap.png"}
```

`status` is `dry_run_ok`, `declared`, `already_declared`, `error`, `busy` or `invalid`. `step` names where it
stopped (`home`, `login`, `otp`, `otp_rejected`, `history`, `declare`, `continue`, `form`, `employee`, `period`,
`activity`, `fill`, `total`, `recap`, `submit`, `confirm`, `confirm_dialog`, `already_submitted`, `request`,
`done`). After the click, `error` with step `submit` (the site showed an alert) or `confirm` (no answer
recognised) means **the declaration may have been sent**: check the site.
`history_seen` is the *Mes dernières déclarations* text, `recap_values` the numbers and the salary payment
date read on the recap, `confirm_text` the page (or dialog) text after the click. `period_seen`, `employee_seen` and `activity_seen` are the selected option texts;
`recap_step` is the active stepper text (or the whole stepper), `recap_buttons` the visible buttons and
links of the main content. On failure the fields known so far are still filled.

Code request, `cesu_otp_needed` (ask mode): `{"request_id": "...", "month": "...", "timeout_s": 300}`.
