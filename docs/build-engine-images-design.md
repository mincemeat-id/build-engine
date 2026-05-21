# Build Engine Images Repository Design

> **Repository:** `mincemeat-id/build-engine-images`
> **Status:** Final implementation plan.
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

V1 GA images:

| Logical Image | GHCR Tag Pattern | Purpose |
|---------------|------------------|---------|
| `node:20` | `ghcr.io/mincemeat-id/build-engine-images/node:20-X.Y.Z` | Node LTS fallback and projects requiring Node 20. |
| `node:22` | `ghcr.io/mincemeat-id/build-engine-images/node:22-X.Y.Z` | Default Node image. |
| `bun:1` | `ghcr.io/mincemeat-id/build-engine-images/bun:1-X.Y.Z` | Bun package manager/runtime. |
| `hugo:latest` | `ghcr.io/mincemeat-id/build-engine-images/hugo:X.Y.Z` | Hugo static builds. |

V1.x candidates:

| Logical Image | Condition To Ship |
|---------------|-------------------|
| `zola:latest` | Zola fixture, docs, image size review, and smoke pass. |
| `node-angular:22` or `node:22` reuse | Angular static fixture and output detection pass. |
| `node-remix:22` or `node:22` reuse | Remix SPA fixture and SSR rejection fixture pass. |

The default should be image reuse where practical. Add framework-specific
images only when a framework needs extra native dependencies or the generic
Node image becomes too large.

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

V1 GA:

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
| Next.js export | `node:22` | `nextjs-export` | `out/` |
| Nuxt generate | `node:22` | `nuxt-generate` | `.output/public/` |
| SvelteKit static | `node:22` | `sveltekit-static` | `build/` |
| Generic | `node:22` | `generic-static` | inferred |

Negative fixtures:

| Fixture | Expected Result |
|---------|-----------------|
| `nextjs-noexport` | `BUILD_INCOMPATIBLE`, code `NEXTJS_REQUIRES_EXPORT`. |
| `remix-ssr` | `BUILD_INCOMPATIBLE`, code `REMIX_REQUIRES_SPA_MODE`. |
| `sveltekit-node-adapter` | `BUILD_INCOMPATIBLE`, code `SVELTEKIT_REQUIRES_STATIC_ADAPTER`. |
| `nuxt-ssr-build` | `BUILD_INCOMPATIBLE`, code `NUXT_REQUIRES_GENERATE`. |

V1.x candidates must add both positive and negative fixtures before being
marked GA.

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

## Implementation Plan

### Stage 0 - Scaffold

Estimate: 1-2 days. Complexity: S.

- [ ] Create repo structure.
- [ ] Add initial README and manifest schema.
- [ ] Add `.editorconfig`, linting for shell/Dockerfiles if available.
- [ ] Add CI skeleton.
- [ ] Add GHCR package naming conventions.

### Stage 1 - Entrypoint Contract

Estimate: 2-3 days. Complexity: M.

- [ ] Implement `/build-entrypoint.sh`.
- [ ] Add manifest parsing.
- [ ] Add package-manager cache path setup.
- [ ] Add install command dispatch for npm, pnpm, yarn, bun.
- [ ] Add build command execution.
- [ ] Add output copy to `/workspace/out`.
- [ ] Add shell tests for malformed manifests and missing output dirs.

### Stage 2 - V1 GA Images

Estimate: 4-6 days. Complexity: L.

- [ ] Build `node:20` Dockerfile.
- [ ] Build `node:22` Dockerfile.
- [ ] Build `bun:1` Dockerfile.
- [ ] Build `hugo:latest` Dockerfile.
- [ ] Pin base image digests.
- [ ] Enable Corepack in Node images.
- [ ] Add common native build dependencies only where needed.
- [ ] Keep image size report in CI.

### Stage 3 - Fixtures And Smoke Tests

Estimate: 1-1.5 weeks. Complexity: L.

- [ ] Add positive fixtures for every v1 GA framework.
- [ ] Add negative fixtures for config-dependent frameworks.
- [ ] Add cold build smoke script.
- [ ] Add warm-cache smoke script.
- [ ] Assert output contains `index.html`.
- [ ] Assert expected log breadcrumbs.
- [ ] Produce fixture timing report.

### Stage 4 - Manifest And Compatibility

Estimate: 2-3 days. Complexity: M.

- [ ] Define JSON schema for `manifest.json`.
- [ ] Generate manifest from built image digests.
- [ ] Validate manifest in CI.
- [ ] Add `engine_compat` metadata.
- [ ] Add release notes template with compatibility matrix.

### Stage 5 - Security Pipeline

Estimate: 3-5 days. Complexity: M.

- [ ] Add Trivy scan workflow.
- [ ] Enforce CVE budget.
- [ ] Add CycloneDX SBOM generation.
- [ ] Add cosign keyless signing.
- [ ] Add provenance generation.
- [ ] Add weekly rebuild/rescan cron.
- [ ] Add security exception documentation process.

### Stage 6 - Publication And Rollback

Estimate: 2-3 days. Complexity: M.

- [ ] Add RC publication on merge.
- [ ] Add stable publication on release.
- [ ] Enforce immutable tags.
- [ ] Add manifest-publish workflow.
- [ ] Add automated PR to `build-engine` for manifest bumps.
- [ ] Document rollback.

### Stage 7 - V1.x Candidate Evaluation

Estimate: 1-2 weeks after v1 GA. Complexity: M.

- [ ] Add Zola Dockerfile or reuse strategy.
- [ ] Add Zola positive fixture.
- [ ] Add Angular static positive/negative fixtures.
- [ ] Add Remix SPA positive and Remix SSR negative fixtures.
- [ ] Decide whether framework-specific images are needed.
- [ ] Promote candidates only after fixture, docs, scan, and size gates pass.

## Acceptance Criteria

- Every v1 GA image builds reproducibly in CI.
- Every released image has digest, SBOM, provenance, and cosign signature.
- `manifest.json` contains only digest-pinned published images.
- Fixture smoke tests pass for all v1 GA frameworks.
- Critical/high vulnerability gate blocks publication as defined.
- No secrets appear in image layers or repo scan.
- Build-engine can pull every manifest image by digest.
