#!/usr/bin/env bash
# Vice installer: sets up system dependencies and Python package. 
# Run as your normal user (not root); sudo is used internally where needed.

set -euo pipefail

ALLOW_MIXED_INSTALL=false
for arg in "$@"; do
    case "$arg" in
        --allow-mixed-install)
            ALLOW_MIXED_INSTALL=true
            ;;
        *)
            echo "[vice] Unknown option: $arg" >&2
            echo "Usage: ./install.sh [--allow-mixed-install]" >&2
            exit 1
            ;;
    esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[vice]${NC} $*"; }
warn()    { echo -e "${YELLOW}[vice]${NC} $*"; }
error()   { echo -e "${RED}[vice]${NC} $*" >&2; }
need_cmd() { command -v "$1" &>/dev/null || { error "Required: $1 (not found)"; exit 1; }; }

USER_BIN="$HOME/.local/bin"
VENV_DIR="$HOME/.local/share/vice/venv"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SYSTEMD_DIR/vice.service"

# ── Detect distro / package manager ───────────────────────────────────────────
OS_ID=""
OS_ID_LIKE=""
OS_NAME=""
OS_PRETTY_NAME=""
OS_VARIANT_ID=""
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-}"
    OS_ID_LIKE="${ID_LIKE:-}"
    OS_NAME="${NAME:-}"
    OS_PRETTY_NAME="${PRETTY_NAME:-${NAME:-}}"
    OS_VARIANT_ID="${VARIANT_ID:-}"
fi

is_rpm_ostree_system() {
    local ident
    ident=" ${OS_ID,,} ${OS_ID_LIKE,,} ${OS_NAME,,} ${OS_PRETTY_NAME,,} ${OS_VARIANT_ID,,} "

    if [[ -e /run/ostree-booted ]]; then
        return 0
    fi

    if ! command -v rpm-ostree &>/dev/null; then
        return 1
    fi

    [[ "$ident" == *" bazzite "* \
        || "$ident" == *" silverblue "* \
        || "$ident" == *" kinoite "* \
        || "$ident" == *" atomic "* \
        || "$ident" == *" ublue "* \
        || "$ident" == *" universal blue "* ]]
}

if is_rpm_ostree_system; then
    if [[ -n "$OS_PRETTY_NAME" ]]; then
        info "Detected distro: $OS_PRETTY_NAME"
    fi
    error "Bazzite / Fedora Atomic (rpm-ostree) is not supported by install.sh yet."
    error "This installer uses distro package managers such as dnf to install host dependencies."
    error "On rpm-ostree systems, dnf is not the right install path and package layering can affect system updates."
    error "For now, do not run Vice's install.sh on Bazzite, Silverblue, Kinoite, or other atomic desktops."
    error "A Flatpak or atomic-safe install path is the intended future fix."
    exit 1
fi

detect_package_manager() {
    local ident
    ident=" ${OS_ID,,} ${OS_ID_LIKE,,} "

    if [[ -f /etc/debian_version ]] && command -v apt-get &>/dev/null; then
        echo apt
        return 0
    fi
    if [[ -f /etc/fedora-release || -f /etc/redhat-release || -d /usr/lib/sysimage/rpm || -d /var/lib/rpm ]] && command -v dnf &>/dev/null; then
        echo dnf
        return 0
    fi
    if [[ -f /etc/arch-release ]] && command -v pacman &>/dev/null; then
        echo pacman
        return 0
    fi
    if [[ -f /etc/SuSE-release || -f /etc/products.d/baseproduct ]] && command -v zypper &>/dev/null; then
        echo zypper
        return 0
    fi

    if [[ "$ident" == *" ubuntu "* || "$ident" == *" debian "* ]]; then
        command -v apt-get &>/dev/null && { echo apt; return 0; }
    fi
    if [[ "$ident" == *" fedora "* || "$ident" == *" rhel "* || "$ident" == *" centos "* ]]; then
        command -v dnf &>/dev/null && { echo dnf; return 0; }
    fi
    if [[ "$ident" == *" arch "* || "$ident" == *" manjaro "* || "$ident" == *" cachyos "* ]]; then
        command -v pacman &>/dev/null && { echo pacman; return 0; }
    fi
    if [[ "$ident" == *" opensuse "* || "$ident" == *" suse "* ]]; then
        command -v zypper &>/dev/null && { echo zypper; return 0; }
    fi

    if command -v apt-get &>/dev/null; then echo apt; return 0; fi
    if command -v dnf &>/dev/null; then echo dnf; return 0; fi
    if command -v zypper &>/dev/null; then echo zypper; return 0; fi
    if command -v pacman &>/dev/null; then echo pacman; return 0; fi
    return 1
}

if ! PKG="$(detect_package_manager)"; then
    error "Unsupported distro. Install dependencies manually (see README)."
    exit 1
fi

if [[ -n "$OS_PRETTY_NAME" ]]; then
    info "Detected distro: $OS_PRETTY_NAME"
fi
info "Detected package manager: $PKG"

if [[ "$PKG" == "pacman" ]] && pacman -Q vice-clipper &>/dev/null; then
    if ! $ALLOW_MIXED_INSTALL; then
        error "Detected an existing AUR install of vice-clipper."
        error "Mixed AUR + install.sh deployments are unsupported."
        error "AUR users should update with yay -Syu or paru -Syu."
        error "If you want to switch to the git clone installer, remove the AUR package first: yay -Rns vice-clipper"
        error "If you intentionally want to override this guard, rerun: ./install.sh --allow-mixed-install"
        exit 1
    fi
    warn "Proceeding with a mixed install because --allow-mixed-install was provided."
fi

# ── Detect display server ─────────────────────────────────────────────────────
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
    SESSION=wayland
