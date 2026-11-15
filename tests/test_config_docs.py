import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestConfigDocs(unittest.TestCase):
    def test_config_docs_up_to_date(self):
        """docs/CONFIG.md must be regenerated after .env.example or
        pa/config.py env reads change — scripts/gen_config_docs.py."""
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gen_config_docs.py"),
             "--check"],
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
