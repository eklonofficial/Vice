import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
# Spelled as an escape so this file does not itself trip the sweep guard below.
EM_DASH = "\u2014"
UI_INDEX = REPO_ROOT / "vice" / "ui" / "index.html"
HOME_JS = REPO_ROOT / "vice" / "ui" / "scripts" / "home.js"
SETTINGS_CSS = REPO_ROOT / "vice" / "ui" / "styles" / "settings.css"
HOME_CSS = REPO_ROOT / "vice" / "ui" / "styles" / "home.css"
README = REPO_ROOT / "README.md"


class UIStaticCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = UI_INDEX.read_text()
        cls.home_js = HOME_JS.read_text()
        cls.readme = README.read_text()

    def test_tutorial_reflects_current_workflows(self) -> None:
        copy = self.index + "\n" + self.home_js

        self.assertIn("Double-tap to start or stop a full recording", copy)
        self.assertIn("Tap once during a session to mark a highlight", copy)
        self.assertIn("trim the best moment", copy)
        self.assertIn("share a link", copy)
        self.assertIn("Vice keeps recording", copy)
        self.assertIn("Discord Rich Presence is on by default", copy)

    def test_discord_copy_no_longer_says_off_by_default(self) -> None:
        copy = self.index + "\n" + self.readme

        self.assertIn("On by default", copy)
        self.assertNotIn("off by default", copy.lower())

    def test_dark_color_scheme_declared_for_native_dropdowns(self) -> None:
        # Without these, native <select> popups render white-on-white on
        # KDE Plasma 6 / Wayland (#85).
        self.assertIn('<meta name="color-scheme" content="dark">', self.index)
        css = SETTINGS_CSS.read_text()
        self.assertIn("select option", css)
        self.assertIn("MenuText", css)

    def test_manual_copy_modal_exists(self) -> None:
        self.assertIn('id="manual-copy-modal"', self.index)
        self.assertIn('id="manual-copy-text"', self.index)

    def test_software_render_mode_drops_heavy_effects(self) -> None:
        # vice-app appends sw=1 when relaunched with software compositing;
        # the UI must drop backdrop blurs and ambient effects in that mode.
        base_css = (REPO_ROOT / "vice" / "ui" / "styles" / "base.css").read_text()
        state_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "state.js").read_text()
        init_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "init.js").read_text()

        self.assertIn(".perf-low", base_css)
        self.assertIn("backdrop-filter: none", base_css)
        self.assertIn("IS_SOFTWARE_RENDER", state_js)
        self.assertIn("perf-low", init_js)

    def test_encoder_dropdown_offers_av1(self) -> None:
        self.assertIn('value="av1_nvenc"', self.index)
        self.assertIn('value="av1_vaapi"', self.index)

    def test_pick_keeps_unknown_select_values(self) -> None:
        # A hand-edited config value (e.g. encoder = "av1" before it was in
        # the dropdown) must not blank the select and get wiped on save (#109).
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("(custom)", settings_js)

    def test_duration_sliders_allow_thirty_minutes(self) -> None:
        self.assertIn('id="s-dur" min="5" max="1800"', self.index)
        self.assertIn('id="s-buf" min="30" max="1800"', self.index)

    def test_replay_storage_setting_exists(self) -> None:
        self.assertIn('id="s-replay-storage"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("gsr_replay_storage", settings_js)
        self.assertIn("updateBufferNote", settings_js)

    def test_desktop_audio_select_groups_sources_and_warns_on_mic(self) -> None:
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("optgroup", settings_js)
        self.assertIn("onDesktopSourceChange", settings_js)
        self.assertIn("microphone input", settings_js)

    def test_volume_sliders_exist_for_desktop_and_mic(self) -> None:
        self.assertIn('id="s-vol-desktop"', self.index)
        self.assertIn('id="s-vol-mic"', self.index)
        # Mic capture must be toggleable from the Audio settings too, or the
        # mic volume slider is undiscoverable.
        self.assertIn('id="settings-mic-toggle"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("desktop_volume", settings_js)
        self.assertIn("microphone_volume", settings_js)
        self.assertIn("syncVolumeRows", settings_js)

    def test_clip_card_handlers_escape_the_slug_for_javascript(self) -> None:
        """#138: slugs are filenames, so they can contain an apostrophe. escAttr
        alone leaves it decoded back into the inline handler's JS string, which
        killed every button on the card."""
        helpers_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "helpers.js").read_text()
        self.assertIn("function escArg", helpers_js)

        clips_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "clips.js").read_text()
        for handler in ("openViewer", "startRename", "delClip", "revealClip",
                        "copyClipFile", "openTrim", "copyLink", "openPlaylistMenu",
                        "onClipDragStart"):
            self.assertRegex(
                clips_js, rf"{handler}\((?:event, )?'\$\{{(?:arg|escArg\()",
                f"{handler} still interpolates an unescaped slug",
            )
        # The old client-side guard is gone; the server normalises instead.
        self.assertNotIn("Clip name cannot contain spaces", clips_js)

    def test_colour_depth_setting_is_wired(self) -> None:
        self.assertIn('id="s-depth"', self.index)
        self.assertIn('value="10"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("color_depth", settings_js)

    def test_follow_mouse_display_setting_is_wired(self) -> None:
        self.assertIn('id="s-follow-mouse"', self.index)
        self.assertIn('id="s-follow-mouse-note"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("follow_mouse_display", settings_js)
        self.assertIn("onFollowMouseChange", settings_js)
        # The picker and follow-the-pointer are alternatives, not both at once.
        self.assertIn("display.disabled = toggle.checked", settings_js)

    def test_mono_microphone_setting_is_wired(self) -> None:
        self.assertIn('id="s-mic-mono"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("microphone_mono", settings_js)

    def test_hardware_video_decode_setting_is_wired(self) -> None:
        self.assertIn('id="s-hw-decode"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("hardware_video_decode", settings_js)
        # It lives in its own config section, not under recording.
        self.assertIn("ui: {", settings_js)

    def test_recorder_failure_banner_is_wired(self) -> None:
        # The daemon stays up when capture will not start, so the window has to
        # be able to say why (#156).
        self.assertIn('id="recorder-banner"', self.index)
        self.assertIn('id="recorder-banner-detail"', self.index)
        self.assertIn('id="cpu-fallback-banner"', self.index)
        status_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "status.js").read_text()
        ws_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "ws.js").read_text()
        self.assertIn("function applyRecorderState", status_js)
        self.assertIn("recorder_error", status_js)
        # Both the poll and the live push have to drive it.
        self.assertIn("applyRecorderState(d)", status_js)
        self.assertIn("applyRecorderState(msg)", ws_js)

    def test_gpu_codec_fallback_banner_is_wired(self) -> None:
        # Still GPU encoding, just not the codec that was asked for, so it
        # needs its own line rather than the CPU one (#156).
        self.assertIn('id="gpu-codec-banner"', self.index)
        status_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "status.js").read_text()
        self.assertIn("codec_fallback", status_js)
        base_css = (REPO_ROOT / "vice" / "ui" / "styles" / "base.css").read_text()
        self.assertIn("#gpu-codec-banner", base_css)

    def test_saved_display_survives_when_the_backend_stops_listing_it(self) -> None:
        # Falling back to Auto here wrote display=null on the next save and
        # destroyed a value set by hand, which is the only way to reach a
        # monitor gpu-screen-recorder will not enumerate (#160).
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        render = settings_js.split("function renderDisplayOptions")[1].split("\nasync function")[0]
        self.assertIn("(saved)", render)
        self.assertIn("el.value = desired", render)
        # The saved id must be added as an option before it is selected, or
        # the select silently refuses the value.
        self.assertLess(render.index("el.add(new Option(`${desired} (saved)`"),
                        render.rindex("el.value = desired"))

    def test_audio_track_conflict_warning_is_wired(self) -> None:
        # With desktop audio off the recorder keeps only microphone sources,
        # which is the whole of #137, and nothing said so.
        self.assertIn('id="s-track-warning"', self.index)
        self.assertIn('onchange="syncTrackWarning()"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("function syncTrackWarning", settings_js)
        self.assertIn("function tracksLostWithoutDesktopAudio", settings_js)
        settings_css = (REPO_ROOT / "vice" / "ui" / "styles" / "settings.css").read_text()
        self.assertIn(".track-warning", settings_css)

    def test_unreadable_clip_is_marked_in_the_gallery(self) -> None:
        # A clip ffmpeg cannot read used to be listed as an ordinary 0:00
        # clip with no thumbnail, which is #154 as the reporter saw it.
        clips_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "clips.js").read_text()
        self.assertIn("c.unreadable", clips_js)
        self.assertIn("unreadable_reason", clips_js)
        self.assertIn("clip-broken-badge", clips_js)
        clips_css = (REPO_ROOT / "vice" / "ui" / "styles" / "clips.css").read_text()
        self.assertIn(".clip-broken-badge", clips_css)
        self.assertIn(".clip-broken-note", clips_css)

    def test_custom_notification_sounds_are_wired(self) -> None:
        for field in ("s-snd-clip", "s-snd-clip-failed", "s-snd-session-start",
                      "s-snd-session-end", "s-snd-highlight"):
            self.assertIn(f'id="{field}"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        for key in ("clip_sound", "clip_failed_sound", "session_start_sound",
                    "session_end_sound", "highlight_sound"):
            self.assertIn(key, settings_js)

    def test_hotkey_blocklist_setting_is_wired(self) -> None:
        self.assertIn('id="s-hotkey-blocklist"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("disable_while_focused", settings_js)

    def test_trim_preview_loop_is_wired(self) -> None:
        self.assertIn('id="trim-preview-btn"', self.index)
        trim_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "trim.js").read_text()
        self.assertIn("toggleTrimPreview", trim_js)
        self.assertIn("onTrimTimeUpdate", trim_js)
        init_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "init.js").read_text()
        self.assertIn("onTrimTimeUpdate", init_js)
        self.assertIn("onTrimVideoEnded", init_js)

    def test_resolution_and_fps_allow_custom_values(self) -> None:
        self.assertIn('value="custom"', self.index)
        self.assertIn('id="s-res-custom"', self.index)
        for fps in ("50", "120", "144"):
            self.assertIn(f'value="{fps}"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("resolvedResolution", settings_js)

    def test_settings_rail_covers_every_section(self) -> None:
        import re
        rails = re.findall(r'data-rail="(\w+)"', self.index)
        sections = re.findall(r'data-section="(\w+)"', self.index)
        self.assertEqual(rails, sections)
        # Audio settings live in their own section instead of being buried
        # at the bottom of Recording.
        self.assertIn("audio", rails)
        self.assertEqual(rails[0], "recording")

    def test_ambient_motion_uses_css_animation_not_js_timer(self) -> None:
        # A perpetual JS style-mutation loop leaked renderer memory while
        # the window sat open (#83); ambient motion must run as CSS.
        self.assertNotIn("setInterval", self.home_js)
        base_css = (REPO_ROOT / "vice" / "ui" / "styles" / "base.css").read_text()
        self.assertIn("vgDrift", base_css)
        self.assertIn("prefers-reduced-motion", base_css)

    def test_sidebar_shell_replaces_top_nav(self) -> None:
        self.assertIn('id="sidebar"', self.index)
        self.assertIn("PLAYLISTS", self.index)
        self.assertIn('id="sidebar-playlists"', self.index)
        self.assertIn('id="search-input"', self.index)
        self.assertNotIn("nav-indicator", self.index)

    def test_playlists_ui_is_wired(self) -> None:
        self.assertIn('id="new-playlist-modal"', self.index)
        self.assertIn('id="tut-page-2"', self.index)
        self.assertIn('id="home-playlists"', self.index)
        self.assertIn('id="playlist-header-tile"', self.index)
        self.assertIn('id="playlist-delete-btn"', self.index)
        playlists_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "playlists.js").read_text()
        self.assertIn("/api/playlists", playlists_js)
        self.assertIn("openPlaylistMenu", playlists_js)

    def test_most_viewed_and_dynamic_home_rows_are_wired(self) -> None:
        self.assertIn('id="home-viewed-row"', self.index)
        self.assertIn("homeRowCapacity", self.home_js)
        viewer_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "viewer.js").read_text()
        self.assertIn("recordClipView", viewer_js)
        self.assertIn("/view", viewer_js)

    def test_playlist_edit_is_wired(self) -> None:
        self.assertIn('id="playlist-edit-btn"', self.index)
        playlists_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "playlists.js").read_text()
        self.assertIn("openEditPlaylistModal", playlists_js)
        self.assertIn("savePlaylistEdits", playlists_js)

    def test_mini_player_exists_and_shares_viewer_video(self) -> None:
        self.assertIn('id="player-bar"', self.index)
        player_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "player.js").read_text()
        self.assertIn("viewer-video", player_js)
        self.assertIn("closePlayerBar", player_js)

    def test_copy_file_replaces_the_add_to_playlist_button(self) -> None:
        clips_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "clips.js").read_text()

        self.assertIn("copyClipFile", clips_js)
        self.assertIn("/copy-file", clips_js)
        # Add-to-playlist moved to right-click, so the button is gone but the
        # popover it used must still be reachable.
        self.assertNotIn('title="Add to playlist"', clips_js)
        self.assertIn("oncontextmenu=\"openPlaylistMenu(event", clips_js)

    def test_clips_can_be_dragged_onto_sidebar_playlists(self) -> None:
        clips_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "clips.js").read_text()
        playlists_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "playlists.js").read_text()

        self.assertIn('draggable="true"', clips_js)
        self.assertIn("onClipDragStart", clips_js)
        for fn in ("onClipDragStart", "onClipDragEnd", "onPlaylistDragOver", "onPlaylistDrop"):
            self.assertIn(f"function {fn}", playlists_js)
        # Every sidebar playlist row is a drop target, including auto ones.
        self.assertNotIn("pl.kind === 'custom' ?", playlists_js)
        sidebar_css = (REPO_ROOT / "vice" / "ui" / "styles" / "sidebar.css").read_text()
        self.assertIn(".side-pl-row.drop-over", sidebar_css)

    def test_auto_playlists_are_editable_and_deletable_in_ui(self) -> None:
        playlists_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "playlists.js").read_text()
        # The header Edit/Delete controls and the edit/delete handlers must not
        # gate on kind === 'custom' anymore.
        self.assertNotIn("pl.kind !== 'custom'", playlists_js)
        self.assertNotIn("p.kind === 'custom'", playlists_js)

    def test_clip_filename_template_is_wired(self) -> None:
        self.assertIn('id="s-clip-name"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("clip_name_template", settings_js)
        self.assertIn("updateClipNamePreview", settings_js)

    def test_auto_playlist_toggle_is_wired(self) -> None:
        self.assertIn('id="s-auto-playlist"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("auto_playlist_by_game", settings_js)

    def test_lan_only_share_links_are_called_out(self) -> None:
        clips_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "clips.js").read_text()

        self.assertIn("share_is_public", clips_js)
        self.assertIn("only works on your network", clips_js)

    def test_default_clip_duration_is_twenty_seconds(self) -> None:
        self.assertIn('id="s-dur" min="5" max="1800" step="5" value="20"', self.index)
        state_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "state.js").read_text()
        self.assertIn("clip_duration: 20", state_js)

    def test_home_lede_duration_is_data_driven(self) -> None:
        # The "last N of your gameplay" copy must reflect the configured clip
        # duration, not a hardcoded number.
        self.assertIn('id="lede-dur"', self.index)
        self.assertIn("setText('lede-dur'", self.home_js)

    def test_tutorial_seen_is_persisted_server_side(self) -> None:
        # localStorage does not survive restarts on every QtWebEngine build,
        # so the seen flag is also stored via /api/app-state.
        init_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "init.js").read_text()
        modals_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "modals.js").read_text()
        self.assertIn("/api/app-state", init_js)
        self.assertIn("tutorial_seen", init_js)
        self.assertIn("/api/app-state", modals_js)

    def test_floating_surfaces_share_gradient_backdrop(self) -> None:
        base_css = (REPO_ROOT / "vice" / "ui" / "styles" / "base.css").read_text()
        player_css = (REPO_ROOT / "vice" / "ui" / "styles" / "player.css").read_text()
        self.assertIn("--float-sheen", base_css)
        # Player bar and viewer modal both use the accent-tinted gradient.
        self.assertEqual(player_css.count("var(--float-sheen)"), 2)
        quit_css = (REPO_ROOT / "vice" / "ui" / "styles" / "modals.css").read_text()
        self.assertIn("var(--float-sheen)", quit_css)

    def test_clip_drag_uses_a_compact_drag_image(self) -> None:
        playlists_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "playlists.js").read_text()
        clips_css = (REPO_ROOT / "vice" / "ui" / "styles" / "clips.css").read_text()
        self.assertIn("setDragImage", playlists_js)
        self.assertIn("clip-drag-ghost", clips_css)

    def test_hevc_clips_request_an_h264_preview_proxy(self) -> None:
        state_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "state.js").read_text()
        helpers_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "helpers.js").read_text()
        viewer_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "viewer.js").read_text()
        trim_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "trim.js").read_text()

        self.assertIn("HEVC_SUPPORTED", state_js)
        self.assertIn("playbackUrl", helpers_js)
        self.assertIn("proxy=1", helpers_js)
        # Both playback surfaces resolve the URL through the proxy helper.
        self.assertIn("playbackUrl", viewer_js)
        self.assertIn("playbackUrl", trim_js)
        # And both show the preparing overlay while the proxy transcodes.
        self.assertIn('id="viewer-video-preparing"', self.index)
        self.assertIn('id="trim-video-preparing"', self.index)

    def test_update_notice_is_wired_and_stays_quiet_once_dismissed(self) -> None:
        self.assertIn('id="update-modal"', self.index)
        self.assertIn('id="update-chip"', self.index)
        self.assertIn("/scripts/updates.js?v=__VICE_VERSION__", self.index)

        modals_css = (REPO_ROOT / "vice" / "ui" / "styles" / "modals.css").read_text()
        # Must be registered in the shared modal shell, both states.
        self.assertIn("#manual-copy-modal, #update-modal {", modals_css)
        self.assertIn("#manual-copy-modal.hidden, #update-modal.hidden", modals_css)
        # It reuses .restart-box, which is already covered by the perf-low and
        # no-backdrop-filter fallbacks, so it cannot render as flat mush.
        self.assertIn("restart-box update-box", self.index)

        updates_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "updates.js").read_text()
        self.assertIn("update_dismissed_version", updates_js)
        self.assertIn("vice_update_dismissed", updates_js)
        ws_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "ws.js").read_text()
        self.assertIn("update_available", ws_js)

    def test_update_copy_has_no_em_dashes(self) -> None:
        updates_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "updates.js").read_text()
        for line in updates_js.splitlines():
            if EM_DASH in line:
                self.assertTrue(line.lstrip().startswith(("//", "*", "/*")), line.strip())
        card = self.index.split('id="update-modal"')[1].split("</div>\n\n")[0]
        self.assertNotIn(EM_DASH, card)

    def test_boot_splash_covers_the_first_paint_and_always_clears(self) -> None:
        self.assertIn('id="boot"', self.index)
        base = (REPO_ROOT / "vice" / "ui" / "styles" / "base.css").read_text()
        self.assertIn("#boot", base)
        init = (REPO_ROOT / "vice" / "ui" / "scripts" / "init.js").read_text()
        # Dismissed when the data lands, and on a timer so a slow or dead
        # daemon can never leave the window stuck behind it.
        self.assertIn("finally(hideBoot)", init)
        self.assertIn("setTimeout(hideBoot", init)

    def test_floating_surfaces_stay_themed_without_backdrop_filter(self) -> None:
        # Andrew's machine falls back to software compositing every launch, so
        # the no-blur path is the everyday look, not a degraded edge case.
        base = (REPO_ROOT / "vice" / "ui" / "styles" / "base.css").read_text()
        self.assertIn("--float-solid:", base)
        self.assertIn("--pop-solid:", base)
        self.assertIn(".perf-low .modal", base)
        # The old flat fills carried no theme at all.
        self.assertNotIn("#131b2e, #0d1424", base)
        self.assertNotIn("#16203a, #101828", base)

    def test_new_assets_carry_version_token(self) -> None:
        # UI assets are cached immutable for a year; any reference without
        # the version token would serve stale files forever after upgrade.
        for ref in (
            "/styles/sidebar.css?v=__VICE_VERSION__",
            "/styles/playlists.css?v=__VICE_VERSION__",
            "/styles/player.css?v=__VICE_VERSION__",
            "/scripts/playlists.js?v=__VICE_VERSION__",
            "/scripts/player.js?v=__VICE_VERSION__",
            "/scripts/perf.js?v=__VICE_VERSION__",
        ):
            self.assertIn(ref, self.index)

    def test_notification_volume_is_adjustable(self) -> None:
        # Issue #127: the clip ping had no volume control and no way off.
        self.assertIn('id="s-vol-notify"', self.index)
        settings_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "settings.js").read_text()
        self.assertIn("sound_volume", settings_js)
        self.assertIn("notifications", settings_js)
        # 0 must read as Off, not as "0%", or it looks broken rather than muted.
        self.assertIn("'Off'", settings_js)
        self.assertIn("s-vol-notify-v", self.index)
        row = self.index.split('id="s-vol-notify"')[0].split("Notification volume")[1]
        self.assertNotIn(EM_DASH, row)

    def test_effects_mode_is_measured_not_guessed(self) -> None:
        perf = (REPO_ROOT / "vice" / "ui" / "scripts" / "perf.js").read_text()
        # The engine can drop to software compositing silently, so the verdict
        # comes from timing real frames rather than from a stderr marker.
        self.assertIn("requestAnimationFrame", perf)
        self.assertIn("SLOW_FRAME_MS", perf)
        # Measuring while the effects are already off would read the cheap UI,
        # decide the machine is fast, and flip back every launch.
        self.assertIn("classList.remove('perf-low')", perf)
        # sw=1 stays a valid shortcut to the same verdict.
        self.assertIn("IS_SOFTWARE_RENDER", perf)
        # Persisted server-side: the native window's localStorage does not
        # survive restarts on every QtWebEngine build.
        self.assertIn("/api/app-state", perf)
        self.assertIn("effects_mode", perf)

        init = (REPO_ROOT / "vice" / "ui" / "scripts" / "init.js").read_text()
        # Probed only once the splash is gone, or it would time the boot
        # animation instead of the UI.
        self.assertIn("initEffects()", init)

    def test_effects_setting_offers_every_mode(self) -> None:
        self.assertIn('id="s-effects"', self.index)
        for value in ('value="auto"', 'value="full"', 'value="reduced"'):
            self.assertIn(value, self.index)
        self.assertIn('id="s-effects-note"', self.index)
        row = self.index.split('id="s-effects"')[0].split("Visual effects")[1]
        self.assertNotIn(EM_DASH, row)

    def test_reduced_effects_stops_the_endless_animations(self) -> None:
        base = (REPO_ROOT / "vice" / "ui" / "styles" / "base.css").read_text()
        # While the glows drift, the whole viewport repaints every frame and
        # every glass surface above them re-blurs, which is the cost this mode
        # exists to avoid. Dropping the blur alone leaves most of it behind.
        self.assertIn(".perf-low #ambient .glow { filter: none; animation: none; }", base)
        self.assertIn(".perf-low .rec-dot.live", base)
        self.assertIn(".perf-low .view { animation: none; }", base)
        # .g3 is a circular gradient in a square box, so rotating it is
        # invisible and only costs a transform the compositor cannot skip.
        drift3 = [ln for ln in base.splitlines() if "vgDrift3" in ln and "@keyframes" in ln]
        self.assertTrue(drift3)
        self.assertNotIn("rotate", drift3[0])


