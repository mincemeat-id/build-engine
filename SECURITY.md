# Security Policy

## Supported Versions

Security fixes are prepared for the latest public minor release of the
standalone build engine. Before the first public release is cut, that target is
the upcoming `0.2.x` line.

| Version | Supported |
|---------|-----------|
| `0.2.x` | Yes |
| `< 0.2` | No public support window |

## Reporting A Vulnerability

Please do not report suspected vulnerabilities in public issues.

Use GitHub's private vulnerability reporting for this repository when
available, or email the maintainers through the contact address listed on the
Mincemeat project page. Include:

- Affected version or commit.
- A short description of impact.
- Reproduction steps or proof-of-concept details.
- Whether the issue is already public or known to be exploited.

Maintainers aim to acknowledge reports within 3 business days and provide an
initial triage update within 7 business days. Coordinated disclosure timing is
handled case by case based on severity, exploitability, and available
mitigations.

## Scope

In scope:

- Build-engine agent authentication, registration, session refresh, and WSS
  protocol handling.
- Docker execution hardening, network guard behavior, cache handling, and
  secret redaction.
- Release artifacts, signatures, checksums, SBOM/provenance material, and
  installer packaging in this repository.

Out of scope:

- Vulnerabilities in customer build source code or third-party dependencies
  installed by a customer build.
- Issues requiring control of the build-engine host root account.
- Denial-of-service reports based only on exhausting intentionally configured
  host resources.
