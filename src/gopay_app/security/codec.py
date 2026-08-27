"""数据库敏感字段 AES-GCM 加解密。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from contextlib import suppress
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRET_PREFIX = "enc:v1:"


class SecretCodecError(ValueError):
    """敏感字段密文或密钥不正确。"""


def _write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SecretCodec:
    """使用独立 256 位密钥保护数据库中的可逆敏感字段。"""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise SecretCodecError("数据库密钥长度必须是 32 字节")
        self._key = key
        self._aead = AESGCM(key)

    @classmethod
    def load(cls, key_path: Path, *, create: bool = True) -> SecretCodec:
        key_path = key_path.expanduser().resolve()
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            os.chmod(key_path.parent, 0o700)

        if not key_path.exists():
            if not create:
                raise SecretCodecError(f"数据库密钥不存在：{key_path}")
            key = os.urandom(32)
            try:
                _write_private_file(key_path, key)
            except FileExistsError:
                key = key_path.read_bytes()
        else:
            key = key_path.read_bytes()

        if len(key) != 32:
            raise SecretCodecError("数据库密钥文件长度不正确")
        with suppress(OSError):
            os.chmod(key_path, 0o600)
        return cls(key)

    def encrypt(self, value: str, *, context: str = "") -> str:
        if not value or value.startswith(SECRET_PREFIX):
            return value
        nonce = os.urandom(12)
        associated_data = context.encode("utf-8") or None
        sealed = nonce + self._aead.encrypt(nonce, value.encode("utf-8"), associated_data)
        return SECRET_PREFIX + base64.urlsafe_b64encode(sealed).decode("ascii").rstrip("=")

    def decrypt(self, value: str, *, context: str = "") -> str:
        if not value or not value.startswith(SECRET_PREFIX):
            return value
        encoded = value[len(SECRET_PREFIX) :]
        padding = "=" * (-len(encoded) % 4)
        try:
            sealed = base64.urlsafe_b64decode(encoded + padding)
            nonce, ciphertext = sealed[:12], sealed[12:]
            associated_data = context.encode("utf-8") or None
            return self._aead.decrypt(nonce, ciphertext, associated_data).decode("utf-8")
        except Exception as exc:
            raise SecretCodecError("数据库敏感字段解密失败") from exc

    def lookup_hash(self, value: str, *, namespace: str) -> str:
        message = f"{namespace}\0{value}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()
