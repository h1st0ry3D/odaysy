import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "odaysy_helper.py"


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="odaysy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.config_home = pathlib.Path(self.temporary.name)
        self.environment = {
            "HOME": self.temporary.name,
            "XDG_CONFIG_HOME": self.temporary.name,
            "PATH": "/usr/bin:/bin",
        }

    def run_helper(self, *arguments, expected=0, stdin=None):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(HELPER), *arguments],
            env=self.environment, capture_output=True, text=True, input=stdin)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def state(self):
        return json.loads(self.run_helper("list").stdout)

    def add(self, title):
        return json.loads(self.run_helper("add", "--", title).stdout)

    def test_starts_empty_without_writing(self):
        state = self.state()
        self.assertEqual(state["events"], [])
        self.assertEqual(state["pinnedId"], "")
        self.assertFalse((self.config_home / "odaysy" / "config.json").exists())

    def test_first_event_is_pinned_automatically(self):
        first = self.add("Quit smoking")
        identifier = first["events"][0]["id"]
        self.assertEqual(first["pinnedId"], identifier)

        second = self.add("No soda")
        self.assertEqual(second["pinnedId"], identifier)

    def test_add_rename_pin_reset_and_remove(self):
        first = self.add("Quit smoking")
        identifier = first["events"][0]["id"]
        self.assertEqual(first["events"][0]["title"], "Quit smoking")
        self.assertTrue(first["events"][0]["since"].endswith("Z"))

        second = self.add("No soda")
        self.assertEqual(len(second["events"]), 2)

        pinned = json.loads(self.run_helper("pin", "--", identifier).stdout)
        self.assertEqual(pinned["pinnedId"], identifier)

        renamed = json.loads(
            self.run_helper("rename", "--", identifier, "Quit nicotine").stdout)
        self.assertEqual(renamed["events"][0]["title"], "Quit nicotine")

        previous = renamed["events"][0]["since"]
        reset = json.loads(self.run_helper("reset", "--", identifier).stdout)
        self.assertNotEqual(reset["events"][0]["since"], previous)

        removed = json.loads(self.run_helper("remove", "--", identifier).stdout)
        self.assertEqual(removed["pinnedId"], "")
        self.assertEqual(len(removed["events"]), 1)
        self.assertEqual(removed["events"][0]["title"], "No soda")

    def test_titles_are_cleaned_and_limited(self):
        state = self.add("  A \x01 <b> & B  ")
        self.assertEqual(state["events"][0]["title"], "A <b> & B")
        self.run_helper("add", "--", "x" * 121, expected=1)

    def test_config_is_private_and_atomic(self):
        self.add("Quit smoking")
        directory = self.config_home / "odaysy"
        config = directory / "config.json"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertFalse(any(path.name.endswith(".tmp") for path in directory.iterdir()))

    def test_symlink_config_is_refused_and_not_touched(self):
        directory = self.config_home / "odaysy"
        directory.mkdir()
        victim = self.config_home / "victim"
        victim.write_text("do not touch")
        (directory / "config.json").symlink_to(victim)

        result = self.run_helper("list", expected=1)
        self.assertIn("symbolic", result.stderr)
        self.assertEqual(victim.read_text(), "do not touch")

    def test_malformed_config_is_not_overwritten(self):
        directory = self.config_home / "odaysy"
        directory.mkdir()
        config = directory / "config.json"
        original = '{"version": 1, "events": "nope"}\n'
        config.write_text(original)

        self.run_helper("add", "--", "Quit smoking", expected=1)
        self.assertEqual(config.read_text(), original)

    def test_invalid_timestamp_and_id_are_refused(self):
        directory = self.config_home / "odaysy"
        directory.mkdir()
        config = directory / "config.json"
        config.write_text(json.dumps({
            "version": 1,
            "pinnedId": "",
            "events": [{"id": "0123456789abcdef", "title": "A", "since": "yesterday"}],
        }))
        self.run_helper("list", expected=1)
        self.run_helper("rename", "--", "../../etc/passwd", "B", expected=1)


    def test_backup_and_restore_round_trip(self):
        self.add("Workout")
        self.add("Last drink")
        exported = self.config_home / "my backup.json"
        result = self.run_helper(
            "backup", "export", stdin='{"path": "%s"}\n' % exported)
        self.assertIn("Wrote", json.loads(result.stdout)["message"])
        self.assertTrue(exported.is_file())

        self.add("Temporary")
        self.assertEqual(len(self.state()["events"]), 3)

        result = self.run_helper(
            "backup", "restore", stdin='{"path": "%s"}\n' % exported)
        self.assertIn("Restored 2 events", json.loads(result.stdout)["message"])
        self.assertEqual([entry["title"] for entry in self.state()["events"]],
                         ["Workout", "Last drink"])
        backups = list((self.config_home / "odaysy" / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].name.startswith("pre-restore_"))

    def test_restore_refuses_a_foreign_or_empty_backup(self):
        self.add("Workout")
        before = self.state()
        empty = self.config_home / "empty.json"
        empty.write_text('{"version": 1, "pinnedId": "", "events": []}\n')
        self.run_helper(
            "backup", "restore", stdin='{"path": "%s"}\n' % empty, expected=1)
        self.assertEqual(self.state()["events"], before["events"])

        junk = self.config_home / "junk.json"
        junk.write_text("not json\n")
        self.run_helper(
            "backup", "restore", stdin='{"path": "%s"}\n' % junk, expected=1)
        self.assertEqual(self.state()["events"], before["events"])

    def test_backup_request_refuses_traversal(self):
        self.add("Workout")
        self.run_helper(
            "backup", "export",
            stdin='{"path": "/tmp/../etc/odaysy-backup.json"}\n', expected=1)

    def test_backup_follows_a_relative_directory_link(self):
        self.add("Workout")
        real = self.config_home / "real"
        real.mkdir()
        (self.config_home / "link").symlink_to("real")
        target = self.config_home / "link" / "backup.json"
        self.run_helper(
            "backup", "export", stdin='{"path": "%s"}\n' % target)
        self.assertTrue((real / "backup.json").is_file())


if __name__ == "__main__":
    unittest.main()
