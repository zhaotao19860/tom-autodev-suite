import unittest
from pathlib import Path


LEGACY_SCRIPTS = [
    Path("/Users/tom/icode/td5/.comate/skills/td5-npl-dev/scripts/docker_build.sh"),
    Path("/Users/tom/icode/td5/.comate/skills/td5-npl-dev/scripts/build_and_capture.sh"),
]


class LegacyTd5GuardTests(unittest.TestCase):
    def test_scripts_reject_darwin_before_execution(self):
        for script in LEGACY_SCRIPTS:
            content = script.read_text(encoding="utf-8")
            guard_region = content.split("set -", maxsplit=1)[0]
            with self.subTest(script=script.name):
                self.assertIn("Darwin", guard_region)
                self.assertIn("LOCAL_EXECUTION_FORBIDDEN", guard_region)


if __name__ == "__main__":
    unittest.main()
