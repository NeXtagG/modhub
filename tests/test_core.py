"""Run: /usr/bin/python3 -I -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from modhub import core, tasks  # noqa: E402


def status(**over):
    st = {
        "steam": {"installed": "1.0.0.87-3", "latest": "1.0.0.87-3", "source": "repo", "pkg": "steam",
                  "client": "100", "client_latest": "100"},
        "millennium": {"installed": "3.6.1-1", "latest": "3.6.1-1", "linked": True, "pkg": "millennium-bin"},
        "discord": {"installed": "1:1.0.162-1", "latest": "1:1.0.162-1", "source": "repo", "pkg": "discord",
                    "host": "1.0.162", "host_latest": "1.0.162", "legacy": False},
        "vencord": {"installed": True, "wanted": True},
        "spotify": {"installed": "1:1.3.4.258-1", "latest": "1:1.3.4.258-1", "source": "repo", "pkg": "spotify"},
        "spicetify": {"installed": "2.45.3", "latest": "2.45.3-1", "applied": True, "wanted": True, "pkg": None},
    }
    for k, v in over.items():
        st[k] = dict(st[k], **v)
    return st


class Versions(unittest.TestCase):
    def test_short_ver(self):
        self.assertEqual(core.short_ver("1:1.2.96.518-3"), "1.2.96.518")
        self.assertEqual(core.short_ver("3.6.2_beta.1-1"), "3.6.2_beta.1")
        self.assertEqual(core.short_ver("2.45.3"), "2.45.3")

    def test_newer(self):
        self.assertTrue(core.newer("1:1.3.4.258-1", "1:1.2.96.518-3"))
        self.assertFalse(core.newer("1:1.2.96.518-3", "1:1.2.96.518-3"))
        self.assertFalse(core.newer(None, "1"))
        self.assertTrue(core.newer("3.6.2_beta.1-1", "3.5.0-1"))

    def test_spicetify_pkgrel(self):
        self.assertFalse(core.spicetify_outdated({"installed": "2.45.3", "latest": "2.45.3-1"}))
        self.assertTrue(core.spicetify_outdated({"installed": "2.45.3", "latest": "2.46.0"}))

    def test_checkupdates(self):
        out = "steam 1.0.0.87-3 -> 1.0.0.88-1\nspotify 1:1.2.96.518-3 -> 1:1.3.4.258-1\n"
        self.assertEqual(core.parse_checkupdates(out), {"steam": "1.0.0.88-1", "spotify": "1:1.3.4.258-1"})

    def test_steam_manifest(self):
        self.assertEqual(core.steam_manifest_version('"ubuntu12"\n{\n\t"version"\t\t"1791918634"\n'), "1791918634")


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_discord_host_and_vencord(self):
        for v in ("1.0.99", "1.0.162"):
            os.makedirs(os.path.join(self.d, f"app-{v}", "resources"))
        self.assertEqual(core.discord_host_version(self.d), "1.0.162")  # numeric, not string order
        res = core.discord_resources(self.d)
        with open(os.path.join(res, "app.asar"), "wb") as f:
            f.write(b'{"files":{}}require("/home/x/.config/Vencord/dist/patcher.js")')
        self.assertFalse(core.vencord_patched(res))  # no _app.asar
        open(os.path.join(res, "_app.asar"), "wb").close()
        self.assertTrue(core.vencord_patched(res))
        os.symlink(os.path.join(self.d, "app-1.0.99/Discord"), os.path.join(self.d, "Discord"))
        open(os.path.join(self.d, "app-1.0.99/Discord"), "w").close()
        self.assertEqual(core.discord_host_version(self.d), "1.0.99")  # the symlink wins

    def test_spicetify_applied(self):
        apps = os.path.join(self.d, "Apps")
        os.makedirs(apps)
        open(os.path.join(apps, "xpui.spa"), "w").close()
        self.assertFalse(core.spicetify_applied(self.d))
        os.remove(os.path.join(apps, "xpui.spa"))
        os.makedirs(os.path.join(apps, "xpui"))
        self.assertTrue(core.spicetify_applied(self.d))

    def test_millennium_relink(self):
        for sub in ("ubuntu12_32", "ubuntu12_64"):
            os.makedirs(os.path.join(self.d, sub))
        os.symlink("/somewhere/else", os.path.join(self.d, "ubuntu12_32/libXtst.so.6"))
        self.assertFalse(core.millennium_linked(self.d))
        old = core.STEAM_DIR
        core.STEAM_DIR = self.d
        try:
            tasks.millennium_relink(tasks.Ctx(lambda m: None))
        finally:
            core.STEAM_DIR = old
        self.assertTrue(core.millennium_linked(self.d))

    def test_ini(self):
        p = os.path.join(self.d, "c.ini")
        with open(p, "w") as f:
            f.write("[Setting]\nspotify_path           = /opt/spotify/\n; DO NOT CHANGE!\n[Backup]\nversion = 1\n")
        self.assertEqual(core.read_ini(p)["Setting"]["spotify_path"], "/opt/spotify/")

    def test_wanted(self):
        old = core.STATE_FILE
        core.STATE_FILE = os.path.join(self.d, "s/state.json")
        try:
            self.assertFalse(core.wanted("vencord"))
            core.adopt("vencord", True)
            self.assertTrue(core.wanted("vencord"))
            core.set_wanted("vencord", False)
            core.adopt("vencord", True)  # an explicit "no" is not overridden
            self.assertFalse(core.wanted("vencord"))
        finally:
            core.STATE_FILE = old


class Pending(unittest.TestCase):
    def test_nothing(self):
        self.assertEqual(core.pending(status()), [])

    def test_everything(self):
        st = status(steam={"client_latest": "101"}, millennium={"linked": False},
                    discord={"host_latest": "1.0.163"}, vencord={"installed": False},
                    spotify={"latest": "1:1.3.5-1"}, spicetify={"applied": False})
        self.assertEqual(core.pending(st), ["Steam", "Millennium", "Discord", "Vencord", "Spotify", "Spicetify"])

    def test_removed_mod_is_not_pending(self):
        st = status(vencord={"installed": False, "wanted": False}, spicetify={"applied": False, "wanted": False})
        self.assertEqual(core.pending(st), [])


if __name__ == "__main__":
    unittest.main()
