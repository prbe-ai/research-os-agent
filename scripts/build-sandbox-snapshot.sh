#!/usr/bin/env bash
# Rebuild the committed probe-sandbox-snapshot binaries reproducibly.
# Same Go version + these flags => bit-identical output (-trimpath, no
# buildid, stripped), so `git diff --stat` after a rebuild verifies the
# committed binaries match the committed source.
set -euo pipefail

cd "$(dirname "$0")/../tools/sandbox-snapshot"
out="../../src/probe/connectors/_bin"
mkdir -p "$out"

go vet ./...
go test ./...

for arch in amd64 arm64; do
  CGO_ENABLED=0 GOOS=linux GOARCH="$arch" \
    go build -trimpath -ldflags="-s -w -buildid=" \
    -o "$out/sandbox-snapshot-linux-$arch" .
done

ls -la "$out"
