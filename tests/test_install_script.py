import importlib
import re
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"


class InstallScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = INSTALL_SH.read_text()

    def test_gsr_source_build_uses_pinned_refs_and_override(self) -> None:
        script = self.script

        self.assertIn('GSR_DEFAULT_REF="6.1.3"', script)
        self.assertIn('GSR_FFMPEG4_REF="5.12.5"', script)
        self.assertIn('VICE_GSR_REF:-', script)
        self.assertIn("major < 57", script)
        self.assertIn('_gsr_fetch_source "$gsr_ref" "$tmpdir"', script)
        self.assertIn('git clone --depth 1 --branch "$ref" "$GSR_REPO_URL" "$dest"', script)

    def test_gsr_source_falls_back_to_the_upstream_tarball(self) -> None:
        """#182: repo.dec05eba.com redirected a reporter to the cgit browse
        host, which serves no git protocol, and the install had nowhere else to
        go. The tarball is the same source upstream's own AUR package builds."""
        script = self.script

        self.assertIn("VICE_GSR_SNAPSHOT_URL:-", script)
        self.assertIn("https://dec05eba.com/snapshot", script)
        self.assertIn('tarball="$GSR_SNAPSHOT_URL/gpu-screen-recorder.git.$ref.tar.gz"', script)
        # Both download tools, so a machine with only one of them still works.
        self.assertIn('curl -fsSL "$tarball" | tar -xz -C "$dest"', script)
        self.assertIn('wget -qO- "$tarball" | tar -xz -C "$dest"', script)
        # A dead end names what it tried and how to point elsewhere.
        self.assertIn("Could not fetch gpu-screen-recorder $ref.", script)
        self.assertIn("VICE_GSR_REPO_URL, VICE_GSR_SNAPSHOT_URL or VICE_GSR_REF.", script)
        # The fetch has to be defined before the build function that calls it.
        self.assertLess(script.index("_gsr_fetch_source() {"),
                        script.index("_gsr_build_from_source() {"))

    def test_rpm_ostree_guard_runs_before_package_manager_detection(self) -> None:
        script = self.script

        self.assertIn("/run/ostree-booted", script)
        self.assertIn("rpm-ostree", script)
        self.assertIn("Bazzite / Fedora Atomic", script)
        self.assertIn("Silverblue", script)
        self.assertIn("dnf is not the right install path", script)
        self.assertIn("vice.flatpakref", script)
        self.assertLess(script.index("if is_rpm_ostree_system; then"), script.index("detect_package_manager()"))

    def test_arch_qt_preflight_preserves_existing_install(self) -> None:
        """A failed Arch Qt preflight must not remove a working install."""
        install_venv = re.search(
            r"install_vice_venv\(\) \{\n(?P<body>.*?)\n\}",
            self.script,
            flags=re.S,
        )
        self.assertIsNotNone(install_venv)
        body = install_venv.group("body")

        qt_check = body.index("import PyQt6.QtWebEngineWidgets, qtpy")
        cleanup = body.index("clean_previous_local_install")
        remove_venv = body.index('rm -rf "$VENV_DIR"')
        create_venv = body.index('"$python_bin" -m venv')

        self.assertLess(qt_check, cleanup)
        self.assertLess(cleanup, remove_venv)
        self.assertLess(remove_venv, create_venv)
        self.assertEqual(self.script.count('rm -rf "$VENV_DIR"'), 1)

    def test_missing_optional_parent_is_reported_as_missing(self) -> None:
        """find_spec imports dotted-name parents and may raise if one is absent."""
        helper = re.search(
            r"^def _missing\(deps\):\n.*?^    return missing$",
            self.script,
            flags=re.M | re.S,
        )
        self.assertIsNotNone(helper)

        namespace = {"importlib": importlib}
        exec(helper.group(0), namespace)
        dependency = {"PyQt6.QtWebEngineWidgets": "PyQt6-WebEngine>=6.5"}
        missing_parent = ModuleNotFoundError("No module named 'PyQt6'")

        with mock.patch.object(importlib.util, "find_spec", side_effect=missing_parent):
            self.assertEqual(
                namespace["_missing"](dependency),
                ["PyQt6-WebEngine>=6.5"],
            )

    def test_gsr_build_runs_as_user_with_sudo_only_for_install(self) -> None:
        """Regression test for #84: building under sudo left a root-owned
        tree in /tmp that cleanup could not delete."""
        script = self.script

        # The upstream installer (which runs everything as root) is gone.
        self.assertNotIn("sudo ./install.sh", script)
        # Build steps run unprivileged; only meson install is elevated.
        self.assertIn("meson setup build", script)
        self.assertNotIn("sudo meson setup", script)
        self.assertNotIn("sudo ninja", script)
        self.assertIn("sudo meson install -C build", script)
        # Cleanup has a sudo fallback for any root-owned leftovers.
        self.assertIn('rm -rf "$tmpdir" 2>/dev/null || sudo rm -rf "$tmpdir"', script)

    def test_fedora_ffmpeg_devel_matches_installed_ffmpeg(self) -> None:
        """Regression test for #115: RPM Fusion systems have ffmpeg, not
        ffmpeg-free, so the -devel package must match."""
        script = self.script

        self.assertIn("_fedora_ffmpeg_devel()", script)
        self.assertIn("rpm -q ffmpeg &>/dev/null", script)
        self.assertIn("printf 'ffmpeg-devel\\n'", script)
        self.assertIn("printf 'ffmpeg-free-devel\\n'", script)

        match = re.search(
            r"dnf\)\s+local ffmpeg_devel.*?_dnf_install_best_effort (?P<packages>.*?)\n\s+;;",
            script,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        self.assertIn('"$ffmpeg_devel"', match.group("packages"))
        self.assertNotIn("ffmpeg-free-devel", match.group("packages"))

    def test_dnf_runtime_ffmpeg_is_not_hard_requested(self) -> None:
        """Regression test for #173: Nobara has ffmpeg-free installed, so naming
        ffmpeg in the package list makes dnf refuse the whole transaction."""
        script = self.script

        self.assertIn("_dnf_ffmpeg_pkg()", script)

        match = re.search(
            r"install_pkgs_dnf\(\) \{\s+local pkgs=\((?P<packages>[^)]*)\)",
            script,
        )
        self.assertIsNotNone(match)
        self.assertNotIn("ffmpeg", match.group("packages"))

        # The package is decided by the helper and only added when one is needed.
        self.assertIn('ffmpeg_pkg="$(_dnf_ffmpeg_pkg)"', script)
        self.assertIn('[[ -n "$ffmpeg_pkg" ]] && pkgs+=("$ffmpeg_pkg")', script)

    def test_dnf_ffmpeg_helper_skips_an_already_working_ffmpeg(self) -> None:
        """The conflict is only avoidable by not asking for a package at all."""
        script = self.script
        helper = re.search(r"_dnf_ffmpeg_pkg\(\) \{.*?\n\}", script, flags=re.S)
        self.assertIsNotNone(helper)
        body = helper.group(0)
        self.assertIn("command -v ffmpeg", body)
        self.assertIn("command -v ffprobe", body)
        self.assertIn("printf 'ffmpeg-free\\n'", body)

    def test_missing_libx264_warns_rather_than_failing(self) -> None:
        """ffmpeg-free has no libx264. Recording still works, so this is a warning."""
        script = self.script
        self.assertIn("_warn_if_no_libx264", script)
        helper = re.search(r"_warn_if_no_libx264\(\) \{.*?\n\}", script, flags=re.S)
        self.assertIsNotNone(helper)
        self.assertNotIn("exit 1", helper.group(0))

    def test_clipboard_tools_installed_per_session_type(self) -> None:
        script = self.script

        self.assertIn("wl-clipboard", script)
        self.assertIn("xclip", script)
        # Present in every package-manager branch.
        for mgr in ("apt-get install -y wl-clipboard",
                    "dnf install -y wl-clipboard",
                    "zypper install -y wl-clipboard"):
            self.assertIn(mgr, script)

    def test_apt_gsr_build_deps_include_upstream_required_headers(self) -> None:
        match = re.search(
            r"apt\)\s+sudo apt-get install -y (?P<packages>.*?) \|\| return 1",
            self.script,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        packages = set(re.findall(r"[A-Za-z0-9_.+-]+", match.group("packages")))

        required = {
            "build-essential",
            "linux-libc-dev",
            "libx11-dev",
            "libavfilter-dev",
            "libva-dev",
            "libcap-dev",
            "libdbus-1-dev",
            "libvulkan-dev",
            "libspa-0.2-dev",
            "libpipewire-0.3-dev",
            "libavcodec-dev",
            "libavformat-dev",
            "libavutil-dev",
            "libswresample-dev",
        }
        self.assertTrue(required.issubset(packages), required - packages)

    def test_dnf_gsr_build_deps_match_the_apt_branch(self) -> None:
        """Fedora was missing a C++ compiler, libva, vulkan and libcap, so the
        source build failed one meson check at a time."""
        match = re.search(
            r"dnf\)\s+local ffmpeg_devel.*?_dnf_install_best_effort (?P<packages>.*?)\n\s+;;",
            self.script,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        packages = set(re.findall(r"[A-Za-z0-9_.+-]+", match.group("packages")))

        required = {"gcc-c++", "libva-devel", "vulkan-loader-devel", "libcap-devel"}
        self.assertTrue(required.issubset(packages), required - packages)

    def test_dnf_build_deps_retry_individually(self) -> None:
        # One unavailable name on an unusual arch used to abort the whole
        # transaction before meson could report the real missing dependency.
        self.assertIn("_dnf_install_best_effort()", self.script)
        self.assertIn('sudo dnf install -y "$pkg"', self.script)

    def test_fedora_qtpy_package_uses_capitalised_name(self) -> None:
        # Fedora ships python3-QtPy and dnf5 matches case-sensitively.
        self.assertIn("sudo dnf install -y python3-QtPy", self.script)
        # Installing the Qt stack as one command meant the case mismatch also
        # dropped PyQt6 and QtWebEngine to PyPI wheels.
        self.assertNotIn(
            "python3-pyqt6 python3-pyqt6-webengine python3-qtpy >/dev/null", self.script
        )

    def test_cloudflared_rpm_matches_machine_architecture(self) -> None:
        self.assertIn('cloudflared-linux-${_cf_arch}.rpm', self.script)
        self.assertIn("aarch64|arm64) _cf_arch=arm64", self.script)

    def test_no_stale_serveo_references(self) -> None:
        # serveo was removed as a tunnel in v1.3.3, but the installer still
        # promised it as a fallback, which confused the reporter of #105.
        self.assertNotIn("serveo", self.script.lower())


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = INSTALL_SH.read_text()

    def test_aur_package_ships_user_service(self) -> None:
        """Regression test for #116: the AUR package installed no systemd
        unit, so the daemon never started at login."""
        service = (REPO_ROOT / "packaging" / "vice.service").read_text()
        self.assertIn("ExecStart=/usr/bin/vice start --no-open-ui", service)
        self.assertIn("WantedBy=graphical-session.target", service)
        self.assertIn("PassEnvironment=WAYLAND_DISPLAY DISPLAY", service)

        pkgbuild = (REPO_ROOT / "PKGBUILD").read_text()
        self.assertIn("packaging/vice.service", pkgbuild)
        self.assertIn("/usr/lib/systemd/user/vice.service", pkgbuild)
        self.assertIn("install=vice-clipper.install", pkgbuild)

    def test_clipboard_and_tunnel_tools_are_hard_dependencies(self) -> None:
        """Copy-to-clipboard and public links silently failed on the AUR
        package because these were only optdepends."""
        import re
        pkgbuild = (REPO_ROOT / "PKGBUILD").read_text()
        depends = re.search(r"depends=\((.*?)\)", pkgbuild, flags=re.S).group(1)
        optdepends = re.search(r"optdepends=\((.*?)\)", pkgbuild, flags=re.S).group(1)
        for pkg in ("wl-clipboard", "xclip", "cloudflared"):
            self.assertIn(f"'{pkg}'", depends)
            self.assertNotIn(f"'{pkg}:", optdepends)
        self.assertIn(
            "systemctl --user enable --now vice.service",
            (REPO_ROOT / "vice-clipper.install").read_text(),
        )

    def test_window_detection_tools_are_hard_dependencies(self) -> None:
        """Game tagging, auto playlists and Discord presence all read the
        focused window through these, and neither install path shipped them,
        so detection silently found nothing (#152)."""
        pkgbuild = (REPO_ROOT / "PKGBUILD").read_text()
        depends = re.search(r"depends=\((.*?)\)", pkgbuild, flags=re.S).group(1)
        for pkg in ("xdotool", "xorg-xprop", "wmctrl"):
            self.assertIn(f"'{pkg}'", depends)
        self.assertIn("xdotool xorg-xprop wmctrl", self.script)
        # The other package managers spell xprop differently.
        for branch in ("xdotool x11-utils wmctrl", "xdotool xprop wmctrl"):
            self.assertIn(branch, self.script)

    def test_nvidia_utils_is_not_forced_over_a_legacy_branch(self) -> None:
        """nvidia-smi answering already proves a driver userspace is there.
        Asking for nvidia-utils by name collided with nvidia-580xx-utils and
        aborted the whole install (#147)."""
        self.assertIn("^nvidia(-[0-9]+xx)?-utils$", self.script)
        # The add still exists, but only behind the already-installed check.
        guard = self.script.index("^nvidia(-[0-9]+xx)?-utils$")
        add = self.script.index("pkgs+=(nvidia-utils)")
        self.assertLess(guard, add)

    def test_service_is_reenabled_so_wantedby_changes_take_effect(self) -> None:
        """enable leaves an existing unit's old symlinks in place, so the new
        default.target want never appeared on upgrades (#139)."""
        self.assertIn("systemctl --user reenable vice.service", self.script)
        self.assertIn(
            "systemctl --user reenable vice.service",
            (REPO_ROOT / "vice-clipper.install").read_text(),
        )


if __name__ == "__main__":
    unittest.main()


class GsrUpgradeTests(unittest.TestCase):
    """Rerunning install.sh has to bring an old source-built gpu-screen-recorder
    up to date: 6.1.2 hardened its privileged helper, and 5.13.3 wrote broken
    MP4s on Debian 13 (#154). A packaged one is its package manager's job.

    The harness runs under the script's own ``set -euo pipefail``. Without it a
    failing command substitution passed here and ended the real script with no
    message (#236)."""

    FUNCTIONS = ("_gsr_libavutil_major", "_gsr_select_ref", "_gsr_installed_version",
                 "_gsr_cannot_load", "_version_lt", "_gsr_owned_by_package",
                 "install_gpu_screen_recorder")

    @classmethod
    def setUpClass(cls) -> None:
        script = INSTALL_SH.read_text()
        cls.strict = re.search(r"^set -[a-z]+ pipefail$", script, re.M).group(0)
        parts = [re.search(r"^GSR_DEFAULT_REF=.*$", script, re.M).group(0),
                 re.search(r"^GSR_FFMPEG4_REF=.*$", script, re.M).group(0)]
        for name in cls.FUNCTIONS:
            match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", script, re.S | re.M)
            if match:
                parts.append(match.group(0))
        cls.functions = "\n".join(parts)

    def _exec(self, *, installed: str = "6.1.3", libavutil: str = "59.39.100",
              packaged: bool = False, broken: bool = False, build_ok: bool = True,
              version_fails: bool = False):
        import os
        import shutil
        import subprocess
        import tempfile
        # Build sandboxes such as Nix have no /usr/bin, so bash and coreutils
        # come from the inherited PATH, behind the fakes (#233).
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            fixed = bin_dir / "rebuilt"
            gsr = (f"echo 'error while loading shared libraries: libavcodec.so.61' >&2; exit 127"
                   if broken else ("exit 1" if version_fails else f"echo '{installed}'"))
            fakes = {
                "gpu-screen-recorder": gsr,
                "pkg-config": f"echo '{libavutil}'",
                "dpkg": "exit 0" if packaged else "exit 1",
                # Reports a missing library until the fake rebuild has run.
                "ldd": (f"[ -e '{fixed}' ] || {{ echo 'libavcodec.so.61 => not found'; }}"
                        if broken else "true"),
            }
            for name, body in fakes.items():
                path = bin_dir / name
                path.write_text(f"#!{bash}\n{body}\n")
                path.chmod(0o755)
            build = f"echo BUILD; touch '{fixed}'" if build_ok else "echo BUILD; return 1"
            harness = (
                f"{self.strict}\n"
                "info() { echo \"INFO $*\"; }\nwarn() { echo \"WARN $*\"; }\n"
                "error() { echo \"ERROR $*\"; }\n"
                f"_gsr_build_from_source() {{ {build}; }}\n"
                f"{self.functions}\nPKG=apt\ninstall_gpu_screen_recorder\necho FINISHED\n"
            )
            return subprocess.run(
                [bash, "-c", harness], capture_output=True, text=True, timeout=10,
                env={"PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", os.defpath)])},
            )

    def _run(self, **kwargs) -> str:
        result = self._exec(**kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FINISHED", result.stdout)
        return result.stdout

    def test_an_old_source_build_is_rebuilt_at_the_pinned_tag(self) -> None:
        out = self._run(installed="5.13.3")
        self.assertIn("updating it to 6.1.3", out)
        self.assertIn("BUILD", out)

    def test_a_packaged_old_version_is_left_to_its_package_manager(self) -> None:
        out = self._run(installed="5.13.3", packaged=True)
        self.assertNotIn("BUILD", out)
        self.assertIn("security issue", out)

    def test_a_current_install_is_left_alone(self) -> None:
        for installed in ("6.1.3", "6.1.10"):
            out = self._run(installed=installed)
            self.assertNotIn("BUILD", out, installed)
            self.assertIn("already installed", out)

    def test_an_unreadable_version_changes_nothing(self) -> None:
        out = self._run(installed="")
        self.assertNotIn("BUILD", out)
        self.assertNotIn("WARN", out)

    def test_a_binary_that_will_not_run_never_ends_the_script_silently(self) -> None:
        out = self._run(version_fails=True)
        self.assertNotIn("BUILD", out)

    def test_a_source_build_broken_by_an_upgrade_is_rebuilt(self) -> None:
        # #236: Pop!_OS 24.04 to 26.04 replaced the FFmpeg it was built against.
        out = self._run(broken=True)
        self.assertIn("cannot load its libraries", out)
        self.assertIn("BUILD", out)
        self.assertIn("rebuilt", out)

    def test_a_broken_packaged_build_says_to_reinstall_it(self) -> None:
        result = self._exec(broken=True, packaged=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("BUILD", result.stdout)
        self.assertIn("Reinstall it with your package manager", result.stdout)

    def test_a_failed_rebuild_stops_with_a_reason(self) -> None:
        result = self._exec(broken=True, build_ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not rebuild gpu-screen-recorder", result.stdout)

    def test_ffmpeg_4_keeps_the_last_tag_that_builds_there(self) -> None:
        # Ubuntu 22.04: 6.1.3 fails to compile against FFmpeg 4.4.
        out = self._run(installed="5.12.5", libavutil="56.70.100")
        self.assertNotIn("BUILD", out)
