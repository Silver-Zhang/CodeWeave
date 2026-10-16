import subprocess
import tempfile
import unittest
from pathlib import Path
from create_fixture_repo import create

class FixtureTest(unittest.TestCase):
    def test_two_commits_with_expected_delta(self):
        with tempfile.TemporaryDirectory(prefix="codeweave-fixture-test-") as d:
            repo = Path(d) / "repo"
            before, after = create(repo)
            self.assertNotEqual(before, after)
            output = subprocess.check_output(
                ["git", "diff", "--name-only", before, after], cwd=repo, text=True
            )
            self.assertEqual(output.strip(), "main.cpp")
            changed = subprocess.check_output(
                ["git", "show", f"{after}:main.cpp"], cwd=repo, text=True
            )
            self.assertIn("int clamp(int value)", changed)

if __name__ == "__main__":
    unittest.main()
