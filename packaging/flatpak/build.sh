#!/usr/bin/env bash
# Build and install the Vice Flatpak locally.
# For Flathub submission, submit the manifest to https://github.com/flathub/flathub

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="$SCRIPT_DIR/com.vice.Vice.yml"
BUILD_DIR="$ROOT/build/flatpak"

# ── Check deps ────────────────────────────────────────────────────────────────
for cmd in flatpak flatpak-builder; do
    command -v "$cmd" &>/dev/null || {
        echo "Missing: $cmd"
        echo "Install: sudo apt install flatpak flatpak-builder   # Debian/Ubuntu"
        echo "         sudo pacman -S flatpak flatpak-builder      # Arch"
        exit 1
    }
done

# ── Add freedesktop remote if missing ────────────────────────────────────────
flatpak remote-add --user --if-not-exists flathub \
    https://flathub.org/repo/flathub.flatpakrepo 2>/dev/null || true

# ── Install runtime ───────────────────────────────────────────────────────────
echo "Installing Flatpak runtime (org.freedesktop.Platform 23.08)…"
flatpak install --user --assumeyes flathub \
    org.freedesktop.Platform//23.08 \
    org.freedesktop.Sdk//23.08 \
    org.freedesktop.Sdk.Extension.python311//23.08 2>/dev/null || true

# ── Build ─────────────────────────────────────────────────────────────────────
echo "Building Vice Flatpak…"
mkdir -p "$BUILD_DIR"
flatpak-builder \
    --force-clean \
    --user \
    --install \
    "$BUILD_DIR/build" \
    "$MANIFEST"

echo ""
echo "Vice Flatpak installed!"
echo "Launch: flatpak run com.vice.Vice"
echo ""
echo "It will appear in your app launcher as 'Vice'."
echo ""
echo "To uninstall: flatpak uninstall com.vice.Vice"
