"""读取 .env 的 HOST、PORT 并启动 API；不安装依赖或同步数据。"""
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .server_config import ServerSettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        settings = ServerSettings.from_env()
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr, flush=True)
        return 1
    print(
        f"API 本机访问：{settings.local_url}（监听 {settings.host}，端口 {settings.port}）。按 Ctrl+C 停止。",
        flush=True,
    )
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