elif [[ -n "${DISPLAY:-}" ]]; then
    SESSION=x11
else
    warn "No DISPLAY or WAYLAND_DISPLAY detected. Assuming Wayland."
    SESSION=wayland
fi
info "Display server: $SESSION"

# ── Detect compositor ─────────────────────────────────────────────────────────
DE="${XDG_CURRENT_DESKTOP:-}"
if [[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then DE=Hyprland; fi
info "Desktop/compositor: ${DE:-unknown}"

# ── Detect GPU ────────────────────────────────────────────────────────────────
HAS_NVIDIA=false
if command -v nvidia-smi &>/dev/null && nvidia-smi -L &>/dev/null; then
    HAS_NVIDIA=true
    info "NVIDIA GPU detected"
fi

# ── Install system packages ───────────────────────────────────────────────────
pacman_repo_has_package() {
    pacman -Si "$1" >/dev/null 2>&1
}

ensure_pacman_packages_resolvable() {
    local missing=()
    local pkg
    for pkg in "$@"; do
        if ! pacman_repo_has_package "$pkg"; then
            missing+=("$pkg")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Pacman could not resolve required packages: ${missing[*]}"
        error "Your pacman sync databases or enabled repos look broken/stale."
        error "Fix pacman first, then rerun the installer. Usually: sudo pacman -Syu"
        exit 1
    fi
}

install_pkgs_pacman() {
    # Python deps mirror PKGBUILD's depends list so that --system-site-packages
    # venv finds them and pywebview/aiohttp/etc don't fall back to PyPI.
    # Qt/QtWebEngine is preferred over WebKit2GTK, gives the native window a
    # Chromium-based GPU-accelerated engine. webkit2gtk-4.1 is kept as a
    # fallback for systems missing Qt bindings.
    local pkgs=(python python-pip ffmpeg
                python-pywebview python-aiohttp python-click
                python-psutil python-evdev python-tomli-w
                python-pyqt6 python-pyqt6-webengine python-qtpy
                webkit2gtk-4.1 gstreamer gst-plugins-base gst-plugins-good
                xdotool xorg-xprop wmctrl)
    # nvidia-smi answering already proves a driver userspace is installed, so
    # there was nothing to add. Asking for nvidia-utils by name only managed to
    # collide with legacy branches like nvidia-580xx-utils and abort the whole
    # install (#147).
    if $HAS_NVIDIA; then
        if pacman -Qq 2>/dev/null | grep -qE '^nvidia(-[0-9]+xx)?-utils$'; then
            info "NVIDIA driver userspace already installed, leaving it alone"
        else
            pkgs+=(nvidia-utils)
            info "Will install NVIDIA utilities"
        fi
    fi

    # Clipboard tool for "Copy share link", the in-page clipboard API is
    # unreliable in QtWebEngine on http:// origins, so the app shells out.
    if [[ "$SESSION" == "wayland" ]]; then
        pkgs+=(wl-clipboard)
    else
        pkgs+=(xclip)
    fi

    # wf-recorder is best-effort, only needed if a user explicitly sets
    # recording.backend=wf-recorder in their config. GSR is the auto default,
    # installed separately via install_gpu_screen_recorder.
    if [[ "$SESSION" == "wayland" ]] && ! command -v wf-recorder &>/dev/null; then
        if pacman_repo_has_package wf-recorder; then
            pkgs+=(wf-recorder)
        fi
    fi

    ensure_pacman_packages_resolvable "${pkgs[@]}"
    sudo pacman -S --needed --noconfirm "${pkgs[@]}"
}

# The PyPI PyQt6-WebEngine wheel is built without proprietary codecs, so it
# cannot decode H.264 and every clip renders as a grey rectangle inside the
# Vice window (issue #79). Distro WebEngine packages enable those codecs.
# $1 = the package-manager command that would fix it.
warn_webengine_wheel() {
    warn "──────────────────────────────────────────────────────────────────────"
    warn "System Qt WebEngine package not available; falling back to PyPI wheels."
    warn "The PyPI wheel has NO H.264 decoder, so clips will NOT play inside the"
    warn "Vice window. Recording, sharing, and your system video player still work."
    warn "To fix in-app playback, run:"
    warn "    $1"
    warn "and then rerun ./install.sh"
    warn "──────────────────────────────────────────────────────────────────────"
}

install_pkgs_apt() {
    local pkgs=(python3 python3-pip ffmpeg v4l-utils)
    sudo apt-get update -qq
    sudo apt-get install -y "${pkgs[@]}" || {
        error "Failed to install required packages with apt."
        error "Fix apt/dpkg state, then rerun the installer."
        exit 1
    }
    # PyQt6 + QtWebEngine for the Chromium-based native window engine.
    # If the system package isn't available, the Python dep-check later falls
    # back to the PyPI wheel (heavy, and unable to play H.264 clips in-app).
    sudo apt-get install -y python3-pyqt6 python3-pyqt6.qtwebengine python3-qtpy >/dev/null 2>&1 || \
        warn_webengine_wheel "sudo apt install python3-pyqt6 python3-pyqt6.qtwebengine python3-qtpy"
    # Mirror the pacman branch: install Vice's Python runtime deps as system
    # packages so --system-site-packages picks them up. Per-package loop,
    # batched apt-get install aborts on the first NotFound, leaving the rest.
    for p in python3-aiohttp python3-tomli-w python3-click python3-psutil python3-evdev; do
        sudo apt-get install -y "$p" >/dev/null 2>&1 || \
            warn "$p not available via apt; will fall back to PyPI wheel."
    done
    # Clipboard tool for "Copy share link", the in-page clipboard API is
    # unreliable in QtWebEngine on http:// origins, so the app shells out.
    if [[ "$SESSION" == "wayland" ]]; then
        sudo apt-get install -y wl-clipboard >/dev/null 2>&1 || \
            warn "wl-clipboard not available; copying links will offer a manual-copy dialog."
    else
        sudo apt-get install -y xclip >/dev/null 2>&1 || \
            warn "xclip not available; copying links will offer a manual-copy dialog."
    fi
    # Focused-window detection, for game tagging, auto playlists and Discord
    # presence. Without these it silently detects nothing (#152).
    for p in xdotool x11-utils wmctrl; do
        sudo apt-get install -y "$p" >/dev/null 2>&1 || \
            warn "$p not available via apt; game detection will be limited."
    done
    if [[ "$SESSION" == "wayland" ]] && ! command -v wf-recorder &>/dev/null; then
        sudo apt-get install -y wf-recorder >/dev/null 2>&1 || true
    fi
}

# Fedora ships ffmpeg-free and RPM Fusion replaces it with the full ffmpeg.
# The two conflict, so naming either one on a machine that already has the
# other aborts the whole transaction and takes the install with it (#173).
# Vice needs a working ffmpeg, not a particular package.
_dnf_ffmpeg_pkg() {
    if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
        return 0
    fi
    if rpm -q ffmpeg &>/dev/null; then
        printf 'ffmpeg\n'
    else
        printf 'ffmpeg-free\n'
    fi
}

# ffmpeg-free is built without libx264. Recording, trimming and thumbnails do
# not need it, so this is a warning and not a failure.
_warn_if_no_libx264() {
    command -v ffmpeg &>/dev/null || return 0
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q ' libx264 ' && return 0
    warn "This ffmpeg has no libx264 encoder."
    warn "Recording, trimming and thumbnails work. Editor exports, watermarks"
    warn "and share previews need it. Install RPM Fusion's ffmpeg to enable them."
}

install_pkgs_dnf() {
    local pkgs=(python3 python3-pip)
    local ffmpeg_pkg
    ffmpeg_pkg="$(_dnf_ffmpeg_pkg)"
    [[ -n "$ffmpeg_pkg" ]] && pkgs+=("$ffmpeg_pkg")
    if $HAS_NVIDIA; then
        info "Will install NVIDIA utilities when available"
        sudo dnf install -y akmod-nvidia xorg-x11-drv-nvidia >/dev/null 2>&1 || true
    fi
    sudo dnf install -y "${pkgs[@]}" || {
        error "Failed to install required packages with dnf."
        error "Fix dnf repo/package state, then rerun the installer."
        exit 1
    }
    _warn_if_no_libx264
    # PyQt6 + QtWebEngine for the Chromium-based native window engine.
    # One package per command: dnf fails the whole transaction on a single
    # unknown name, which used to drop PyQt6 and QtWebEngine to PyPI wheels
    # just because Fedora spells QtPy with capitals and dnf5 matches case.
    sudo dnf install -y python3-pyqt6 >/dev/null 2>&1 || true
    sudo dnf install -y python3-QtPy >/dev/null 2>&1 || \
        sudo dnf install -y python3-qtpy >/dev/null 2>&1 || \
        warn "python3-QtPy not available via dnf; will fall back to PyPI wheel."
    sudo dnf install -y python3-pyqt6-webengine >/dev/null 2>&1 || \
        warn_webengine_wheel "sudo dnf install python3-pyqt6 python3-pyqt6-webengine python3-QtPy"
    # Mirror the pacman branch: install Vice's Python runtime deps as system
    # packages so --system-site-packages picks them up. Per-package loop,
    # a single missing pkg on RHEL-without-EPEL must not skip the rest.
    for p in python3-aiohttp python3-tomli-w python3-click python3-psutil python3-evdev; do
        sudo dnf install -y "$p" >/dev/null 2>&1 || \
            warn "$p not available via dnf; will fall back to PyPI wheel."
    done
    # Clipboard tool for "Copy share link", the in-page clipboard API is
    # unreliable in QtWebEngine on http:// origins, so the app shells out.
    if [[ "$SESSION" == "wayland" ]]; then
        sudo dnf install -y wl-clipboard >/dev/null 2>&1 || \
            warn "wl-clipboard not available; copying links will offer a manual-copy dialog."
    else
        sudo dnf install -y xclip >/dev/null 2>&1 || \
            warn "xclip not available; copying links will offer a manual-copy dialog."
    fi
    # Focused-window detection, for game tagging, auto playlists and Discord
    # presence. Without these it silently detects nothing (#152).
    for p in xdotool xprop wmctrl; do
        sudo dnf install -y "$p" >/dev/null 2>&1 || \
            warn "$p not available via dnf; game detection will be limited."
    done
    if [[ "$SESSION" == "wayland" ]] && ! command -v wf-recorder &>/dev/null; then
        sudo dnf install -y wf-recorder >/dev/null 2>&1 || true
    fi
}

install_pkgs_zypper() {
    local pkgs=(python3 python3-pip ffmpeg)
    sudo zypper install -y "${pkgs[@]}" || {
        error "Failed to install required packages with zypper."
        error "Fix zypper repo/package state, then rerun the installer."
        exit 1
    }
    # PyQt6 + QtWebEngine for the Chromium-based native window engine.
    sudo zypper install -y python3-qt6 python3-qt6-webengine python3-qtpy >/dev/null 2>&1 || \
        warn_webengine_wheel "sudo zypper install python3-qt6 python3-qt6-webengine python3-qtpy"
    # Mirror the pacman branch: install Vice's Python runtime deps as system
    # packages so --system-site-packages picks them up. Per-package loop,
    # Leap 15.x is missing python3-tomli-w; pip fallback handles it.
    for p in python3-aiohttp python3-tomli-w python3-click python3-psutil python3-evdev; do
        sudo zypper install -y "$p" >/dev/null 2>&1 || \
            warn "$p not available via zypper; will fall back to PyPI wheel."
    done
    # Clipboard tool for "Copy share link", the in-page clipboard API is
    # unreliable in QtWebEngine on http:// origins, so the app shells out.
    if [[ "$SESSION" == "wayland" ]]; then
        sudo zypper install -y wl-clipboard >/dev/null 2>&1 || \
            warn "wl-clipboard not available; copying links will offer a manual-copy dialog."
    else
        sudo zypper install -y xclip >/dev/null 2>&1 || \
            warn "xclip not available; copying links will offer a manual-copy dialog."
    fi
    # Focused-window detection, for game tagging, auto playlists and Discord
    # presence. Without these it silently detects nothing (#152).
    for p in xdotool xprop wmctrl; do
        sudo zypper install -y "$p" >/dev/null 2>&1 || \
            warn "$p not available via zypper; game detection will be limited."
    done
    if [[ "$SESSION" == "wayland" ]] && ! command -v wf-recorder &>/dev/null; then
        sudo zypper install -y wf-recorder >/dev/null 2>&1 || true
    fi
}

# ── gpu-screen-recorder install (mandatory; no fallback) ──────────────────────
# Vice's auto backend is GSR-only. Other recorders (wf-recorder, ffmpeg) only
# fire when the user explicitly sets recording.backend in config, they're
# edge-case overrides, never defaults. install.sh aborts if GSR can't be installed.
GSR_REPO_URL="${VICE_GSR_REPO_URL:-https://repo.dec05eba.com/gpu-screen-recorder}"
# Upstream's own AUR package builds from these per-tag tarballs rather than from
# git. They are a second host for the same code, which the clone alone is not:
# repo.dec05eba.com answered one reporter with a redirect to the cgit browse
# host, which serves no git protocol at all, and the install had nowhere to go
# from there (#182).
GSR_SNAPSHOT_URL="${VICE_GSR_SNAPSHOT_URL:-https://dec05eba.com/snapshot}"
GSR_DEFAULT_REF="5.13.3"
GSR_FFMPEG6_REF="5.12.5"

_gsr_libavutil_major() {
    local version major
    command -v pkg-config &>/dev/null || return 1
    version="$(pkg-config --modversion libavutil 2>/dev/null || true)"
    [[ -n "$version" ]] || return 1
    major="${version%%.*}"
    [[ "$major" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$major"
}

_gsr_select_ref() {
    if [[ -n "${VICE_GSR_REF:-}" ]]; then
        printf '%s\n' "$VICE_GSR_REF"
        return 0
    fi

    local major
    if major="$(_gsr_libavutil_major)"; then
        # Ubuntu 24.04 / Linux Mint 22.x ship FFmpeg 6.1 (libavutil 58).
        # GSR 5.13.x enables Vulkan encoder code that expects newer FFmpeg
        # Vulkan queue-family fields, so pin to the last known FFmpeg 6-safe
        # tag on those systems.
        if (( major < 59 )); then
            printf '%s\n' "$GSR_FFMPEG6_REF"
            return 0
        fi
    fi

    printf '%s\n' "$GSR_DEFAULT_REF"
}

# Fedora ships ffmpeg-free, RPM Fusion replaces it with the full ffmpeg. The
# -devel package has to match whichever one is installed or dnf refuses the
# transaction (issue #115).
_fedora_ffmpeg_devel() {
    if rpm -q ffmpeg &>/dev/null; then
        printf 'ffmpeg-devel\n'
    else
        printf 'ffmpeg-free-devel\n'
    fi
}

# dnf fails the entire transaction when one package name is unavailable, which
# on an unusual arch took the whole GSR build down before meson ever ran. Try
# the batch, then retry one at a time so meson gets to report which dependency
# is actually missing.
_dnf_install_best_effort() {
    if sudo dnf install -y "$@" >/dev/null 2>&1; then
        return 0
    fi
    local pkg
    local failed=()
    for pkg in "$@"; do
        sudo dnf install -y "$pkg" >/dev/null 2>&1 || failed+=("$pkg")
    done
    if (( ${#failed[@]} > 0 )); then
        warn "Could not install: ${failed[*]}"
        warn "The gpu-screen-recorder build will fail if any of these are required."
    fi
}

# Fetch the gpu-screen-recorder source into an empty directory. Tries git
# first, then the tarball, so one host being down or misrouted is no longer the
# end of the install.
_gsr_fetch_source() {
    local ref="$1" dest="$2"
    local tarball="$GSR_SNAPSHOT_URL/gpu-screen-recorder.git.$ref.tar.gz"

    if command -v git &>/dev/null; then
        git clone --depth 1 --branch "$ref" "$GSR_REPO_URL" "$dest" && return 0
        warn "Could not clone $GSR_REPO_URL, trying the source tarball instead."
    else
        warn "git is not installed; using the source tarball."
    fi

    # The archive has no top-level directory, so it lands straight in $dest.
    if command -v curl &>/dev/null; then
        curl -fsSL "$tarball" | tar -xz -C "$dest" && return 0
    elif command -v wget &>/dev/null; then
        wget -qO- "$tarball" | tar -xz -C "$dest" && return 0
    else
        error "Neither curl nor wget is installed, so the tarball fallback cannot run."
    fi

    error "Could not fetch gpu-screen-recorder $ref."
    error "  git:     $GSR_REPO_URL"
    error "  tarball: $tarball"
    error "Check that both are reachable, or point Vice elsewhere with"
    error "VICE_GSR_REPO_URL, VICE_GSR_SNAPSHOT_URL or VICE_GSR_REF."
    return 1
}

_gsr_build_from_source() {
    info "Building gpu-screen-recorder from source (this takes 2-5 minutes)..."
    case "$PKG" in
        apt)
            sudo apt-get install -y git meson ninja-build pkg-config \
                build-essential linux-libc-dev \
                libx11-dev libavfilter-dev libva-dev libcap-dev libdbus-1-dev \
                libvulkan-dev libspa-0.2-dev \
                libpipewire-0.3-dev libx264-dev libxcomposite-dev libxcb-randr0-dev \
                libxdamage-dev libxfixes-dev libpulse-dev libdrm-dev \
                libavcodec-dev libavformat-dev libavutil-dev libswresample-dev \
                libwayland-dev libgl-dev libegl-dev libxrandr-dev || return 1
            ;;
        dnf)
            local ffmpeg_devel
            ffmpeg_devel="$(_fedora_ffmpeg_devel)"
            _dnf_install_best_effort git meson ninja-build pkgconfig gcc-c++ \
                pipewire-devel libX11-devel libXcomposite-devel libXrandr-devel \
                libXdamage-devel libXfixes-devel pulseaudio-libs-devel libdrm-devel \
                libva-devel vulkan-loader-devel libcap-devel dbus-devel \
                "$ffmpeg_devel" wayland-devel mesa-libGL-devel mesa-libEGL-devel
            ;;
        zypper)
            sudo zypper install -y git meson ninja pkg-config pipewire-devel \
                libX11-devel libXcomposite-devel libXrandr-devel libXdamage-devel \
                libXfixes-devel libpulse-devel libdrm-devel ffmpeg-7-libavcodec-devel \
                wayland-devel Mesa-libGL-devel Mesa-libEGL-devel dbus-1-devel || return 1
            ;;
        pacman)
            sudo pacman -S --needed --noconfirm git meson ninja pkgconf || return 1
            ;;
    esac
    local gsr_ref tmpdir
    gsr_ref="$(_gsr_select_ref)"
    if [[ -n "${VICE_GSR_REF:-}" ]]; then
        info "Using gpu-screen-recorder source ref from VICE_GSR_REF: $gsr_ref"
    elif [[ "$gsr_ref" == "$GSR_FFMPEG6_REF" ]]; then
        info "Using gpu-screen-recorder $gsr_ref for FFmpeg 6.x compatibility"
    else
        info "Using gpu-screen-recorder source ref: $gsr_ref"
    fi
    tmpdir=$(mktemp -d -t vice-gsr-XXXXXX)
    _gsr_fetch_source "$gsr_ref" "$tmpdir" || { rm -rf "$tmpdir"; return 1; }
    # Build as the invoking user; only the install step needs root. Running
    # upstream's deprecated install.sh under sudo built everything as root,
    # which left a root-owned build tree in /tmp that the cleanup below
    # could not delete (hundreds of "rm: Permission denied" lines, #84).
    # These meson options match what upstream's installer used; meson's
    # install scripts (run as root) handle the gsr-kms-server capability
    # setup that KMS capture needs.
    (
        cd "$tmpdir" \
        && meson setup build --prefix=/usr --buildtype=release -Dsystemd=true -Dstrip=true \
        && ninja -C build \
        && sudo meson install -C build
    ) || { rm -rf "$tmpdir" 2>/dev/null || sudo rm -rf "$tmpdir"; return 1; }
    rm -rf "$tmpdir" 2>/dev/null || sudo rm -rf "$tmpdir"
}

install_gpu_screen_recorder() {
    if command -v gpu-screen-recorder &>/dev/null; then
        info "gpu-screen-recorder already installed: $(command -v gpu-screen-recorder)"
        return 0
    fi
    info "Installing gpu-screen-recorder (Vice's required recording backend)..."
    case "$PKG" in
        pacman)
            if pacman_repo_has_package gpu-screen-recorder; then
                sudo pacman -S --needed --noconfirm gpu-screen-recorder
            elif command -v yay  &>/dev/null; then
                yay  -S --noconfirm gpu-screen-recorder-git
            elif command -v paru &>/dev/null; then
                paru -S --noconfirm gpu-screen-recorder-git
            else
                _gsr_build_from_source
            fi
            ;;
        dnf)    sudo dnf    install -y gpu-screen-recorder || _gsr_build_from_source ;;
        zypper) sudo zypper install -y gpu-screen-recorder || _gsr_build_from_source ;;
        apt)    _gsr_build_from_source ;;
    esac
    if ! command -v gpu-screen-recorder &>/dev/null; then
        error "Vice requires gpu-screen-recorder. Install failed; see error above."
        error "Manual install: https://git.dec05eba.com/gpu-screen-recorder"
        exit 1
    fi
    info "gpu-screen-recorder installed: $(command -v gpu-screen-recorder)"
}