EDITOR_SCRIPTS = ("editor-core", "editor-library", "editor-timeline",
                  "editor-preview", "editor-export")


class EditorUiStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = UI_INDEX.read_text()
        cls.scripts = "\n".join(
            (REPO_ROOT / "vice" / "ui" / "scripts" / f"{n}.js").read_text()
            for n in EDITOR_SCRIPTS)
        cls.css = (REPO_ROOT / "vice" / "ui" / "styles" / "editor.css").read_text()

    def test_editor_view_nav_and_assets_are_wired(self) -> None:
        self.assertIn('id="view-editor"', self.index)
        self.assertIn('data-view="editor"', self.index)
        self.assertIn("/styles/editor.css?v=__VICE_VERSION__", self.index)
        for n in EDITOR_SCRIPTS:
            self.assertIn(f"/scripts/{n}.js?v=__VICE_VERSION__", self.index)

    def test_editor_modals_exist(self) -> None:
        for eid in ("ed-export-modal", "ed-reset-modal", "ed-inspector",
                    "ed-stage", "ed-tl-canvas"):
            self.assertIn(f'id="{eid}"', self.index)

    def test_editor_playback_uses_the_proxy_helper(self) -> None:
        self.assertIn("playbackUrl", self.scripts)
        self.assertIn("clipNeedsProxy", self.scripts)

    def test_editor_copy_has_no_em_dashes(self) -> None:
        # User-facing copy only; header comments follow the existing code
        # style, which does use em dashes.
        editor_section = self.index.split('id="view-editor"')[1].split("</section>")[0]
        self.assertNotIn(EM_DASH, editor_section)
        for line in self.scripts.splitlines():
            if EM_DASH in line:
                self.assertTrue(line.lstrip().startswith(("//", "*", "/*")),
                                f"em dash outside a comment: {line.strip()}")

    def test_perf_low_gets_solid_editor_surfaces(self) -> None:
        # Popovers overlay real content and go solid; the panels do not,
        # because they share the sidebar's glass (see the parity test).
        self.assertIn(".perf-low .ed-menu", self.css)
        self.assertNotIn(".perf-low .ed-panel", self.css)
        # The blur transition preview is skipped under perf-low/reduced motion.
        self.assertIn("prefers-reduced-motion", self.scripts)

    def test_editor_panels_share_the_sidebar_glass(self) -> None:
        base = (REPO_ROOT / "vice" / "ui" / "styles" / "base.css").read_text()
        sidebar = (REPO_ROOT / "vice" / "ui" / "styles" / "sidebar.css").read_text()
        self.assertIn("--glass-fill:", base)
        self.assertIn("var(--glass-fill)", sidebar)
        self.assertIn("var(--glass-fill)", self.css)

    def test_preview_keeps_idle_videos_decoding(self) -> None:
        # display:none stops the browser decoding, which would defeat the
        # preload the allocator depends on.
        self.assertIn("ed-vhide", self.css)
        self.assertIn("ed-vhide", self.scripts)
        preview = (REPO_ROOT / "vice" / "ui" / "scripts" / "editor-preview.js").read_text()
        self.assertNotIn("v.style.display", preview)
        self.assertIn("isolation: isolate", self.css)

    def test_ws_dispatch_covers_export_messages(self) -> None:
        ws_js = (REPO_ROOT / "vice" / "ui" / "scripts" / "ws.js").read_text()
        for msg in ("export_progress", "export_done", "export_error",
                    "editor_project_changed"):
            self.assertIn(msg, ws_js)


