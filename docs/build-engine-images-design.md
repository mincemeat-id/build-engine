# Build Engine Images Repository Design

> **Repository:** `mincemeat-id/build-engine-images`
> **Status:** Design and decision documentation.
> **Audience:** Platform maintainers, build-engine maintainers, security
> reviewers.

The build-engine-images repository owns curated Docker images used by the
standalone build engine. Images are public GHCR artifacts with pinned digests,
SBOMs, provenance, vulnerability gates, and a versioned manifest consumed by
the engine.

## Goals

- Provide auditable, reproducible builder images for v1 GA frameworks.
- Keep image lifecycle independent from the engine binary.
- Pin image digests in a versioned manifest.
- Publish public GHCR images with SBOM and provenance.
- Block publication when vulnerability budget is exceeded.
- Keep images free of secrets and platform credentials.

## Non-Goals

- User-supplied builder images.
- Dynamic runtime images for SSR applications.
- Private registry dependency in v1.
- Multi-architecture images in v1.
- Frameworks without fixture coverage.

## Repository Layout

```text
build-engine-images/
├── node/
│   ├── 20.Dockerfile
│   └── 22.Dockerfile
├── bun/
│   └── 1.Dockerfile
├── hugo/
│   └── latest.Dockerfile
├── entrypoint/
│   └── build-entrypoint.sh
├── manifest.json
├── tests/
│   ├── fixtures/
│   └── smoke/
└── .github/workflows/
    ├── build-and-publish.yml
    ├── trivy-scan.yml
    ├── fixture-smoke.yml
    └── manifest-publish.yml
```

## Image Matrix

Supported images:

| Logical Image | GHCR Tag Pattern | Purpose |
|---------------|------------------|---------|
| `node:20` | `ghcr.io/mincemeat-id/build-engine-images/node:20-X.Y.Z` | Node LTS fallback and projects requiring Node 20. |
| `node:22` | `ghcr.io/mincemeat-id/build-engine-images/node:22-X.Y.Z` | Default Node image. |
| `bun:1` | `ghcr.io/mincemeat-id/build-engine-images/bun:1-X.Y.Z` | Bun package manager/runtime. |
| `hugo:latest` | `ghcr.io/mincemeat-id/build-engine-images/hugo:X.Y.Z` | Hugo static builds. |
| `zola:latest` | `ghcr.io/mincemeat-id/build-engine-images/zola:X.Y.Z` | Zola static builds. |

The default is image reuse where practical. Framework-specific images are only
added when a framework needs extra native dependencies or the generic Node
image would become too large. Angular static and Remix SPA build with the
existing Node 22 image.

## Base Image Policy

- Use Debian/Ubuntu-based official runtime images when possible for glibc and
  native dependency compatibility.
- Pin base image by digest in Dockerfiles or lock metadata.
- Install only required build tools:
  - `ca-certificates`
  - `curl`
  - `git`
  - `tar`
  - `xz-utils`
  - `python3`, `make`, `g++` only where needed for native npm modules
- Enable Corepack in Node images.
- Include no secrets, tokens, SSH keys, npmrc credentials, or platform config.

## Entrypoint Contract

Each image uses `/build-entrypoint.sh`.

Inputs:

| Path / Env | Purpose |
|------------|---------|
| `/build/manifest.json` | Build command, package manager, output dir, framework, root, env metadata. |
| `/workspace/src` | Source root mount. |
| `/workspace/out` | Normalized output mount. |
| `/cache` | Package-manager cache mount. |

Entrypoint responsibilities:

1. Read manifest.
2. Configure package-manager cache paths.
3. Run install command exactly as requested.
4. Run build command exactly as requested.
5. Copy or move configured output directory into `/workspace/out`.
6. Preserve stdout/stderr for engine log streaming.
7. Exit non-zero on install/build/output copy failure.

The engine performs final output validation and artifact packaging; images
should not duplicate those checks beyond useful early errors.

## Manifest Contract

`manifest.json` in the images repo:

