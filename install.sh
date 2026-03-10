#!/usr/bin/env bash
# Vice installer — sets up system dependencies and Python package.
# Run as your normal user (not root); sudo is used internally where needed.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[vice]${NC} $*"; }
warn()    { echo -e "${YELLOW}[vice]${NC} $*"; }
error()   { echo -e "${RED}[vice]${NC} $*" >&2; }
need_cmd() { command -v "$1" &>/dev/null || { error "Required: $1 (not found)"; exit 1; }; }

# ── Detect package manager ────────────────────────────────────────────────────
if   command -v pacman  &>/dev/null; then PKG=pacman
elif command -v apt-get &>/dev/null; then PKG=apt
elif command -v dnf     &>/dev/null; then PKG=dnf
elif command -v zypper  &>/dev/null; then PKG=zypper
else
    error "Unsupported distro. Install dependencies manually (see README)."
    exit 1
fi
info "Detected package manager: $PKG"

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
install_pkgs_pacman() {
    local pkgs=(python python-pip ffmpeg)
    if $HAS_NVIDIA; then
        pkgs+=(nvidia-utils)
        info "Will install NVIDIA utilities"
    fi

    # GPU screen recorder (AUR)
    if ! command -v gpu-screen-recorder &>/dev/null; then
        info "gpu-screen-recorder not found."
        if command -v yay &>/dev/null; then
            info "Installing gpu-screen-recorder from AUR via yay..."
            yay -S --noconfirm gpu-screen-recorder-git
        elif command -v paru &>/dev/null; then
            paru -S --noconfirm gpu-screen-recorder-git
        else
            warn "No AUR helper found. Install gpu-screen-recorder manually:"
            warn "  https://git.dec05eba.com/gpu-screen-recorder"
            warn "Falling back to wf-recorder + ffmpeg."
            if [[ "$SESSION" == "wayland" ]]; then
                pkgs+=(wf-recorder)
            fi
        fi
    fi

    sudo pacman -S --needed --noconfirm "${pkgs[@]}"
}

install_pkgs_apt() {
    local pkgs=(python3 python3-pip ffmpeg v4l-utils)
    if [[ "$SESSION" == "wayland" ]] && ! command -v wf-recorder &>/dev/null; then
        pkgs+=(wf-recorder) || true
    fi
    sudo apt-get update -qq
    sudo apt-get install -y "${pkgs[@]}" || {
        warn "Some packages failed to install — check output above."
    }
    if ! command -v gpu-screen-recorder &>/dev/null; then
        warn "gpu-screen-recorder is not in apt repos."
        warn "Build from source for best experience:"
        warn "  https://git.dec05eba.com/gpu-screen-recorder"
    fi
}

install_pkgs_dnf() {
    local pkgs=(python3 python3-pip ffmpeg)
    if [[ "$SESSION" == "wayland" ]]; then
        pkgs+=(wf-recorder) || true
    fi
    sudo dnf install -y "${pkgs[@]}" || warn "Some packages may not be available."
}

install_pkgs_zypper() {
    local pkgs=(python3 python3-pip ffmpeg)
    sudo zypper install -y "${pkgs[@]}"
}

case "$PKG" in
    pacman) install_pkgs_pacman ;;
    apt)    install_pkgs_apt    ;;
    dnf)    install_pkgs_dnf    ;;
    zypper) install_pkgs_zypper ;;
esac

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
# Falls back to SSH/serveo.net automatically if cloudflared is unavailable.
if ! command -v cloudflared &>/dev/null; then
    info "Installing cloudflared (for public share links that work outside your WiFi)..."
    _cf_ok=false
    case "$PKG" in
        pacman)
            if command -v yay &>/dev/null; then
                yay -S --noconfirm cloudflared && _cf_ok=true
            elif command -v paru &>/dev/null; then
                paru -S --noconfirm cloudflared && _cf_ok=true
            else
                warn "AUR helper (yay/paru) not found — cloudflared skipped."
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
            sudo dnf install -y 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-x86_64.rpm' \
                && _cf_ok=true || true
            ;;
        *)
            warn "Install cloudflared manually: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
            ;;
    esac
    if ! $_cf_ok; then
        warn "cloudflared not installed. Vice will use SSH/serveo.net as a fallback for public links."
        warn "You can install cloudflared later for a more reliable tunnel."
    fi
