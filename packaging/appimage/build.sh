#!/usr/bin/env bash
# Build the Vice AppImage using appimage-builder.
# Run from the repository root:  ./packaging/appimage/build.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# ── Check / install appimage-builder ─────────────────────────────────────────
if ! command -v appimage-builder &>/dev/null; then
    echo "Installing appimage-builder…"
    pip install appimage-builder
fi

# ── Build ─────────────────────────────────────────────────────────────────────
echo "Building Vice AppImage…"
appimage-builder \
    --recipe packaging/appimage/appimage-builder.yml \
    --skip-test

echo ""
if ls Vice-*.AppImage 1>/dev/null 2>&1; then
    APPIMAGE="$(ls -1 Vice-*.AppImage | tail -1)"
    chmod +x "$APPIMAGE"
    echo "Built: $ROOT/$APPIMAGE"
    echo ""
    echo "To run:    ./$APPIMAGE"
    echo "To install (copy to PATH):"
    echo "  cp $APPIMAGE ~/.local/bin/Vice"
    echo "  chmod +x ~/.local/bin/Vice"
else
    echo "AppImage not found — check build output above."
    exit 1
fi
