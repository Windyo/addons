# Changelog

## 0.1.0

- First release: explorer / dry run only. Logs in, handles the optional email code, opens the
  declaration form, fills hours and hourly rate, captures every page, then stops.
- The email code is asked for over stdin by default (`cesu_otp_needed` event, then `{request_id, otp}`);
  IMAP is optional.
- There is no code path that submits a declaration.
