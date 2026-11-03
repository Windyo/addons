import pytest

from logic import (
    RequestError,
    extract_otp,
    find_employee_text,
    find_period_text,
    format_rate_fr,
    media_url_path,
    nearest_minute_option,
    redact,
    split_hours,
    validate_request,
)


def req(**kw):
    base = {"request_id": "abc", "month": "2026-10", "hours": 12.5, "rate": 16, "dry_run": True}
    base.update(kw)
    return base


# ---- request validation ----
def test_valid_request_normalised():
    r = validate_request(req(hours=12, rate=16.5))
    assert r == {"request_id": "abc", "month": "2026-10", "hours": 12.0, "rate": 16.5, "dry_run": True}


def test_dry_run_defaults_true_and_is_echoed():
    r = req()
    del r["dry_run"]
    assert validate_request(r)["dry_run"] is True
    assert validate_request(req(dry_run=False))["dry_run"] is False


@pytest.mark.parametrize("bad", [
    {"month": "2026-13"}, {"month": "2026-00"}, {"month": "26-10"}, {"month": "2026-1"},
    {"month": "2026-10-01"}, {"month": 202610}, {"month": None},
    {"hours": 0}, {"hours": -1}, {"hours": 200.01}, {"hours": "3"}, {"hours": True},
    {"hours": float("nan")}, {"hours": float("inf")},
    {"rate": 9.99}, {"rate": 40.01}, {"rate": "16"}, {"rate": False}, {"rate": None},
    {"request_id": ""}, {"request_id": "  "}, {"request_id": 5}, {"request_id": "x" * 101},
    {"dry_run": "yes"},
])
def test_invalid_requests(bad):
    with pytest.raises(RequestError):
        validate_request(req(**bad))


@pytest.mark.parametrize("obj", [[], "x", None, 3])
def test_non_object_rejected(obj):
    with pytest.raises(RequestError):
        validate_request(obj)


@pytest.mark.parametrize("ok", [{"hours": 200}, {"hours": 0.25}, {"rate": 10}, {"rate": 40}])
def test_boundaries_accepted(ok):
    validate_request(req(**ok))


def test_missing_fields():
    with pytest.raises(RequestError):
        validate_request({"request_id": "a"})


# ---- OTP extraction ----
def test_otp_plain():
    assert extract_otp("Votre code est 123456. Il expire dans 10 minutes.") == "123456"


def test_otp_html_only():
    html = "<html><style>.a{color:#111111}</style><body><p>Code&nbsp;:</p><b>654<i>321</i></b></body></html>"
    # tags stripped without a separator inside a word are NOT merged by the extractor: spaces are added
    assert extract_otp(None, "<p>Votre code : <strong>654321</strong></p>") == "654321"
    assert extract_otp("", html) is None  # '654 321' is not a standalone 6-digit number


def test_otp_multiple_numbers_takes_first_standalone_six_digits():
    text = "Ref 1234567 tel 0612345678 code 482913 puis 111111"
    assert extract_otp(text) == "482913"


def test_otp_none():
    assert extract_otp("Bonjour, rien ici 12345 et 1234567") is None
    assert extract_otp(None, None) is None
    assert extract_otp("", "<p>no code</p>") is None


def test_otp_plain_preferred_over_html():
    assert extract_otp("code 111222", "<p>999888</p>") == "111222"


def test_otp_html_ignores_script_and_style():
    html = "<style>.x{width:100000}</style><script>var a=777777;</script><p>code 246810</p>"
    assert extract_otp(None, html) == "246810"


def test_otp_falls_back_to_html_when_plain_has_none():
    assert extract_otp("Voir la version HTML", "<div>Code : 135790</div>") == "135790"


# ---- minutes nearest ----
OPTS = [("", "--"), ("0", "00"), ("15", "15"), ("30", "30"), ("45", "45")]


@pytest.mark.parametrize("minutes,expected", [
    (0, "0"), (6, "0"), (7.5, "0"),  # tie 7.5 -> lower
    (8, "15"), (22, "15"), (23, "30"), (30, "30"), (40, "45"), (59.4, "45"),
])
def test_nearest_minute(minutes, expected):
    assert nearest_minute_option(OPTS, minutes) == expected


def test_nearest_minute_reads_label_when_value_not_numeric():
    assert nearest_minute_option([("a", "10 min"), ("b", "40 min")], 35) == "b"


def test_nearest_minute_no_numeric_options():
    assert nearest_minute_option([("", "--")], 10) is None
    assert nearest_minute_option([], 10) is None


def test_split_hours():
    assert split_hours(3) == (3, 0.0)
    assert split_hours(2.75) == (2, 45.0)
    assert split_hours(12.5) == (12, 30.0)
    assert split_hours(0.25) == (0, 15.0)
    assert split_hours(7.1 + 0.2) == (7, 18.0)  # float noise below 1e-9 does not drop an hour
    assert split_hours(3.0000000001)[0] == 3


# ---- French rate ----
@pytest.mark.parametrize("rate,expected", [
    (16, "16"), (16.0, "16"), (16.5, "16,50"), (16.25, "16,25"), (12.345, "12,35"),
    (10, "10"), (40, "40"), (16.999, "17"), (15.1, "15,10"),
])
def test_format_rate_fr(rate, expected):
    assert format_rate_fr(rate) == expected


# ---- misc ----
def test_redact():
    assert redact("login=bob pw=hunter2", ["hunter2", "", "bob"]) == "login=*** pw=***"


def test_media_path():
    assert media_url_path("/media/cesu/20261103T101500Z/05-form.png") == "/media/local/cesu/20261103T101500Z/05-form.png"


def test_period_and_employee_text():
    text = "Déclarer\nPériode : octobre 2026\nSalarié : Mme Dupont\nBouton\n"
    assert "octobre 2026" in find_period_text(text)
    assert "Dupont" in find_employee_text(text)
    assert find_period_text("rien") == ""
