import asyncio

import pytest

from dispatch import OtpBroker, classify_line
from logic import OtpFormatError, parse_otp, validate_request


# ---- otp value validation ----
@pytest.mark.parametrize("value,expected", [("123456", "123456"), (123456, "123456"), ("012345", "012345"), (" 654321 ", "654321")])
def test_parse_otp_ok(value, expected):
    assert parse_otp(value) == expected


@pytest.mark.parametrize("value", ["12345", "1234567", "12a456", "", None, True, 12345, 1234567, 123456.0, ["123456"], "123 456"])
def test_parse_otp_bad(value):
    with pytest.raises(OtpFormatError):
        parse_otp(value)


def test_month_is_coerced_with_str():
    class M:  # a non-string that renders as a valid month
        def __str__(self):
            return "2026-10"
    assert validate_request({"request_id": "a", "month": M(), "hours": 3, "rate": 16})["month"] == "2026-10"


# ---- classification ----
def test_classify():
    assert classify_line("   ")[0] == "empty"
    assert classify_line("not json")[0] == "junk"
    assert classify_line('{"request_id": "a", "otp": "123456"}')[0] == "otp"
    assert classify_line('{"request_id": "a", "otp": 123456}')[0] == "otp"
    assert classify_line('{"request_id": "a", "month": "2026-10", "hours": 3, "rate": 16}')[0] == "request"
    assert classify_line("[1]")[0] == "request"  # rejected later by validate_request


# ---- routing ----
def run(coro):
    return asyncio.run(coro)


def test_deliver_to_waiting_run():
    async def go():
        b = OtpBroker()
        fut = b.expect("r1")
        assert b.deliver({"request_id": "r1", "otp": 123456}) == "delivered"
        return await fut
    assert run(go()) == "123456"


def test_deliver_mismatched_id_is_ignored_and_run_keeps_waiting():
    async def go():
        b = OtpBroker()
        fut = b.expect("r1")
        assert b.deliver({"request_id": "other", "otp": "123456"}) == "ignored"
        assert not fut.done()
        assert b.deliver({"otp": "123456"}) == "ignored"
        assert not fut.done()
    run(go())


def test_deliver_without_waiter_is_ignored():
    async def go():
        assert OtpBroker().deliver({"request_id": "r1", "otp": "123456"}) == "ignored"
    run(go())


def test_bad_format_fails_the_waiting_run():
    async def go():
        b = OtpBroker()
        fut = b.expect("r1")
        assert b.deliver({"request_id": "r1", "otp": "12345"}) == "bad_format"
        with pytest.raises(OtpFormatError):
            await fut
    run(go())


def test_release_stops_routing():
    async def go():
        b = OtpBroker()
        b.expect("r1")
        b.release("r1")
        assert b.deliver({"request_id": "r1", "otp": "123456"}) == "ignored"
    run(go())


def test_split_json_objects_without_newlines():
    from main import split_json_objects
    texts, rest = split_json_objects('{"a": 1}{"b": "x}"}  {"c"')
    assert texts == ['{"a": 1}', '{"b": "x}"}'] and rest == '{"c"'
    texts, rest = split_json_objects(rest + ': 2}\n')
    assert texts == ['{"c": 2}'] and rest == ""


def test_split_json_objects_junk():
    from main import split_json_objects
    texts, rest = split_json_objects('hello {"a": 1}')
    assert texts == ['hello ', '{"a": 1}'] and rest == ""


def test_split_json_objects_partial_nested_and_string():
    from main import split_json_objects
    assert split_json_objects('{"a": {"b"') == ([], '{"a": {"b"')
    assert split_json_objects('{"otp": "12') == ([], '{"otp": "12')
    assert split_json_objects('{bad} {"a": 1}') == (['{bad} ', '{"a": 1}'], "")