case "$PKG" in
    pacman) install_pkgs_pacman ;;
    apt)    install_pkgs_apt    ;;
    dnf)    install_pkgs_dnf    ;;
    zypper) install_pkgs_zypper ;;
esac

install_gpu_screen_recorder

ensure_recording_backend() {
    if command -v gpu-screen-recorder &>/dev/null; then
        info "Recording backend ready: gpu-screen-recorder"
        return 0
    fi
    error "gpu-screen-recorder is unavailable on PATH after install. Aborting."
    error "Vice's auto backend requires it. Manual install: https://git.dec05eba.com/gpu-screen-recorder"
    exit 1
}

ensure_recording_backend

# ── Add user to input group ───────────────────────────────────────────────────
if ! groups | grep -q '\binput\b'; then
    info "Adding $USER to the 'input' group (required for global hotkeys)..."
    sudo usermod -aG input "$USER"
    warn "You must log out and back in for the group change to take effect."
    warn "Alternatively: run Vice with 'newgrp input' in your current session."
else
    info "User already in 'input' group."
fi

# ── cloudflared for public share URLs ────────────────────────────────────────
# Vice uses cloudflared for public Discord/external share links by default.
# Without it, share links stay on the local network only.
install_cloudflared() {
    if command -v cloudflared &>/dev/null; then
        info "cloudflared already installed: $(command -v cloudflared)"
        return 0
    fi
    info "Installing cloudflared (for public share links that work outside your WiFi)..."
    # Cloudflare's .deb (and .rpm) postinst symlinks into /usr/local/bin without
    # ensuring it exists; minimal Ubuntu cloud images sometimes ship without it,
    # which causes dpkg to leave cloudflared in a broken half-configured state.
    sudo mkdir -p /usr/local/bin
    local _cf_ok=false
    local _cf_arch
    case "$PKG" in
        pacman)
            if command -v yay &>/dev/null; then
                yay -S --noconfirm cloudflared && _cf_ok=true
            elif command -v paru &>/dev/null; then
                paru -S --noconfirm cloudflared && _cf_ok=true
            else
                warn "AUR helper (yay/paru) not found, cloudflared skipped."
                warn "Install it manually from AUR: https://aur.archlinux.org/packages/cloudflared"
            fi
            ;;
        apt)
            if command -v curl &>/dev/null; then
                curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
                    | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null && \
                echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
                    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null && \
                sudo apt-get update -qq && sudo apt-get install -y cloudflared && _cf_ok=true
            fi
            ;;
        dnf)
            # Cloudflare names the aarch64 asset "arm64"; the x86_64 URL was
            # hardcoded, so this always failed on ARM machines.
            case "$(uname -m)" in
                aarch64|arm64) _cf_arch=arm64  ;;
                armv7l)        _cf_arch=arm    ;;
                *)             _cf_arch=x86_64 ;;
            esac
            sudo dnf install -y "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${_cf_arch}.rpm" \
                && _cf_ok=true || true
            ;;
        *)
            warn "Install cloudflared manually: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
            ;;
    esac
    if ! $_cf_ok; then
        warn "cloudflared not installed. Share links will only work on your own network."
        warn "Install cloudflared later to get public links that your friends can open."
    fi
}

