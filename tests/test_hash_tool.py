"""The operator's hash tool: a real password in, one hash line out, nothing else."""

from __future__ import annotations

import getpass
import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from app import hash_emergency_password as tool
from app.utils.passwords import parse_password_hash, verify_password


def _answers(monkeypatch: pytest.MonkeyPatch, *values: str) -> None:
    given = iter(values)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(given))


def test_a_matching_long_password_prints_the_env_line_and_nothing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answers(monkeypatch, "operator on call tonight", "operator on call tonight")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tool.main()
    line = out.getvalue().strip()
    assert code == 0 and err.getvalue() == ""
    assert line.startswith("EMERGENCY_ADMIN_PASSWORD_HASH=scrypt:")
    encoded = line.split("=", 1)[1]
    assert "operator" not in encoded
    assert verify_password("operator on call tonight", encoded)
    assert parse_password_hash(encoded).log_n >= 14  # the real cost, not the tests' cheap one


@pytest.mark.parametrize("short", ["", "1234", "elevenchars"])
def test_a_short_password_is_refused(monkeypatch: pytest.MonkeyPatch, short: str) -> None:
    _answers(monkeypatch, short, short)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tool.main()
    assert code == 1 and out.getvalue() == ""
    assert str(tool.MIN_PASSWORD_LENGTH) in err.getvalue()
    assert short not in err.getvalue() or short == ""


def test_mismatched_entries_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, "operator on call tonight", "operator on call tonite")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tool.main()
    assert code == 1 and out.getvalue() == "" and "differ" in err.getvalue()
