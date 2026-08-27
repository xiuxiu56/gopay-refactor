"""ASGI 应用入口。"""

from gopay_app.api.app import create_app

app = create_app()
