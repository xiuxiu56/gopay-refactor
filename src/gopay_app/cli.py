"""本地服务和数据维护命令。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from gopay_app.api.app import create_app
from gopay_app.config import Settings
from gopay_app.db.engine import (
    build_session_factory,
    create_database_engine,
    database_status,
    upgrade_database,
)
from gopay_app.logging_config import configure_logging
from gopay_app.migration.legacy import LegacyImporter, count_imported_rows, inspect_legacy
from gopay_app.security.codec import SecretCodec


def _settings(database: str = "") -> Settings:
    settings = Settings()
    if database.strip():
        path = Path(database).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        settings = settings.model_copy(update={"database_path": path.resolve()})
    return settings


def _print_preview(preview) -> None:
    print(f"旧数据目录：{preview.source}")
    for item in preview.files:
        state = "有效" if item.valid else "错误"
        if not item.exists:
            state = "缺失"
        detail = f"，{item.message}" if item.message else ""
        print(f"  {item.name}：{state}，记录 {item.records}{detail}")
    for warning in preview.warnings:
        print(f"  提醒：{warning}")
    print(f"预检结果：{'通过' if preview.valid else '存在错误'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gopay-v2", description="GoPay 本地控制台维护工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动本地服务")
    serve.add_argument("--host", default="", help="覆盖监听地址")
    serve.add_argument("--port", type=int, default=0, help="覆盖监听端口")
    serve.add_argument("--database", default="", help="覆盖 SQLite 数据库路径")

    upgrade = subparsers.add_parser("db-upgrade", help="升级数据库结构")
    upgrade.add_argument("--database", default="", help="覆盖 SQLite 数据库路径")

    status = subparsers.add_parser("db-status", help="查看数据库状态")
    status.add_argument("--database", default="", help="覆盖 SQLite 数据库路径")

    importer = subparsers.add_parser("import-legacy", help="预检或迁移旧版数据")
    importer.add_argument("--source", type=Path, required=True, help="旧项目根目录")
    importer.add_argument("--database", default="", help="覆盖 SQLite 数据库路径")
    mode = importer.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只读预检，不创建数据库")
    mode.add_argument("--apply", action="store_true", help="执行幂等迁移")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings(getattr(args, "database", ""))
    configure_logging(settings.log_level)

    if args.command == "serve":
        if args.host:
            settings = settings.model_copy(update={"host": args.host})
        if args.port:
            settings = settings.model_copy(update={"port": args.port})
        uvicorn.run(
            create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level.lower()
        )
        return 0

    if args.command == "db-upgrade":
        upgrade_database(settings)
        engine = create_database_engine(settings)
        try:
            print(json.dumps(database_status(engine, settings.database_path), ensure_ascii=False, indent=2))
        finally:
            engine.dispose()
        return 0

    if args.command == "db-status":
        upgrade_database(settings)
        engine = create_database_engine(settings)
        try:
            print(json.dumps(database_status(engine, settings.database_path), ensure_ascii=False, indent=2))
        finally:
            engine.dispose()
        return 0

    if args.command == "import-legacy":
        preview = inspect_legacy(args.source)
        _print_preview(preview)
        if not args.apply:
            return 0 if preview.valid else 2
        if not preview.valid:
            return 2
        upgrade_database(settings)
        engine = create_database_engine(settings)
        try:
            session_factory = build_session_factory(engine)
            codec = SecretCodec.load(settings.database_key_path)
            result = LegacyImporter(session_factory, codec).apply(preview)
            counts = count_imported_rows(session_factory)
            print(f"本次迁移：{result.imported}")
            print(f"摘要相同已跳过：{result.skipped}")
            print(f"数据库当前记录：{counts}")
        finally:
            engine.dispose()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
