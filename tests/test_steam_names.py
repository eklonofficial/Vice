import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice import active_window
from vice.config import Config, DiscordConfig, DiscordCustomGame
from vice.main import ViceDaemon

FLATPAK = ".var/app/com.valvesoftware.Steam/.local/share/Steam"


def _manifest(root: Path, app_id: str, name: str) -> None:
    apps = root / "steamapps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"appmanifest_{app_id}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{app_id}"\n\t"name"\t\t"{name}"\n}}\n'
    )


def _libraries(root: Path, *paths: Path) -> None:
    apps = root / "steamapps"
    apps.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{p}"\n\t}}\n' for i, p in enumerate(paths)
    )
    (apps / "libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n{body}}}\n')


class _SteamFolderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.steam = self.home / ".local/share/Steam"
        self.steam.mkdir(parents=True)
        patcher = mock.patch(
            "vice.active_window.actual_home_dir", return_value=self.home
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class SteamGameNameTests(_SteamFolderTestCase):
    def test_a_game_in_the_main_library_is_named(self) -> None:
        _manifest(self.steam, "1671210", "DELTARUNE")

        self.assertEqual(active_window.steam_game_name("1671210"), "DELTARUNE")

    def test_a_game_in_a_second_library_is_named(self) -> None:
        other = self.tmp / "games" / "SteamLibrary"
        _libraries(self.steam, self.steam, other)
        _manifest(other, "413150", "Stardew Valley")

        self.assertEqual(
            active_window.steam_game_name("413150"),
            "Stardew Valley",
        )

    def test_a_missing_manifest_gives_none(self) -> None:
        _manifest(self.steam, "1671210", "DELTARUNE")

        self.assertIsNone(active_window.steam_game_name("42"))

    def test_a_manifest_without_a_name_gives_none(self) -> None:
        apps = self.steam / "steamapps"
        apps.mkdir(parents=True)
        (apps / "appmanifest_7.acf").write_text('"AppState"\n{\n\t"appid"\t"7"\n}\n')

        self.assertIsNone(active_window.steam_game_name("7"))

    def test_an_unusable_id_gives_none(self) -> None:
        _manifest(self.steam, "1671210", "DELTARUNE")

        for bad in (None, "", "abc", "../1671210"):
            with self.subTest(app_id=bad):
                self.assertIsNone(active_window.steam_game_name(bad))

    def test_proton_and_runtime_entries_are_skipped(self) -> None:
        for app_id, name in (
            ("1493710", "Proton Experimental"),
            ("1628350", "Steam Linux Runtime 3.0 (sniper)"),
            ("228980", "Steamworks Common Redistributables"),
        ):
            _manifest(self.steam, app_id, name)
            with self.subTest(name=name):
                self.assertIsNone(active_window.steam_game_name(app_id))

    def test_the_flatpak_install_is_read(self) -> None:
        _manifest(self.home / FLATPAK, "1671210", "DELTARUNE")

        self.assertEqual(active_window.steam_game_name("1671210"), "DELTARUNE")

    def test_the_flatpak_library_list_is_read(self) -> None:
        flatpak = self.home / FLATPAK
        other = self.tmp / "drive" / "SteamLibrary"
        _libraries(flatpak, flatpak, other)
        _manifest(other, "1671210", "DELTARUNE")

        self.assertEqual(active_window.steam_game_name("1671210"), "DELTARUNE")

    def test_a_symlinked_root_is_only_checked_once(self) -> None:
        (self.home / ".steam").mkdir()
        (self.home / ".steam/steam").symlink_to(self.steam)

        roots = active_window._steam_roots()

        self.assertEqual(len({r.resolve() for r in roots}), len(roots))


class MatchGameSteamFallbackTests(unittest.TestCase):
    def _daemon(self) -> ViceDaemon:
        daemon = ViceDaemon.__new__(ViceDaemon)
        daemon.cfg = Config(discord=DiscordConfig())
        return daemon

    def test_the_list_still_wins_when_both_match(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None):
            with mock.patch("vice.main.steam_game_name", return_value="Other") as steam:
                got = daemon._match_game(
                    {"process": "tf_linux64", "class": "steam_app_440", "pid": 1}
                )

        self.assertEqual(got, "Team Fortress 2")
        steam.assert_not_called()

    def test_an_unlisted_steam_game_is_named_from_its_window_class(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None):
            with mock.patch("vice.main.steam_game_name", return_value="Some Game") as steam:
                got = daemon._match_game(
                    {"process": "game.exe", "class": "steam_app_999999999", "pid": 1}
                )

        self.assertEqual(got, "Some Game")
        steam.assert_called_once_with("999999999")

    def test_an_unlisted_steam_game_is_named_from_its_process_app_id(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value="999999999"):
            with mock.patch("vice.main.steam_game_name", return_value="Some Game") as steam:
                got = daemon._match_game({"process": "game", "class": "", "pid": 1})

        self.assertEqual(got, "Some Game")
        steam.assert_called_once_with("999999999")

    def test_a_window_with_no_steam_id_tags_nothing(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None):
            with mock.patch("vice.main.steam_game_name") as steam:
                got = daemon._match_game(
                    {"process": "firefox", "class": "firefox", "pid": 1}
                )

        self.assertIsNone(got)
        steam.assert_not_called()

    def test_a_tool_or_unknown_app_tags_nothing(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None):
            with mock.patch("vice.main.steam_game_name", return_value=None):
                got = daemon._match_game(
                    {"process": "x", "class": "steam_app_999999999", "pid": 1}
                )

        self.assertIsNone(got)


class GameMatchPrecisionTests(unittest.TestCase):
    def _daemon(self, custom=()) -> ViceDaemon:
        daemon = ViceDaemon.__new__(ViceDaemon)
        daemon.cfg = Config(discord=DiscordConfig(custom_games=list(custom)))
        return daemon

    def _match(self, win, steam_name=None, custom=()):
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None), \
             mock.patch("vice.main.steam_game_name", return_value=steam_name):
            return self._daemon(custom)._match_game({"pid": 1, **win})

    def test_an_app_id_is_not_matched_by_a_shorter_one(self) -> None:
        # #264: Half-Life's steam_app_70 claimed SCP: Secret Laboratory.
        got = self._match({"process": "scpsl.x86_64", "class": "steam_app_700330"},
                          steam_name="SCP: Secret Laboratory")
        self.assertEqual(got, "SCP: Secret Laboratory")

    def test_an_app_id_still_matches_on_its_own(self) -> None:
        self.assertEqual(self._match({"process": "hl_linux", "class": "steam_app_70"}), "Half-Life")
        self.assertEqual(self._match({"process": "x", "class": "steam_app_70"}), "Half-Life")

    def test_a_cut_down_process_name_matches_its_game(self) -> None:
        # #262: the kernel shows RocketLeague.exe as RocketLeague.ex.
        got = self._match({"process": "RocketLeague.ex", "class": "steam_app_252950"})
        self.assertEqual(got, "Rocket League")

    def test_a_custom_entry_wins_on_a_cut_down_name(self) -> None:
        custom = [DiscordCustomGame(name="My League", matches=["rocketleague.exe"])]
        got = self._match({"process": "RocketLeague.ex", "class": ""}, custom=custom)
        self.assertEqual(got, "My League")

    def test_steam_names_the_game_before_a_cut_down_name_guesses(self) -> None:
        # Client-Win64-Shipping.exe is shared by many Unreal games, so its
        # cut-down form only counts when nothing better is known.
        got = self._match({"process": "Client-Win64-Sh", "class": "steam_app_999999999"},
                          steam_name="Some Unreal Game")
        self.assertEqual(got, "Some Unreal Game")

    def test_a_short_process_name_is_not_treated_as_cut_down(self) -> None:
        self.assertIsNone(self._match({"process": "rocket", "class": ""}))


if __name__ == "__main__":
    unittest.main()