if __name__ == "__main__":
    unittest.main()


class NoEmDashesAnywhereTests(unittest.TestCase):
    """The rule is no em-dashes in anything shipped, so it is checked rather
    than remembered. It kept creeping back into log lines and comments where
    nobody was looking."""

    SHIPPED_SUFFIXES = {".py", ".js", ".css", ".html", ".sh", ".md", ".toml", ".service", ".desktop"}
    SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}

    def _shipped_files(self):
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in self.SKIP_DIRS for part in path.parts):
                continue
            # Not committed; it is the local maintenance guide.
            if path.name == "CLAUDE.md":
                continue
            if path.suffix in self.SHIPPED_SUFFIXES or path.name in {"install.sh", "PKGBUILD", ".SRCINFO"}:
                yield path

    def test_no_shipped_file_contains_an_em_dash(self) -> None:
        offenders = []
        for path in self._shipped_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if EM_DASH in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:90]}")
        self.assertEqual(
            offenders, [],
            "Em-dashes are not used anywhere in Vice. Use a comma, a colon or "
            "a full stop:\n" + "\n".join(offenders),
        )

    def test_the_guard_actually_looks_at_the_source(self) -> None:
        # A guard that silently matches nothing is worse than none at all.
        found = list(self._shipped_files())
        self.assertGreater(len(found), 40)
        self.assertTrue(any(p.name == "recorder.py" for p in found))
        self.assertTrue(any(p.name == "index.html" for p in found))
