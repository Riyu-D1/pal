""".env auto-load (pa/envfile.py): parse + apply rules, PA_ENV_FILE,
never overriding a var already in the environment."""

import os
import tempfile
import unittest

from pa import envfile


def _write(text):
    fd, path = tempfile.mkstemp(prefix="pa-env-", suffix=".env")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


class TestParse(unittest.TestCase):
    def test_shapes(self):
        cases = [
            ("A=1", ("A", "1")),
            ("A = 1", ("A", "1")),
            ("export A=1", ("A", "1")),
            ('A="a b # c"', ("A", "a b # c")),
            ("A='a b # c'", ("A", "a b # c")),
            ("A=1 # trailing", ("A", "1")),
            ("A=", ("A", "")),
            ("  A= spaced  ", ("A", "spaced")),
            ('A="unclosed', ("A", '"unclosed')),
            # not assignments
            ("# comment", None),
            ("", None),
            ("   ", None),
            ("A B=1", None),
            ("1A=1", None),
            ("no-equals", None),
            ("=v", None),
            ("A-B=1", None),
        ]
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(envfile.parse_line(line), want)


class TestLoad(unittest.TestCase):
    def test_applies_missing_only(self):
        path = _write("A=from-file\nB=from-file\n#C=x\n")
        env = {"A": "from-env"}
        n = envfile.load(path, environ=env)
        self.assertEqual(n, 1)
        self.assertEqual(env["A"], "from-env")   # never overrides
        self.assertEqual(env["B"], "from-file")

    def test_missing_file_is_fine(self):
        env = {}
        self.assertEqual(
            envfile.load("/nonexistent/pa.env", environ=env), 0)
        self.assertEqual(env, {})

    def test_pa_env_file_wins_over_default(self):
        path = _write("MARKER=custom\n")
        env = {"PA_ENV_FILE": path}
        self.assertEqual(envfile.load(environ=env), 1)
        self.assertEqual(env["MARKER"], "custom")

    def test_default_is_dotenv_in_cwd(self):
        d = tempfile.mkdtemp(prefix="pa-envcwd-")
        path = os.path.join(d, ".env")
        with open(path, "w") as fh:
            fh.write("MARKER=cwd\n")
        env = {}
        cwd = os.getcwd()
        try:
            os.chdir(d)
            self.assertEqual(envfile.load(environ=env), 1)
        finally:
            os.chdir(cwd)
        self.assertEqual(env["MARKER"], "cwd")

    def test_missing_dotenv_in_cwd(self):
        d = tempfile.mkdtemp(prefix="pa-envcwd-")
        env = {}
        cwd = os.getcwd()
        try:
            os.chdir(d)
            self.assertEqual(envfile.load(environ=env), 0)
        finally:
            os.chdir(cwd)
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
