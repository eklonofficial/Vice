#!/usr/bin/env bash
# Builds the Vice Flatpak and leaves Vice.flatpak in the working directory.
#
# With FLATPAK_PUBLISH=1 it also signs the build into the Flatpak repo checked
# out at $SITE_DIR (the splitshotz/vice-flatpak Pages site) and writes the
# .flatpakref and .flatpakrepo that point at it. The bundle then carries the
# repo's address, so anyone who installed it gets updates with flatpak update.
#
# Runs in the flatpak-github-actions KDE container, in CI and by hand.
set -euo pipefail

APP_ID=io.github.splitshotz.Vice
MANIFEST=packaging/flatpak/$APP_ID.yml
BRANCH=stable
SITE_URL=${SITE_URL:-https://splitshotz.github.io/vice-flatpak}
FLATHUB=https://dl.flathub.org/repo/flathub.flatpakrepo
BUILD_DIR=${BUILD_DIR:-build/flatpak-app}
STATE_DIR=${STATE_DIR:-.flatpak-builder}

flatpak remote-add --if-not-exists flathub "$FLATHUB"

# build REPO [export options]: build, check, and only then export, so a
# build that fails the check never reaches the repo.
build() {
    local repo=$1
    shift
    flatpak-builder --force-clean --disable-rofiles-fuse --install-deps-from=flathub \
        --state-dir="$STATE_DIR" --default-branch="$BRANCH" "$BUILD_DIR" "$MANIFEST"
    smoke
    mkdir -p "$(dirname "$repo")"
    flatpak build-export "$@" "$repo" "$BUILD_DIR" "$BRANCH"
}

# Everything Vice imports or runs has to be in the build, not just compile.
smoke() {
    flatpak build "$BUILD_DIR" sh -c '
        set -e
        python3 -c "
import gi, cairo, evdev, aiohttp, webview, qtpy, vice.main
gi.require_version(\"Gtk\", \"3.0\")
gi.require_version(\"GtkLayerShell\", \"0.1\")
from gi.repository import Gtk, GtkLayerShell
from PyQt6.QtWebEngineWidgets import QWebEngineView
"
        for tool in xdotool xprop wmctrl wl-copy xclip ffmpeg ffprobe paplay flatpak-spawn vice vice-app; do
            command -v "$tool" >/dev/null || { echo "missing in the Flatpak: $tool"; exit 1; }
        done
        test -x /app/bin/QtWebEngineProcess
    '
}

if [[ "${FLATPAK_PUBLISH:-0}" != 1 ]]; then
    build build/flatpak-repo
    flatpak build-bundle --runtime-repo="$FLATHUB" build/flatpak-repo Vice.flatpak "$APP_ID" "$BRANCH"
    exit 0
fi

: "${SITE_DIR:?SITE_DIR must point at the vice-flatpak checkout}"
: "${GPG_KEY_ID:?GPG_KEY_ID is required to publish}"
: "${GPG_PRIVATE_KEY:?GPG_PRIVATE_KEY is required to publish}"

export GNUPGHOME
GNUPGHOME=$(mktemp -d)
trap 'rm -rf "$GNUPGHOME"' EXIT
gpg --batch --import <<<"$GPG_PRIVATE_KEY"

REPO="$SITE_DIR/repo"
# A repo checked out of git has lost its empty directories.
if [[ -f "$REPO/config" ]]; then
    mkdir -p "$REPO"/refs/{heads,remotes,mirrors} "$REPO"/tmp/cache "$REPO"/extensions "$REPO"/state
fi
build "$REPO" --gpg-sign="$GPG_KEY_ID"
# Two releases back is enough to update from, and keeps the site well under
# the GitHub Pages size limit.
flatpak build-update-repo --gpg-sign="$GPG_KEY_ID" --generate-static-deltas \
    --prune --prune-depth=2 --title=Vice --default-branch="$BRANCH" "$REPO"
flatpak build-bundle --gpg-sign="$GPG_KEY_ID" --repo-url="$SITE_URL/repo" \
    --runtime-repo="$FLATHUB" "$REPO" Vice.flatpak "$APP_ID" "$BRANCH"

KEY=$(gpg --export "$GPG_KEY_ID" | base64 -w0)
gpg --export --armor "$GPG_KEY_ID" > "$SITE_DIR/vice.gpg"
touch "$SITE_DIR/.nojekyll"

cat > "$SITE_DIR/vice.flatpakref" <<EOF
[Flatpak Ref]
Name=$APP_ID
Branch=$BRANCH
Title=Vice
Url=$SITE_URL/repo
RuntimeRepo=$FLATHUB
SuggestRemoteName=vice
IsRuntime=false
GPGKey=$KEY
EOF

cat > "$SITE_DIR/vice.flatpakrepo" <<EOF
[Flatpak Repo]
Title=Vice
Url=$SITE_URL/repo
Homepage=https://github.com/splitshotz/Vice
Comment=Instant replay clips for Linux
GPGKey=$KEY
EOF

cat > "$SITE_DIR/index.html" <<EOF
<!doctype html>
<meta charset="utf-8">
<title>Vice Flatpak</title>
<h1>Vice Flatpak (beta)</h1>
<p>Install with:</p>
<pre>flatpak install --user $SITE_URL/vice.flatpakref</pre>
<p>Vice installs gpu-screen-recorder itself the first time it opens. See
<a href="https://github.com/splitshotz/Vice">the Vice repository</a> for details.</p>
EOF