install_cloudflared

# ── Install pywebview system deps ────────────────────────────────────────────
info "Installing pywebview system dependencies (for native window/audio)..."
case "$PKG" in
    pacman)
        sudo pacman -S --needed --noconfirm \
            python-gobject webkit2gtk-4.1 gstreamer gst-plugins-base gst-plugins-good \
            2>/dev/null || \
        sudo pacman -S --needed --noconfirm \
            python-gobject webkit2gtk gstreamer gst-plugins-base gst-plugins-good \
            2>/dev/null || true
        ;;
    apt)
        sudo apt-get install -y python3-gi python3-gi-cairo \
            gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
            libwebkit2gtk-4.1-0 \
            gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
            2>/dev/null || \
        sudo apt-get install -y python3-gi gir1.2-webkit2-4.0 \
            gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
            2>/dev/null || true
        ;;
    dnf)
        sudo dnf install -y \
            python3-gobject webkit2gtk4.1 \
            gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
            2>/dev/null || \
        sudo dnf install -y \
            python3-gobject webkit2gtk3 \
            gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
            2>/dev/null || true
        ;;
    zypper)
        sudo zypper install -y \
            python3-gobject typelib-1_0-WebKit2-4_1 \
            gstreamer gstreamer-plugins-base gstreamer-plugins-good \
            2>/dev/null || true
        ;;
