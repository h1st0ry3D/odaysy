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

    def run_helper(self, *arguments, expected=0):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(HELPER), *arguments],
            env=self.environment, capture_output=True, text=True)
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


if __name__ == "__main__":
    unittest.main()
