"""Configuration loading tests."""

from pathlib import Path

from build_engine.config import (
    EngineCredentials,
    config_capabilities,
    load_config,
    write_credentials,
)


def test_config_layering_prefers_env_and_cli_over_files(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    credentials_path = tmp_path / "credentials.toml"
    config_path.write_text(
        "\n".join(
            (
                'backend_url = "https://from-config.example"',
                'name = "config-name"',
                "max_concurrency = 3",
                f'credentials_path = "{credentials_path}"',
                "",
            )
        )
    )
    write_credentials(
        credentials_path,
        EngineCredentials(
            engine_id="00000000-0000-0000-0000-000000000000",
            engine_secret="secret",
            session_jwt="jwt",
            session_jwt_expires_at="2030-01-01T00:00:00+00:00",
            backend_url="https://from-credentials.example",
            name="credential-name",
        ),
    )

    config = load_config(
        config_path=config_path,
        env={"BUILD_ENGINE_MAX_CONCURRENCY": "4"},
        overrides={"name": "cli-name"},
    )

    assert config.backend_url == "https://from-credentials.example"
    assert config.name == "cli-name"
    assert config.max_concurrency == 4
    assert config.credentials_path == credentials_path


def test_config_capabilities_match_openapi_contract(tmp_path: Path) -> None:
    config = load_config(
        config_path=tmp_path / "missing.toml",
        overrides={
            "max_concurrency": 2,
            "images": ("node:22", "hugo:latest"),
            "image_manifest_version": "1.2.3",
            "state_dir": tmp_path / "state",
        },
    )

    assert config.state_dir == tmp_path / "state"
    assert config_capabilities(config) == {
        "os": "linux",
        "arch": "amd64",
        "max_concurrency": 2,
        "images": ["node:22", "hugo:latest"],
        "proto_version": 1,
        "image_manifest_version": "1.2.3",
    }
