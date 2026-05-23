"""Packaging and operations asset tests."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_systemd_unit_points_at_packaged_binary() -> None:
    unit = (REPO_ROOT / "packaging/systemd/build-engine.service").read_text(encoding="utf-8")

    assert "ExecStart=/usr/local/bin/build-engine serve" in unit
    assert "Requires=docker.service" in unit
    assert "ProtectSystem=strict" in unit


def test_install_script_installs_service_docs_and_binary() -> None:
    script = (REPO_ROOT / "scripts/install-build-engine.sh").read_text(encoding="utf-8")

    assert "BUILD_ENGINE_BINARY" in script
    assert "packaging/systemd/build-engine.service" in script
    assert "docs/build-engine-operations.md" in script
    assert "DESTDIR" in script


def test_release_script_writes_checksum_and_supports_signing() -> None:
    script = (REPO_ROOT / "scripts/release-artifacts.sh").read_text(encoding="utf-8")

    assert "SHA256SUMS" in script
    assert "sha256sum" in script
    assert "COSIGN_SIGN" in script
    assert "output-certificate" in script
    assert "CHECKSUM_ONLY" in script
    assert "GPG_SIGN" in script


def test_verify_release_script_checks_sigstore_slsa_and_sbom() -> None:
    script = (REPO_ROOT / "scripts/verify-release.sh").read_text(encoding="utf-8")

    assert "cosign verify-blob" in script
    assert "slsa-verifier verify-artifact" in script
    assert "CycloneDX" in script


def test_release_workflow_publishes_signed_attested_artifacts() -> None:
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "v*.*.*" in workflow
    assert "attest-build-provenance@" in workflow
    assert "attest-sbom@" in workflow
    assert "softprops/action-gh-release@" in workflow
    assert "build-engine-${version}-${RELEASE_ARCH}" in workflow


def test_pyinstaller_spec_has_onefile_hidden_imports_and_debuggable_settings() -> None:
    spec = (REPO_ROOT / "packaging/pyinstaller/build-engine.spec").read_text(encoding="utf-8")

    assert 'collect_submodules("build_engine")' in spec
    assert '"websockets"' in spec
    assert "upx=False" in spec
    assert "console=True" in spec
