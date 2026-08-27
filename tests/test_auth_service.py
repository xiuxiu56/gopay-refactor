"""管理员与会话服务测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from gopay_app.auth.service import AuthError, AuthService
from gopay_app.db.models import Admin


def test_single_admin_login_and_logout(database):
    _engine, session_factory, _codec = database
    service = AuthService(session_factory, session_ttl_hours=24)

    assert service.setup_required() is True
    created = service.setup("admin", "测试-password-123")
    assert service.setup_required() is False
    assert service.authenticate(created.token).username == "admin"
    assert service.verify_csrf(created.token, created.csrf_token) is True

    with session_factory() as session:
        admin = session.scalar(select(Admin))
        assert admin is not None
        assert "测试-password-123" not in admin.password_hash

    with pytest.raises(AuthError) as repeated:
        service.setup("other", "测试-password-456")
    assert repeated.value.code == "setup_completed"

    logged_in = service.login("ADMIN", "测试-password-123")
    assert service.authenticate(logged_in.token).id == created.admin.id
    service.logout(logged_in.token, created.admin.id)
    assert service.authenticate(logged_in.token) is None


def test_invalid_login_uses_generic_error(database):
    _engine, session_factory, _codec = database
    service = AuthService(session_factory, session_ttl_hours=24)
    service.setup("admin", "测试-password-123")

    with pytest.raises(AuthError) as failure:
        service.login("admin", "错误-password-123")
    assert failure.value.code == "invalid_credentials"
    assert str(failure.value) == "账号或密码错误"
