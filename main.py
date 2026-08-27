"""GoPay 本地控制台的直接启动入口。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from gopay_app.cli import main as cli_main  # noqa: E402


def main() -> int:
    """使用 serve 子命令启动本地服务。"""
    try:
        return cli_main(["serve", *sys.argv[1:]])
    except KeyboardInterrupt:
        print("服务已停止")
        return 0
    except Exception as exc:
        print(f"项目启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
