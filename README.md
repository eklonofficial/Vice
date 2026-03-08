<p align="center">
  <img src="assets/vice.svg" width="96" alt="Vice icon"/>
</p>

# Vice

**Medal.tv-style game clip recorder for Linux.**
Press a hotkey — instantly save the last 15 seconds of gameplay.

---

## Quick Start

```bash
# To install:
git clone https://github.com/eklonofficial/Vice
cd Vice
./install.sh

# To update:
cd Vice
git pull
./install.sh
```

After the installer finishes, **restart your terminal**, then launch **Vice** from your app launcher (or run `vice-app`).

> If the `vice` command isn't found after restart, run `exec $SHELL` (or `exec fish` on fish shell).

### How it works

| Action | What happens |
|---|---|
| **F9** | Save the last 15 seconds as a clip |
| **Double-tap F9** | Start a session recording (records continuously until you double-tap again) |
| **F9 during session** | Mark a highlight at that timestamp — it shows up in the viewer once the session ends |
| **Double-tap F9 again** | Stop the session and save the full recording with your highlights baked in |
| **Click a thumbnail** | Open the video viewer — use ← / → to navigate, H to add a highlight, Esc to close |
| **Share button** | Copy a public link that works outside your WiFi and embeds in Discord |
| **Trim** | Visually trim a clip in-place |
| **Settings → Hotkeys** | Rebind the clip key — click the button and press any key |

Clips are saved to `~/Videos/Vice/`. Closing the window keeps the daemon running — reopen from your launcher any time.

---

## What It Does

| | |
|---|---|
| **Instant clips** | Rolling buffer saves the last 15 seconds on a single keypress |
| **Session recording** | Double-tap to record continuously for as long as you want |
| **Hotkey highlights** | Tap the clip key during a session to mark timestamps — no need to edit later |
| **Clip gallery** | Browse, watch, rename, trim, and share clips |
| **Share links** | Public URLs via Cloudflare Tunnel — embeds in Discord |
| **Viewer highlights** | Press H in the viewer to mark timestamps, colour-code and rename them |
| **Themes** | Blue, purple, green, red, or orange accent colour |

---

## Why Vice?

OBS has a replay buffer. So why use Vice?

| | OBS Replay Buffer | Vice |
|---|---|---|
| **Setup** | Open OBS, configure scene, enable replay buffer, press hotkey | Install once, press F9 |
| **Always on** | OBS must be open and a scene active | Daemon runs silently in the background |
| **GPU overhead** | OBS encodes a full scene continuously | `gpu-screen-recorder` captures the compositor's framebuffer directly — near-zero overhead |
| **Hotkey scope** | OBS window must be focused, or use global hotkey plugin | Works globally on any compositor via evdev |
| **Share links** | Manual upload | Built-in public URLs, Discord embeds |
| **Clip management** | None | Gallery, viewer, trim, highlights, rename |

**Performance:** Vice uses `gpu-screen-recorder` by default, which hooks into NVIDIA's NVENC or AMD/Intel VAAPI at the driver level — the same approach ShadowPlay uses. CPU usage is typically under 1%. On hardware that doesn't support it, Vice falls back to `wf-recorder` or `ffmpeg`.

---

## CLI Reference

```
vice start          Start the recording daemon
vice stop           Stop the daemon
vice clip           Save a clip right now
vice status         Show daemon status, backend, and share URL
vice ui             Open the web UI in your browser
vice clips          List saved clips
vice config         Print current config
vice open-config    Open config in $EDITOR
vice list-keys      Show valid hotkey names (KEY_F9, KEY_INSERT, …)
vice uninstall      Remove Vice cleanly
```

---

## Credits

Created by **Andrew Marin** — [github.com/eklonofficial](https://github.com/eklonofficial)

---

## Details

### Compatibility

| Environment | Status |
|---|---|
| Hyprland (Wayland) | ✅ |
| GNOME (Wayland) | ✅ |
| KDE Plasma (Wayland) | ✅ |
| sway (Wayland) | ✅ |
| Any X11 WM | ✅ |
| NVIDIA GPU | ✅ NVENC hardware encoding |
| AMD / Intel GPU | ✅ VAAPI hardware encoding |
| Software encoding | ✅ libx264 fallback |
| fish / bash / zsh | ✅ PATH configured automatically |

### Recording Backends

Vice picks the best available backend automatically:

| Backend | Wayland | X11 | NVIDIA | AMD/Intel |
|---|---|---|---|---|
| `gpu-screen-recorder` | ✅ | ✅ | ✅ NVENC | ✅ VAAPI |
| `wf-recorder` | ✅ | ❌ | ✅ | ✅ |
| `ffmpeg x11grab` | ❌ | ✅ | ✅ | ✅ |

Vice uses **evdev** to read hotkeys directly from `/dev/input/` — works on any compositor, no keybind config needed.

### Configuration

`~/.config/vice/config.toml` — created on first run, all settings editable in the GUI:

```toml
[recording]
buffer_duration = 120   # seconds kept in rolling buffer
clip_duration   = 15    # seconds saved per clip
fps             = 60
encoder         = "auto"   # auto | h264_nvenc | libx264 | hevc_nvenc | h264_vaapi
backend         = "auto"   # auto | gsr | wf-recorder | ffmpeg
capture_audio   = true
apply_watermark = false   # enable only if you want watermark text on exports

[hotkeys]
clip = "KEY_F9"

[output]
directory       = "~/Videos/Vice"

[sharing]
enabled           = true
port              = 8765
cloudflare_tunnel = true
```

### Alternative Install Methods

**AppImage (portable — no install needed):**
```bash
chmod +x Vice-1.0.0-x86_64.AppImage
./Vice-1.0.0-x86_64.AppImage
```

**Flatpak (build yourself):**
```bash
./packaging/flatpak/build.sh
flatpak run com.vice.Vice
```

### Troubleshooting

**`vice: command not found` after install**
> Restart your terminal or run `exec $SHELL`. On fish: `exec fish`.

**App launcher icon does nothing**
> Check the log: `cat ~/.local/share/vice/vice-app.log`
> Common cause: recording backend not found. Install `gpu-screen-recorder`, `wf-recorder`, or `ffmpeg`.

**Hotkey not working**
> You may need to be in the `input` group:
> ```bash
> sudo usermod -aG input $USER
> newgrp input
> ```

**Share link only works on my WiFi**
> Make sure `cloudflare_tunnel = true` in Settings. Install `cloudflared` if it's missing.

### Uninstall

```bash
vice uninstall
```

---

## License

[GPL-3.0](LICENSE)
