"""The fixture recorder only calls endpoints the real iPipe client still has (REL-001)."""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

from clients.ipipe_client import IpipeApiClient

RECORDER = Path(__file__).resolve().parent / "fixtures" / "platform" / "record.py"


class RecorderTargetTests(unittest.TestCase):
    def test_every_target_endpoint_exists_on_the_real_client(self):
        spec = importlib.util.spec_from_file_location("fixture_recorder", RECORDER)
        recorder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recorder)
        missing = [endpoint for _id, endpoint, _source, _read in recorder.TARGETS
                   if not callable(getattr(IpipeApiClient, endpoint, None))]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