esac

# ── Clipboard tooling (best-effort, for the native-window share-link copy) ───
# The QtWebEngine clipboard API is unreliable on http://localhost, so the
# native window shells out to wl-copy (Wayland) / xclip (X11) instead.
case "$PKG" in
    pacman) sudo pacman -S --needed --noconfirm wl-clipboard xclip 2>/dev/null || true ;;
    apt)    sudo apt-get install -y wl-clipboard xclip 2>/dev/null || true ;;
    dnf)    sudo dnf install -y wl-clipboard xclip 2>/dev/null || true ;;
    zypper) sudo zypper install -y wl-clipboard xclip 2>/dev/null || true ;;
esac

# ── Install Python package ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$USER_BIN"

stop_running_service_for_reinstall() {
    if ! command -v systemctl &>/dev/null; then
        return
    fi
    if ! systemctl --user status &>/dev/null 2>&1; then
        return
    fi
    if systemctl --user is-active --quiet vice.service; then
        info "Stopping existing Vice user service before reinstall..."
        systemctl --user stop vice.service || true
    fi
}

clean_previous_local_install() {
    stop_running_service_for_reinstall

    rm -f "$USER_BIN/vice" "$USER_BIN/vice-app"

    shopt -s nullglob
    local stale_paths=(
        "$HOME"/.local/lib/python*/site-packages/vice
        "$HOME"/.local/lib/python*/site-packages/vice-*.dist-info
        "$HOME"/.local/lib/python*/site-packages/vice.egg-info
    )
    local removed_any=false
    local path
    for path in "${stale_paths[@]}"; do
        rm -rf "$path"
        removed_any=true
    done
    shopt -u nullglob

    if $removed_any; then
        info "Removed previous local Vice Python install artifacts."
    fi
}

