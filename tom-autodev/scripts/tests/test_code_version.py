import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_watch import ApprovalWatcher  # noqa: E402
from code_version import changed_since, fingerprint  # noqa: E402
from ipipe_watch import IpipeWatcher  # noqa: E402


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "a.py").write_text("x = 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_a.py").write_text("y = 1\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_editing_a_source_file_changes_the_fingerprint(self):
        before = fingerprint(self.root)
        (self.root / "a.py").write_text("x = 2\n")
        self.assertNotEqual(fingerprint(self.root), before)

    def test_tests_and_touches_do_not_count(self):
        before = fingerprint(self.root)
        (self.root / "tests" / "test_a.py").write_text("y = 2\n")
        (self.root / "a.py").touch()
        self.assertEqual(fingerprint(self.root), before)

    def test_adding_or_removing_a_module_counts(self):
        before = fingerprint(self.root)
        (self.root / "b.py").write_text("")
        added = fingerprint(self.root)
        self.assertNotEqual(added, before)
        (self.root / "b.py").unlink()
        self.assertEqual(fingerprint(self.root), before)

    def test_changed_since_reports_the_change(self):
        check = changed_since(fingerprint(self.root), self.root)
        self.assertFalse(check())
        (self.root / "a.py").write_text("x = 3\n")
        self.assertTrue(check())


class _Recorder:
    def __init__(self, flips_after):
        self.ticks = 0
        self.flips_after = flips_after

    def changed(self):
        return self.ticks >= self.flips_after


class WatcherStopTests(unittest.TestCase):
    def _run(self, cls, factory_args):
        recorder = _Recorder(flips_after=2)
        watcher = cls.__new__(cls)
        cls.__init__(watcher, *factory_args, sleeper=lambda _s: None,
                     code_changed=recorder.changed)

        def tick():
            recorder.ticks += 1
            return []
        watcher.tick = tick
        watcher.run(1, iterations=None)
        return watcher, recorder

    def test_approval_watcher_stops_once_code_changes(self):
        watcher, recorder = self._run(ApprovalWatcher, (object(), lambda: None, None))
        self.assertTrue(watcher.stopped_for_code_change)
        self.assertEqual(recorder.ticks, 2)

    def test_ipipe_watcher_stops_once_code_changes(self):
        watcher, recorder = self._run(IpipeWatcher, (object(), lambda _r: None, None))
        self.assertTrue(watcher.stopped_for_code_change)
        self.assertEqual(recorder.ticks, 2)

    def test_without_a_check_the_watcher_keeps_its_old_contract(self):
        watcher = ApprovalWatcher(object(), lambda: None, None, sleeper=lambda _s: None)
        watcher.tick = lambda: []
        self.assertEqual(watcher.run(1, iterations=3), [])
        self.assertFalse(watcher.stopped_for_code_change)


if __name__ == "__main__":
    unittest.main()
