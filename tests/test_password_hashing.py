"""The emergency password hash: scrypt in a PHC string, verified in constant time."""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.utils.passwords import (
    MAX_LOG_N,
    MEMORY_LIMIT,
    MIN_LOG_N,
    PasswordHash,
    hash_password,
    parse_password_hash,
    verify_password,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = ROOT / "app" / "utils" / "passwords.py"

# Cheap parameters keep the suite fast; the format and the checks are the same.
FAST = {"log_n": MIN_LOG_N}


def test_a_password_verifies_against_its_own_hash_and_no_other() -> None:
    encoded = hash_password("correct horse battery staple", **FAST)
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("correct horse battery stable", encoded)
    assert not verify_password("", encoded)
    assert not verify_password("correct horse battery staple ", encoded)


def test_the_hash_is_a_phc_string_carrying_its_parameters() -> None:
    encoded = hash_password("secret", log_n=11, r=4, p=2)
    assert encoded.startswith("scrypt:ln=11,r=4,p=2:")
    parsed = parse_password_hash(encoded)
    assert (parsed.log_n, parsed.r, parsed.p) == (11, 4, 2)
    assert len(parsed.salt) == 16 and len(parsed.digest) == 32
    assert parsed.encode() == encoded
    assert "secret" not in encoded


def test_two_hashes_of_one_password_differ_by_their_salt() -> None:
    first, second = (hash_password("same", **FAST) for _ in range(2))
    assert first != second
    assert parse_password_hash(first).salt != parse_password_hash(second).salt
    assert verify_password("same", first) and verify_password("same", second)


def test_unicode_is_normalised_so_composed_and_decomposed_input_agree() -> None:
    composed, decomposed = "café", "café"
    encoded = hash_password(composed, **FAST)
    assert verify_password(decomposed, encoded)


def test_an_empty_password_cannot_be_hashed() -> None:
    with pytest.raises(ValueError):
        hash_password("")
    with pytest.raises(ValueError):
        hash_password(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "plaintext",
        "$2b$12$abcdefghijklmnopqrstuv",  # bcrypt, not ours
        # the PHC `$` form: Compose would expand `$scrypt` and `$ln` inside .env
        "$scrypt$ln=14,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt:ln=14,r=8,p=1:salt",  # digest missing
        "scrypt:ln=14,r=8,p=1:!!!:###",  # not base64
        "scrypt:ln=9,r=8,p=1:AAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt:ln=18,r=8,p=1:AAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt:ln=17,r=32,p=1:AAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt:ln=14,r=8,p=1:AAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # short salt
    ],
    ids=[
        "empty",
        "plaintext",
        "bcrypt",
        "dollar separators",
        "truncated",
        "not base64",
        "ln too small",
        "ln too large",
        "too much memory",
        "short salt",
    ],
)
def test_a_malformed_or_hostile_hash_is_refused_and_never_verifies(encoded: str) -> None:
    with pytest.raises(ValueError) as refused:
        parse_password_hash(encoded)
    assert encoded == "" or encoded not in str(refused.value)  # the message never echoes it
    assert verify_password("anything", encoded) is False


def test_the_memory_bound_holds_for_every_accepted_parameter_set() -> None:
    largest = PasswordHash(
        log_n=MAX_LOG_N, r=1, p=1, salt=bytes(16), digest=bytes(32)
    )  # the most iterations allowed
    assert largest.memory <= MEMORY_LIMIT
    with pytest.raises(ValueError):
        PasswordHash(log_n=MAX_LOG_N, r=8, p=1, salt=bytes(16), digest=bytes(32))


def test_the_repr_shows_parameters_only() -> None:
    parsed = parse_password_hash(hash_password("secret", **FAST))
    text = repr(parsed)
    assert "ln=" in text
    assert parsed.encode().split(":")[-1] not in text
    assert parsed.encode().split(":")[-2] not in text


def test_verification_compares_digests_in_constant_time() -> None:
    """The comparison is hmac.compare_digest, and nothing else compares a digest."""
    module = ast.parse(MODULE.read_text(encoding="utf-8"))
    verify = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "verify_password"
    )
    calls = [
        node.func.attr
        for node in ast.walk(verify)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "compare_digest" in calls
    compares = [node for node in ast.walk(verify) if isinstance(node, ast.Compare)]
    assert compares == [], "a digest must never be compared with =="


def test_the_module_never_logs() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert "logging" not in source and "print(" not in source and "getpass" not in source
