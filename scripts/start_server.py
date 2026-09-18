"""准备项目环境、同步 mud.ren 社区知识库，然后启动 API。"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ENV = ("DIFY_API_KEY", "DASHSCOPE_API_KEY", "DB_HOST", "DB_USER", "DB_NAME")
PLACEHOLDERS = {
    "DIFY_API_KEY": "your_dify_api_key_here",
    "DASHSCOPE_API_KEY": "sk-your_dashscope_api_key_here",
    "DB_NAME": "your_database_name",
    "DB_PASSWORD": "your_password",
}


class StartupError(RuntimeError):
    pass


def run_step(command: list[str], description: str):
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode:
        raise StartupError(f"{description}失败（退出码 {result.returncode}），已停止启动，请检查上方日志。")


def ensure_virtualenv() -> Path:
    if sys.version_info < (3, 10):
        raise StartupError("需要 Python 3.10 及以上版本，推荐 Python 3.12。")
    environment = PROJECT_ROOT / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("[1/4] 正在创建项目虚拟环境 .venv……", flush=True)
        run_step([sys.executable, "-m", "venv", str(environment)], "创建虚拟环境")
    else:
        print("[1/4] 使用项目虚拟环境 .venv。", flush=True)
    # Do not install packages globally if an existing environment is broken.
    run_step([
        str(python), "-c",
        "import sys; sys.exit(not (sys.version_info >= (3, 10) and sys.prefix != sys.base_prefix))",
    ], "虚拟环境检查（需要有效的 Python 3.10+ 环境）")
    return python


def install_dependencies(python: Path):
    print("[2/4] 正在检查并补齐项目依赖……", flush=True)
    pip_probe = subprocess.run(
        [str(python), "-m", "pip", "--version"], cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if pip_probe.returncode:
        run_step([str(python), "-m", "ensurepip", "--upgrade"], "安装 pip")
    pip = [str(python), "-m", "pip", "--disable-pip-version-check", "--no-input"]
    # No --upgrade or --force-reinstall: preserve installed versions that satisfy requirements.
    run_step([*pip, "install", "-r", str(PROJECT_ROOT / "requirements.txt")], "安装项目依赖")
    run_step([*pip, "check"], "依赖兼容性检查")


def validate_configuration():
    """Called in the prepared virtual environment, before any database/model requests."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists() and not all(os.getenv(name, "").strip() for name in REQUIRED_ENV):
        template = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        try:
            with env_file.open("x", encoding="utf-8") as handle:
                handle.write(template)
        except FileExistsError:
            pass  # Another process created the file; validate it below.
        else:
            raise StartupError("已根据 .env.example 创建 .env，请填写 API 密钥和数据库配置后重新启动。")
    # Import only after dependency setup; never require third-party packages to bootstrap.
    sys.path.insert(0, str(PROJECT_ROOT))
    from dotenv import load_dotenv

    load_dotenv(env_file)
    from app.config import Settings
    from app.server_config import ServerSettings

    ServerSettings.from_env()
    Settings.from_env()
    invalid = [
        name for name in REQUIRED_ENV
        if not os.getenv(name, "").strip() or os.getenv(name, "").strip() == PLACEHOLDERS.get(name)
    ]
    if os.getenv("DB_PASSWORD") == PLACEHOLDERS["DB_PASSWORD"]:
        invalid.append("DB_PASSWORD")
    if invalid:
        raise StartupError("请在 .env 或进程环境变量中填写有效配置：" + ", ".join(invalid))
    try:
        port = int(os.getenv("DB_PORT", "3306"))
    except ValueError:
        raise StartupError("DB_PORT 必须是 1 到 65535 之间的整数。") from None
    if not 1 <= port <= 65535:
        raise StartupError("DB_PORT 必须在 1 到 65535 之间。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-pause", action="store_true", help="批处理退出时不等待按键，供终端或自动化调用使用。")
    parser.parse_args(argv)
    try:
        python = ensure_virtualenv()
        install_dependencies(python)
        print("[3/4] 正在校验配置并增量同步知识库……", flush=True)
        run_step([
            str(python), "-c",
            "from scripts.start_server import validate_configuration; validate_configuration()",
        ], "配置校验")
        run_step([str(python), "-u", str(PROJECT_ROOT / "scripts" / "sync_data.py")], "知识库同步")
        print("[4/4] 正在按 HOST、PORT 配置启动 API……", flush=True)
        return subprocess.run([
            str(python), "-m", "app.server",
        ], cwd=PROJECT_ROOT).returncode
    except (StartupError, OSError) as exc:
        print(f"[错误] {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\n启动流程或服务已中断。", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
