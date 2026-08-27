import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from install_links import install_links


class InstallLinksTests(unittest.TestCase):
    def test_dry_run_reports_create_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            (source / "skill-a").mkdir(parents=True)

            actions = install_links(source, [destination], ["skill-a"], dry_run=True)

            self.assertEqual(actions[0]["action"], "CREATE")
            self.assertFalse((destination / "skill-a").exists())

    def test_real_directory_is_a_conflict_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            (source / "skill-a").mkdir(parents=True)
            (destination / "skill-a").mkdir(parents=True)

            actions = install_links(source, [destination], ["skill-a"], dry_run=False)

            self.assertEqual(actions[0]["action"], "CONFLICT")
            self.assertTrue((destination / "skill-a").is_dir())
            self.assertFalse((destination / "skill-a").is_symlink())


if __name__ == "__main__":
    unittest.main()
