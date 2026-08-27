"""管理员密码摘要。"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=2, hash_len=32, salt_len=16)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, encoded: str) -> tuple[bool, bool]:
    try:
        valid = _hasher.verify(encoded, password)
        return bool(valid), bool(valid and _hasher.check_needs_rehash(encoded))
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False, False