```json
{
  "version": "1.0.0",
  "generated_at": "2026-05-19T00:00:00Z",
  "images": {
    "node:20": {
      "tag": "ghcr.io/mincemeat-id/build-engine-images/node:20-1.0.0",
      "digest": "sha256:...",
      "frameworks": ["astro", "vite", "eleventy", "docusaurus", "vitepress", "vuepress", "gatsby", "nextjs-export", "nuxt-generate", "sveltekit-static", "generic"]
    },
    "node:22": {
      "tag": "ghcr.io/mincemeat-id/build-engine-images/node:22-1.0.0",
      "digest": "sha256:...",
      "frameworks": ["astro", "vite", "eleventy", "docusaurus", "vitepress", "vuepress", "gatsby", "nextjs-export", "nuxt-generate", "sveltekit-static", "generic"]
    },
    "bun:1": {
      "tag": "ghcr.io/mincemeat-id/build-engine-images/bun:1-1.0.0",
      "digest": "sha256:...",
      "frameworks": ["astro", "vite", "generic"]
    },
    "hugo:latest": {
      "tag": "ghcr.io/mincemeat-id/build-engine-images/hugo:1.0.0",
      "digest": "sha256:...",
      "frameworks": ["hugo"]
    }
  },
  "engine_compat": {
    "proto_min": 1,
    "proto_max": 1,
    "engine_min": "1.0.0"
  }
}
```

Rules:

- Manifest version is immutable once released.
- Every image entry must include a digest.
- Engine pulls by digest when available.
- Build-engine releases pin an accepted manifest version range.

## Framework Acceptance Matrix

Supported frameworks:

| Framework | Image | Fixture | Expected Output |
|-----------|-------|---------|-----------------|
| Astro | `node:22` or `bun:1` | `astro-blog` | `dist/` |
| Vite | `node:22` or `bun:1` | `vite-vanilla` | `dist/` |
| Eleventy | `node:22` | `eleventy-blog` | `_site/` |
| Docusaurus | `node:22` | `docusaurus-docs` | `build/` |
| VitePress | `node:22` | `vitepress-docs` | `.vitepress/dist/` |
| VuePress | `node:22` | `vuepress-docs` | `dist/` |
| Gatsby | `node:22` | `gatsby-blog` | `public/` |
| Hugo | `hugo:latest` | `hugo-quickstart` | `public/` |
| Zola | `zola:latest` | `zola-quickstart` | `public/` |
| Next.js export | `node:22` | `nextjs-export` | `out/` |
| Nuxt generate | `node:22` | `nuxt-generate` | `.output/public/` |
| SvelteKit static | `node:22` | `sveltekit-static` | `build/` |
| Angular static | `node:22` | `angular-static` | `dist/<project>/browser/` |
| Remix SPA | `node:22` | `remix-spa` | `build/client/` |
| Generic | `node:22` | `generic-static` | inferred |

Negative fixtures:

| Fixture | Expected Result |
|---------|-----------------|
| `nextjs-noexport` | `BUILD_INCOMPATIBLE`, code `NEXTJS_REQUIRES_EXPORT`. |
| `remix-ssr` | `BUILD_INCOMPATIBLE`, code `REMIX_REQUIRES_SPA_MODE`. |
| `angular-ssr` | `BUILD_INCOMPATIBLE`, code `ANGULAR_REQUIRES_STATIC_OUTPUT`. |
| `sveltekit-node-adapter` | `BUILD_INCOMPATIBLE`, code `SVELTEKIT_REQUIRES_STATIC_ADAPTER`. |
| `nuxt-ssr-build` | `BUILD_INCOMPATIBLE`, code `NUXT_REQUIRES_GENERATE`. |

## Security And Supply Chain

Publication requirements:

- Trivy scan on every PR and release build.
- SBOM generated in CycloneDX format.
- Cosign keyless signature.
- SLSA provenance attached.
- Immutable release tags.
- Weekly rebuild scan.

CVE budget:

| Severity | Policy |
|----------|--------|
| Critical with fix | Block publish. |
| More than 5 high with fixes | Block publish. |
| High without fix | Require maintainer acknowledgement in release notes. |
| Medium with fix | Warn. |

Secret policy:

- No secrets in Dockerfiles, layers, build args, labels, test fixtures, or
  published artifacts.
- CI uses short-lived GitHub OIDC permissions for GHCR/cosign where possible.

## Publication Flow

PR:

1. Build changed images.
2. Run fixture smoke tests.
3. Run Trivy.
4. Generate SBOM/provenance as artifacts.
5. Do not publish stable tags.

Merge to main:

1. Rebuild images.
2. Push RC tags.
3. Sign RC images.
4. Publish candidate manifest artifact.

Release:

1. Maintainer cuts release `vX.Y.Z`.
2. Workflow rebuilds and pushes immutable release tags.
3. Workflow records digests into `manifest.json`.
4. Workflow signs images and attaches SBOM/provenance.
5. Workflow opens PR in `mincemeat-id/build-engine` to bump accepted manifest.

Rollback:

- Revert manifest bump in `build-engine`.
- Redeploy engine with previous accepted manifest.
- Do not mutate existing image tags.
