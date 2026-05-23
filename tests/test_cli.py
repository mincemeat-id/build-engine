"""CLI smoke tests."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from build_engine import __version__
from build_engine.cli import doctor as doctor_module
from build_engine.cli.commands import main
from build_engine.cli.doctor import DoctorCheck, DoctorReport, _clock_skew_seconds
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


def test_drain_command_persists_local_drain_marker(
    capsys: CaptureFixture[str],
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'state_dir = "{tmp_path}"\n', encoding="utf-8")

    exit_code = main(["drain", "--config", str(config_path)])

    assert exit_code == 0
    assert (tmp_path / "drain.json").exists()
    assert "DRAINING" in capsys.readouterr().out


def test_clock_skew_seconds_returns_none_when_header_missing() -> None:
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert _clock_skew_seconds(None, now=now) is None


def test_clock_skew_seconds_parses_valid_rfc_date_header() -> None:
    # Backend says it's 12:00:30Z, we say it's 12:00:00Z → skew of -30s.
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    skew = _clock_skew_seconds("Fri, 22 May 2026 12:00:30 GMT", now=now)
    assert skew is not None
    assert abs(skew - (-30.0)) < 0.001


def test_clock_skew_seconds_swallows_value_error_on_malformed_header() -> None:
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert _clock_skew_seconds("not a real date", now=now) is None


def test_clock_skew_seconds_swallows_type_error_branch(
    monkeypatch: MonkeyPatch,
) -> None:
    def raise_type_error(_value: str) -> datetime:
        raise TypeError("forced for branch coverage")

    monkeypatch.setattr(doctor_module, "parsedate_to_datetime", raise_type_error)
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert _clock_skew_seconds("Fri, 22 May 2026 12:00:00 GMT", now=now) is None
