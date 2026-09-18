import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.start_server as startup
from app.config import Settings  # Load app configuration before isolating test environments.

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "mudren project"
    root.mkdir()
    monkeypatch.setattr(startup, "PROJECT_ROOT", root)
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    return root, python


def stage(command):
    if "venv" in command:
        return "venv"
    if "--version" in command:
        return "pip_probe"
    if "ensurepip" in command:
        return "ensurepip"
    if "install" in command:
        return "install"
    if "check" in command:
        return "pip_check"
    if "app.server" in command:
        return "serve"
    if any(item.endswith("sync_data.py") for item in command):
        return "sync"
    if any("validate_configuration()" in item for item in command):
        return "config"
    return "python_check"


def simulate(monkeypatch, fail=None, no_pip=False):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        current = stage(command)
        code = 1 if current == fail or current == "pip_probe" and no_pip else 0
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(startup.subprocess, "run", run)
    return calls


def test_startup_installs_then_syncs_before_serving(project, monkeypatch):
    root, python = project
    calls = simulate(monkeypatch)
    assert startup.main(["--no-pause"]) == 0
    assert [stage(command) for command, _ in calls] == [
        "python_check", "pip_probe", "install", "pip_check", "config", "sync", "serve",
    ]
    assert all(command[0] == str(python) and kwargs["cwd"] == root for command, kwargs in calls)
    install = next(command for command, _ in calls if stage(command) == "install")
    assert str(root / "requirements.txt") in install
    assert "--upgrade" not in install and "--force-reinstall" not in install
    sync = next(command for command, _ in calls if stage(command) == "sync")
    assert "--full" not in sync


@pytest.mark.parametrize("failure", ["python_check", "ensurepip", "install", "pip_check", "config", "sync"])
def test_failed_startup_stage_never_starts_api(project, monkeypatch, failure):
    calls = simulate(monkeypatch, fail=failure, no_pip=failure == "ensurepip")
    assert startup.main([]) == 1
    stages = [stage(command) for command, _ in calls]
    assert stages[-1] == failure
    assert "serve" not in stages


def test_missing_pip_is_bootstrapped_before_install(project, monkeypatch):
    calls = simulate(monkeypatch, no_pip=True)
    assert startup.main([]) == 0
    stages = [stage(command) for command, _ in calls]
    assert stages.index("pip_probe") < stages.index("ensurepip") < stages.index("install")


def test_missing_environment_is_created_with_selected_python(project, monkeypatch):
    root, python = project
    python.unlink()
    calls = simulate(monkeypatch)
    assert startup.main([]) == 0
    assert calls[0][0] == [sys.executable, "-m", "venv", str(root / ".venv")]
    assert all(command[0] == str(python) for command, _ in calls[1:])


def test_venv_creation_failure_stops_startup(project, monkeypatch):
    _, python = project
    python.unlink()
    calls = simulate(monkeypatch, fail="venv")
    assert startup.main([]) == 1
    assert len(calls) == 1


def test_old_python_is_rejected_before_installation(project, monkeypatch):
    calls = simulate(monkeypatch)
    monkeypatch.setattr(startup.sys, "version_info", (3, 9, 0))
    assert startup.main([]) == 1
    assert calls == []


def test_server_exit_status_is_preserved(project, monkeypatch):
    calls = simulate(monkeypatch, fail="serve")
    assert startup.main([]) == 1
    assert stage(calls[-1][0]) == "serve"


def test_interrupt_stops_startup(project, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(startup.subprocess, "run", interrupted)
    assert startup.main([]) == 130


@pytest.fixture
def configuration(project, monkeypatch):
    root, _ = project
    for key in (*startup.REQUIRED_ENV, "DB_PASSWORD", "DB_PORT", "HOST", "PORT"):
        monkeypatch.delenv(key, raising=False)
    (root / ".env.example").write_text(
        (REPOSITORY / ".env.example").read_text(encoding="utf-8"), encoding="utf-8",
    )
    return root


def set_valid_environment(monkeypatch):
    values = {
        "DIFY_API_KEY": "test-dify", "DASHSCOPE_API_KEY": "test-model",
        "DB_HOST": "localhost", "DB_USER": "test-user", "DB_NAME": "test-forum", "DB_PASSWORD": "",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_first_run_creates_template_then_stops(configuration):
    with pytest.raises(startup.StartupError, match="已根据"):
        startup.validate_configuration()
    assert (configuration / ".env").read_bytes() == (configuration / ".env.example").read_bytes()


def test_valid_process_environment_does_not_require_env_file(configuration, monkeypatch):
    set_valid_environment(monkeypatch)
    startup.validate_configuration()
    assert not (configuration / ".env").exists()


def test_existing_env_is_loaded_and_preserved(configuration, monkeypatch):
    values = set_valid_environment(monkeypatch)
    for key in values:
        monkeypatch.delenv(key)
    env_file = configuration / ".env"
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    env_file.write_text(content, encoding="utf-8")
    startup.validate_configuration()
    assert env_file.read_text(encoding="utf-8") == content
    for key in values:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("key", list(startup.PLACEHOLDERS))
def test_placeholder_credentials_stop_before_sync(configuration, monkeypatch, key):
    set_valid_environment(monkeypatch)
    monkeypatch.setenv(key, startup.PLACEHOLDERS[key])
    with pytest.raises(startup.StartupError, match=key):
        startup.validate_configuration()


@pytest.mark.parametrize("value", ["invalid", "0", "65536"])
def test_invalid_database_port_is_rejected(configuration, monkeypatch, value):
    set_valid_environment(monkeypatch)
    monkeypatch.setenv("DB_PORT", value)
    with pytest.raises(startup.StartupError, match="DB_PORT"):
        startup.validate_configuration()


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
@pytest.mark.parametrize("existing_venv", [True, False])
def test_batch_handles_spaces_unicode_and_propagates_exit_status(tmp_path, existing_venv):
    # Execute only a harmless bootstrap stub: no pip, real DB, API or model calls.
    root = tmp_path / "社区 service & (test)!"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(REPOSITORY / "start_server.bat", root / "start_server.bat")
    (root / "scripts" / "start_server.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'prefix': sys.prefix}))\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    if existing_venv:
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
            check=True, capture_output=True, timeout=30,
        )
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call", str(root / "start_server.bat"), "--no-pause"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 7, result.stdout + result.stderr
    data = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))
    assert data["args"] == ["--no-pause"]
    assert Path(data["cwd"]) == root
    if existing_venv:
        assert Path(data["prefix"]) == root / ".venv"


@pytest.mark.parametrize("key, value", [("HOST", "http://localhost"), ("PORT", "65536")])
def test_invalid_listener_is_rejected_before_sync(configuration, monkeypatch, key, value):
    set_valid_environment(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        startup.validate_configuration()
