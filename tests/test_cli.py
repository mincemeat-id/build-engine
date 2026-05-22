"""CLI smoke tests."""

import json
import subprocess
import sys
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from build_engine import __version__
from build_engine.cli.commands import main
from build_engine.cli.doctor import DoctorCheck, DoctorReport
from build_engine.config import EngineConfig


def test_version_flag_exits_cleanly(capsys: CaptureFixture[str]) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()
    assert "build-engine" in captured.out


def test_module_entrypoint_starts_and_reports_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "build_engine", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "build-engine" in result.stdout


def test_doctor_json_reports_missing_credentials(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_doctor(*_args: object, **_kwargs: object) -> DoctorReport:
        return DoctorReport(
            version=__version__,
            protocol_version=1,
            status="error",
            checks=(
                DoctorCheck(
                    name="credentials",
                    status="fail",
                    detail=f"No such file: {tmp_path / 'credentials.toml'}",
                ),
            ),
        )

    monkeypatch.setattr("build_engine.cli.commands.run_doctor", fake_doctor)
    exit_code = main(
        [
            "doctor",
            "--json",
            "--config",
            str(tmp_path / "missing.toml"),
            "--credentials",
            str(tmp_path / "credentials.toml"),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["protocol_version"] == 1
    assert payload["status"] == "error"
    assert payload["checks"][0]["name"] == "credentials"


def test_doctor_human_output(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_doctor(*_args: object, **_kwargs: object) -> DoctorReport:
        return DoctorReport(
            version=__version__,
            protocol_version=1,
            status="ok",
            checks=(DoctorCheck(name="version", status="ok", detail="build-engine ready"),),
        )

    monkeypatch.setattr("build_engine.cli.commands.run_doctor", fake_doctor)

    exit_code = main(
        [
            "doctor",
            "--config",
            str(tmp_path / "missing.toml"),
            "--credentials",
            str(tmp_path / "credentials.toml"),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "build-engine doctor: ok" in captured.out
    assert "- version: OK" in captured.out


def test_doctor_accepts_network_blocklist_flag(
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_config: EngineConfig | None = None

    def fake_doctor(config: EngineConfig, **_kwargs: object) -> DoctorReport:
        nonlocal captured_config
        captured_config = config
        return DoctorReport(
            version=__version__,
            protocol_version=1,
            status="ok",
            checks=(DoctorCheck(name="network_guard", status="ok", detail="ready"),),
        )

    monkeypatch.setattr("build_engine.cli.commands.run_doctor", fake_doctor)

    exit_code = main(
        [
            "doctor",
            "--config",
            str(tmp_path / "missing.toml"),
            "--credentials",
            str(tmp_path / "credentials.toml"),
            "--network-blocklist",
            "203.0.113.0/24,198.51.100.7",
        ]
    )

    assert exit_code == 0
    assert captured_config is not None
    assert captured_config.network_blocklist == ("203.0.113.0/24", "198.51.100.7")
    assert "network_guard" in capsys.readouterr().out


def test_serve_reports_missing_credentials(
    capsys: CaptureFixture[str],
    tmp_path: Path,
) -> None:
    exit_code = main(
        [
            "serve",
            "--config",
            str(tmp_path / "missing.toml"),
            "--credentials",
            str(tmp_path / "credentials.toml"),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "not registered" in captured.err