install_vice_venv() {
    info "Creating a dedicated virtual environment at $VENV_DIR"

    # On Arch-family systems, pacman installs Python modules for the distro
    # interpreter under /usr/bin. A pyenv/asdf/conda Python earlier in PATH
    # cannot see python-pyqt6/python-pyqt6-webengine even with
    # --system-site-packages, which previously made the installer silently
    # replace the distro WebEngine with PyPI's H.264-less wheel.
    local python_bin
    python_bin="$(command -v python3)"
    if [[ "$PKG" == "pacman" && -x /usr/bin/python3 ]]; then
        python_bin=/usr/bin/python3
    fi
    info "Using Python for Vice venv: $python_bin ($("$python_bin" --version 2>&1))"

    if [[ "$PKG" == "pacman" ]]; then
        if ! "$python_bin" -c 'import PyQt6.QtWebEngineWidgets, qtpy' >/dev/null 2>&1; then
            error "Arch Qt packages are installed but are not importable by $python_bin."
            error "Vice will not replace them with PyPI Qt because that build cannot decode H.264."
            error "Refresh the Arch/CachyOS package set, then rerun the installer:"
            error "  sudo pacman -Syu python python-pyqt6 python-pyqt6-webengine python-qtpy"
            exit 1
        fi
        info "System PyQt6 + QtWebEngine are importable; preserving distro H.264 support."
    fi

    clean_previous_local_install
    rm -rf "$VENV_DIR"
    "$python_bin" -m venv --system-site-packages "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip

    # Two-step install to avoid shadowing system Python packages:
    #   1. Force-reinstall ONLY the vice package itself (no deps). This is the
    #      only thing that changes between vice releases.
    #   2. Fill in any MISSING deps individually. On Arch, `--system-site-
    #      packages` already provides them via pacman (python-pywebview etc.)
    #      so nothing is installed here; on Debian/Fedora where those packages
    #      don't exist, pip fetches them into the venv. Crucially, this path
    #      NEVER shadows a working system package with a newer PyPI wheel,
    #      that regression was the source of Gdk "Protocol error" crashes on
    #      Hyprland + NVIDIA + Wayland (pywebview 6.2.1 vs. system 6.1).
    "$VENV_DIR/bin/pip" install --force-reinstall --no-deps "$SCRIPT_DIR"
    "$VENV_DIR/bin/python" - <<'PY'
import importlib.util, subprocess, sys
# Import name → PyPI name. Import names mirror pyproject.toml dependencies.
# CORE deps are required at runtime, if pip can't install one, abort the
# install with a loud error rather than leaving a silently broken venv.
# OPTIONAL deps power the QtWebEngine native-window backend; if their
# (heavy ~100 MB) wheels fail, vice-app gracefully falls back to GTK.
CORE = {
    "webview":  "pywebview>=5.0",
    "aiohttp":  "aiohttp>=3.9.0",
    "click":    "click>=8.1.7",
    "psutil":   "psutil>=5.9.0",
    "tomli_w":  "tomli-w>=1.0.0",
    "evdev":    "evdev>=1.6.1",
}
OPTIONAL = {
    "PyQt6.QtWebEngineWidgets": "PyQt6-WebEngine>=6.5",
    "qtpy":                     "QtPy>=2.4",
}

def _missing(deps):
    missing = []
    for mod, pypi in deps.items():
        try:
            spec = importlib.util.find_spec(mod)
        except (ImportError, ModuleNotFoundError, AttributeError):
            # find_spec("parent.child") imports the parent package first.
            # A completely absent optional parent (for example PyQt6)
            # is simply a missing dependency, not an installer failure.
            spec = None
        if spec is None:
            missing.append(pypi)
    return missing

core_missing = _missing(CORE)
if core_missing:
    print(f"[vice] Installing CORE deps: {', '.join(core_missing)}")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *core_missing])
    except subprocess.CalledProcessError:
        print(f"[vice] FATAL: pip failed to install required deps: {core_missing}",
              file=sys.stderr)
        print("[vice] Vice cannot run without these. Check your network/proxy "
              "and rerun ./install.sh", file=sys.stderr)
        sys.exit(1)

