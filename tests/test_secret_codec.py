"""敏感字段加密测试。"""

from __future__ import annotations

import pytest

from gopay_app.security.codec import SECRET_PREFIX, SecretCodecError


def test_secret_codec_round_trip_and_context(database):
    _engine, _session_factory, codec = database
    plaintext = "token-测试-123456"
    encrypted = codec.encrypt(plaintext, context="account:1")

    assert encrypted.startswith(SECRET_PREFIX)
    assert plaintext not in encrypted
    assert codec.decrypt(encrypted, context="account:1") == plaintext
    with pytest.raises(SecretCodecError):
        codec.decrypt(encrypted, context="account:2")


def test_lookup_hash_is_stable_and_namespaced(database):
    _engine, _session_factory, codec = database
    assert codec.lookup_hash("value", namespace="otp") == codec.lookup_hash("value", namespace="otp")
    assert codec.lookup_hash("value", namespace="otp") != codec.lookup_hash("value", namespace="phone")