fi

# ── Install pywebview system deps ────────────────────────────────────────────
info "Installing pywebview system dependencies (for native window)..."
case "$PKG" in
    pacman)
        sudo pacman -S --needed --noconfirm python-gobject webkit2gtk-4.1 2>/dev/null || \
        sudo pacman -S --needed --noconfirm python-gobject webkit2gtk 2>/dev/null || true
        ;;
    apt)
        sudo apt-get install -y python3-gi python3-gi-cairo \
            gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
            libwebkit2gtk-4.1-0 2>/dev/null || \
        sudo apt-get install -y python3-gi gir1.2-webkit2-4.0 2>/dev/null || true
        ;;
    dnf)
        sudo dnf install -y python3-gobject webkit2gtk4.1 2>/dev/null || \
        sudo dnf install -y python3-gobject webkit2gtk3 2>/dev/null || true
        ;;
    zypper)
        sudo zypper install -y python3-gobject typelib-1_0-WebKit2-4_1 2>/dev/null || true
        ;;
esac

# ── Install Python package ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_BIN="$HOME/.local/bin"
VENV_DIR="$HOME/.local/share/vice/venv"
mkdir -p "$USER_BIN"

install_vice_user_site() {
    if command -v python3 &>/dev/null; then
        if python3 -m pip install --user "$SCRIPT_DIR"; then
            return 0
        fi
    else
        if pip install --user "$SCRIPT_DIR"; then
            return 0
        fi
    fi
    return 1
}

is_externally_managed_python() {
    python3 -c 'import sysconfig; from pathlib import Path; raise SystemExit(0 if (Path(sysconfig.get_path("stdlib") or "") / "EXTERNALLY-MANAGED").exists() else 1)' >/dev/null 2>&1
}

install_vice_venv() {
    info "Creating a dedicated virtual environment at $VENV_DIR"
    rm -rf "$VENV_DIR"
    python3 -m venv --system-site-packages "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    "$VENV_DIR/bin/pip" install "$SCRIPT_DIR"

    ln -sf "$VENV_DIR/bin/vice" "$USER_BIN/vice"
    ln -sf "$VENV_DIR/bin/vice-app" "$USER_BIN/vice-app"
    info "Installed vice/vice-app shims to $USER_BIN"
}

info "Installing Vice Python package..."
if [[ "$PKG" == "pacman" ]] || is_externally_managed_python; then
    info "Detected externally-managed Python. Using isolated virtual environment install."
    install_vice_venv
elif ! install_vice_user_site; then
    warn "User-site pip install failed. Falling back to an isolated virtual environment install."
    install_vice_venv
fi

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

# Fish uses its own path management — fish_add_path is idempotent.
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
    info "Hyprland detected. Vice uses evdev — no compositor keybind config needed."
fi

# ── systemd user service (keeps daemon running even when window is closed) ───
if command -v systemctl &>/dev/null && systemctl --user status &>/dev/null 2>&1; then
    echo
    info "A systemd user service keeps the recording daemon running at login"
    info "so Vice is always ready even before you open the window."
    read -r -p "Install Vice daemon as a startup service? [Y/n] " ans
    ans="${ans:-y}"
    if [[ "${ans,,}" == "y" ]]; then
        SYSTEMD_DIR="$HOME/.config/systemd/user"
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
Environment=HOME=${HOME}
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u)
Environment=PATH=${USER_BIN}:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=graphical-session.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now vice.service
        info "Vice daemon service enabled — it will start automatically on login."
    fi
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
