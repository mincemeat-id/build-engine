#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fixtures=(
  "astro-blog"
  "docusaurus-docs"
  "eleventy-blog"
  "gatsby-blog"
  "generic-static"
  "nextjs-export"
  "nuxt-generate"
  "sveltekit-static"
  "vite-vanilla"
  "vitepress-docs"
  "vuepress-docs"
)

for fixture in "${fixtures[@]}"; do
  dir="$ROOT/$fixture"
  echo "Regenerating lockfile for $fixture"
  rm -rf "$dir/node_modules"
  if grep -q '"packageManager"[[:space:]]*:[[:space:]]*"pnpm@' "$dir/package.json"; then
    (cd "$dir" && pnpm install --lockfile-only --ignore-scripts)
  else
    (cd "$dir" && npm install --package-lock-only --ignore-scripts --no-audit --no-fund)
  fi
  rm -rf "$dir/node_modules"
done
