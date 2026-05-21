"""CLI smoke tests."""

import json
from pathlib import Path

from pytest import CaptureFixture

from build_engine.cli.commands import main


def test_version_flag_exits_cleanly(capsys: CaptureFixture[str]) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()
    assert "build-engine" in captured.out


def test_doctor_json_reports_missing_credentials(
    capsys: CaptureFixture[str],
    tmp_path: Path,
) -> None:
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
