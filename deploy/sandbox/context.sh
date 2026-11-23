#!/usr/bin/env bash
# Build context of the task sandbox image without a Kite machine (CI, kite-sandbox on GHCR): this folder's
# Dockerfile, OpenCode from npm at the version in deploy/opencode.env (its SHA-512 checked) and an empty
# package cache (OpenCode 2 runs offline without one). `kite sandbox build` makes the same context from the
# machine's own OpenCode instead.
#   deploy/sandbox/context.sh /tmp/sandbox-context && docker build /tmp/sandbox-context
set -euo pipefail
out="${1:?usage: context.sh OUTDIR}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/opencode.env
. "$here/../opencode.env"
mkdir -p "$out/opencode-cache"
cp "$here/Dockerfile" "$out/Dockerfile"
curl -fsSL -o "$out/opencode.tgz" \
  "https://registry.npmjs.org/@opencode/cli-linux-x64/-/cli-linux-x64-${OPENCODE_VERSION}.tgz"
echo "${OPENCODE_SHA512}  $out/opencode.tgz" | sha512sum -c -
tar -xzf "$out/opencode.tgz" -C "$out" package/bin/opencode
mv "$out/package/bin/opencode" "$out/opencode"
rm -rf "$out/package" "$out/opencode.tgz"
echo "context ready: $out (OpenCode ${OPENCODE_VERSION})"