opt_missing = _missing(OPTIONAL)
if opt_missing:
    print(f"[vice] Installing optional Qt deps: {', '.join(opt_missing)}")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *opt_missing])
    except subprocess.CalledProcessError:
        print("[vice] Warning: optional Qt deps failed to install. "
              "vice-app will fall back to the GTK/WebKit2GTK engine.")

if not core_missing and not opt_missing:
    print("[vice] All Python dependencies already satisfied (no venv shadow).")
PY

    ln -sf "$VENV_DIR/bin/vice" "$USER_BIN/vice"
    ln -sf "$VENV_DIR/bin/vice-app" "$USER_BIN/vice-app"
    info "Installed vice/vice-app shims to $USER_BIN"

    # Sanity-check that every CORE module is reachable from the venv. Catches
    # the failure mode where a system package was found by find_spec but is
    # actually broken/shadowed (wrong Python ABI from --system-site-packages).
    # Better to abort here than let vice-app crash at startup with ModuleNotFoundError.
    local failed=()
    local mod
    for mod in webview aiohttp click psutil tomli_w evdev; do
        "$VENV_DIR/bin/python" -c "import $mod" >/dev/null 2>&1 || failed+=("$mod")
    done
    if [[ ${#failed[@]} -gt 0 ]]; then
        error "Vice venv is broken, these CORE modules are not importable: ${failed[*]}"
        error "Try:  $VENV_DIR/bin/pip install ${failed[*]}"
        error "Then rerun: ./install.sh"
        exit 1
    fi
    info "All core Python modules importable from venv."
}

info "Installing Vice Python package..."
install_vice_venv

# Ensure $USER_BIN is on PATH for the rest of this script.
export PATH="$USER_BIN:$PATH"

# ── Add ~/.local/bin to shell PATH permanently ────────────────────────────────
info "Ensuring ~/.local/bin is on your shell PATH..."

add_to_path_posix() {
    local rc_file="$1"
    if [[ -f "$rc_file" ]] && ! grep -q 'local/bin' "$rc_file" 2>/dev/null; then
        printf '\n# Added by Vice installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc_file"
        info "  Updated $rc_file"
    fi
}

add_to_path_posix "$HOME/.bashrc"
add_to_path_posix "$HOME/.bash_profile"
add_to_path_posix "$HOME/.zshrc"
# Also update .profile if no .bash_profile (sourced by some login managers).
[[ ! -f "$HOME/.bash_profile" ]] && add_to_path_posix "$HOME/.profile"

# Fish uses its own path management, fish_add_path is idempotent.
FISH_CONFIG="$HOME/.config/fish/config.fish"
if command -v fish &>/dev/null || [[ -d "$HOME/.config/fish" ]]; then
    mkdir -p "$(dirname "$FISH_CONFIG")"
    if ! grep -q 'local/bin' "$FISH_CONFIG" 2>/dev/null; then
        printf '\n# Added by Vice installer\nfish_add_path -g $HOME/.local/bin\n' >> "$FISH_CONFIG"
        info "  Updated $FISH_CONFIG (fish)"
    fi
fi

# ── Desktop integration (app icon + launcher entry) ───────────────────────────
info "Installing desktop entry and icon..."
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
APP_DIR="$HOME/.local/share/applications"
mkdir -p "$ICON_DIR" "$APP_DIR"

cp "$SCRIPT_DIR/assets/vice.svg" "$ICON_DIR/vice.svg"

# Write the .desktop file with the *absolute* binary path embedded directly so
# the app launcher doesn't rely on PATH being set correctly at launch time.
VICE_APP_BIN="$USER_BIN/vice-app"
cat > "$APP_DIR/vice.desktop" <<DESKTOP_EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Vice
GenericName=Game Clip Recorder
Comment=Record and share gameplay clips on Linux
Exec=${VICE_APP_BIN}
Icon=vice
Terminal=false
Categories=Game;Video;Recorder;AudioVideo;
Keywords=clip;record;game;capture;gameplay;
StartupNotify=true
StartupWMClass=Vice
DESKTOP_EOF

# Refresh icon/desktop caches (harmless if tools not present).
update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

info "Vice now appears in your app launcher as 'Vice'."

# ── Hyprland keybind hint ─────────────────────────────────────────────────────
if [[ "$DE" == "Hyprland" ]]; then
    echo
    info "Hyprland detected. Vice uses evdev, no compositor keybind config needed."
fi

# ── systemd user service (keeps daemon running even when window is closed) ───
if command -v systemctl &>/dev/null && systemctl --user status &>/dev/null 2>&1; then
    echo
    if [[ -f "$SERVICE_FILE" ]]; then
        info "Refreshing existing Vice user service..."
        ans="y"
    else
        info "A systemd user service keeps the recording daemon running at login"
        info "so Vice is always ready even before you open the window."
        read -r -p "Install Vice daemon as a startup service? [Y/n] " ans
        ans="${ans:-y}"
    fi
    if [[ "${ans,,}" == "y" ]]; then
        mkdir -p "$SYSTEMD_DIR"
        VICE_BIN="$USER_BIN/vice"
        cat >"$SYSTEMD_DIR/vice.service" <<EOF
[Unit]
Description=Vice game clip recorder daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=${VICE_BIN} start --no-open-ui
Restart=on-failure
RestartSec=3
Environment=PATH=${USER_BIN}:/usr/local/bin:/usr/bin:/bin
PassEnvironment=WAYLAND_DISPLAY DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XDG_CURRENT_DESKTOP
# Do not use shell syntax like \${HOME} or \$(id -u) in Environment= lines here.

[Install]
# graphical-session.target alone was not enough: compositors that do not go
# through uwsm never activate it, so the unit was enabled and then never
# started (#139). default.target is always reached by the user manager.
WantedBy=graphical-session.target default.target
EOF
        systemctl --user daemon-reload
        # reenable, not enable: an install that already had the unit keeps its
        # old symlinks otherwise and never picks up the new WantedBy.
        systemctl --user reenable vice.service
        systemctl --user restart vice.service
        info "Vice daemon service enabled, it will start automatically on login."
    fi
fi

# If QtWebEngine ended up resolving to a PyPI wheel inside the venv rather
# than a distro package, in-app clip playback cannot work: the wheel ships
# without an H.264 decoder (issue #79). Repeat the warning here so it is one
# of the last things the user reads, not a line lost mid-scroll.
if "$VENV_DIR/bin/python" - >/dev/null 2>&1 <<'PY'
import importlib.util, sys
spec = importlib.util.find_spec("PyQt6.QtWebEngineCore")
sys.exit(0 if spec and spec.origin and spec.origin.startswith(sys.prefix) else 1)
PY
then
    echo
    case "$PKG" in
        apt)    warn_webengine_wheel "sudo apt install python3-pyqt6 python3-pyqt6.qtwebengine python3-qtpy" ;;
        dnf)    warn_webengine_wheel "sudo dnf install python3-pyqt6 python3-pyqt6-webengine python3-qtpy" ;;
        zypper) warn_webengine_wheel "sudo zypper install python3-qt6 python3-qt6-webengine python3-qtpy" ;;
        pacman) warn_webengine_wheel "sudo pacman -S python-pyqt6 python-pyqt6-webengine python-qtpy" ;;
    esac
fi

echo
info "Installation complete!"
info ""
info "  • Open Vice:      click 'Vice' in your app launcher, or run: vice-app"
info "  • CLI:            vice --help"
info "  • Clip hotkey:    F9 (change in Settings)"
info "  • Build AppImage: ./packaging/appimage/build.sh"
info "  • Build Flatpak:  ./packaging/flatpak/build.sh"
info "  • Uninstall:      vice uninstall"
info ""
warn "Restart your terminal (or run 'exec \$SHELL') for PATH changes to take effect."
warn "On fish: run 'exec fish' or open a new terminal window."
