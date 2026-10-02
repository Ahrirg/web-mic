from app.server.auth import Authenticator, format_code, generate_pairing_code


def test_code_format():
    code = generate_pairing_code()
    assert len(code) == 6 and code.isdigit()
    assert format_code("842951") == "84-29-51"


def test_correct_and_wrong_code():
    a = Authenticator("123456")
    assert a.check("1.2.3.4", "c1", "123456", "") == (True, "ok")
    assert a.check("1.2.3.4", "c1", "000000", "") == (False, "bad_code")
    assert a.check("1.2.3.4", "c1", "", "") == (False, "bad_code")


def test_token_allows_reconnect_without_code():
    a = Authenticator("123456")
    token = a.issue_token("c1")
    assert a.check("ip", "c1", "", token) == (True, "token")
    # a token is bound to its client id
    assert a.check("ip", "other", "", token)[0] is False


def test_regenerate_invalidates_tokens_and_old_code():
    a = Authenticator("123456")
    token = a.issue_token("c1")
    new = a.regenerate()
    assert a.check("ip", "c1", "", token)[0] is False
    assert a.check("ip", "c1", "123456", "")[0] is (new == "123456")
    assert a.check("ip", "c1", new, "")[0] is True


def test_disabled_accepts_everyone():
    a = Authenticator("123456", enabled=False)
    assert a.check("ip", "c", "", "") == (True, "auth_disabled")


def test_rate_limit_after_many_failures():
    a = Authenticator("123456")
    for _ in range(Authenticator.MAX_FAILURES):
        a.check("9.9.9.9", "c", "111111", "")
    assert a.check("9.9.9.9", "c", "123456", "") == (False, "rate_limited")
    # other clients are not affected
    assert a.check("8.8.8.8", "c", "123456", "")[0] is True
