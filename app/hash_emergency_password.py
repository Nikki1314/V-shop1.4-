"""Operator tool: turn the emergency admin password into the hash ``.env`` needs.

Run on a trusted machine::

    python -m app.hash_emergency_password

It prompts for the password twice without echo, and prints one line —
``EMERGENCY_ADMIN_PASSWORD_HASH=…`` — to paste into ``.env``. The password itself
is never printed, stored or logged; only its scrypt hash leaves this process.

Exit codes: 0 printed a hash; 1 the password was too short or the entries did not match.
Operator-facing text below is INTENTIONALLY NOT LOCALIZED — this is a console
tool for the person deploying the bot, never a customer.
"""

from __future__ import annotations

import getpass
import sys

from app.utils.passwords import hash_password

# The secret is shared by everyone who may break the glass and guessed at
# EMERGENCY_ADMIN_MAX_FAILED_ATTEMPTS per account per window: it has to be long.
MIN_PASSWORD_LENGTH = 12


def main() -> int:
    first = getpass.getpass("Emergency admin password: ")
    second = getpass.getpass("Repeat: ")
    if len(first) < MIN_PASSWORD_LENGTH:
        print(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    if first != second:
        print("The two entries differ.", file=sys.stderr)
        return 1
    print(f"EMERGENCY_ADMIN_PASSWORD_HASH={hash_password(first)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator tool
    raise SystemExit(main())
