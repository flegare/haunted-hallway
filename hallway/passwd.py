"""Password hashing with scrypt (standard library). CLI: python -m hallway.passwd"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import secrets
import sys

DEFAULT_N = 2 ** 15
R, P = 8, 1
MAXMEM = 128 * 1024 * 1024


def hash_password(password: str, n: int = DEFAULT_N) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=R, p=P, maxmem=MAXMEM, dklen=32)
    return f"scrypt${n}${R}${P}${salt.hex()}${digest.hex()}"


def parse(stored: str) -> tuple[int, int, int, bytes, bytes]:
    scheme, n, r, p, salt, digest = stored.split("$")
    if scheme != "scrypt":
        raise ValueError("unknown scheme")
    n, r, p = int(n), int(r), int(p)
    if n < 2 ** 10 or n > 2 ** 20 or n & (n - 1):
        raise ValueError("bad scrypt cost")
    return n, r, p, bytes.fromhex(salt), bytes.fromhex(digest)


def verify_password(password: str, stored: str) -> bool:
    try:
        n, r, p, salt, expected = parse(stored)
    except ValueError:
        return False
    got = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=len(expected))
    return hmac.compare_digest(got, expected)


def main() -> int:
    first = getpass.getpass("Admin password: ")
    if len(first) < 12:
        print("error: use at least 12 characters", file=sys.stderr)
        return 2
    if getpass.getpass("Again: ") != first:
        print("error: passwords differ", file=sys.stderr)
        return 2
    print("ADMIN_PASSWORD_HASH=" + hash_password(first))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
