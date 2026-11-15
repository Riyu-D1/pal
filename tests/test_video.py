"""lightweight video understanding (off by default)."""

import os
import unittest

from pa import video
from tests.helpers import TmpEnv


class TestVideo(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()

    def test_disabled_by_default(self):
        out = video.look(self.env.settings, self.env.env, "x.mp4")
        self.assertEqual(out["error"], "disabled")

    def test_sandbox_only(self):
        env = TmpEnv(PA_VIDEO="true")
        out = video.look(env.settings, env.env, "/etc/passwd.mp4")
        self.assertEqual(out["error"], "bad_arguments")
        out = video.look(env.settings, env.env, "../x.mp4")
        self.assertEqual(out["error"], "bad_arguments")

    def test_size_cap(self):
        env = TmpEnv(PA_VIDEO="true", PA_VIDEO_MAX_MB="1")
        big = os.path.join(env.settings.chat_sandbox, "big.mp4")
        os.makedirs(env.settings.chat_sandbox, exist_ok=True)
        with open(big, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024))
        out = video.look(env.settings, env.env, big)
        self.assertEqual(out["error"], "too_large")

    def test_no_ffmpeg(self):
        env = TmpEnv(PA_VIDEO="true")
        vid = os.path.join(env.settings.chat_sandbox, "v.mp4")
        os.makedirs(env.settings.chat_sandbox, exist_ok=True)
        with open(vid, "wb") as f:
            f.write(b"fakevideo")
        orig = video._have_ffmpeg
        video._have_ffmpeg = lambda: False
        try:
            out = video.look(env.settings, env.env, vid)
            self.assertEqual(out["error"], "no_ffmpeg")
        finally:
            video._have_ffmpeg = orig

    def test_summary_is_fenced(self):
        env = TmpEnv(PA_VIDEO="true")
        vid = os.path.join(env.settings.chat_sandbox, "v.mp4")
        os.makedirs(env.settings.chat_sandbox, exist_ok=True)
        with open(vid, "wb") as f:
            f.write(b"fakevideo")
        video._have_ffmpeg = lambda: True
        video._duration = lambda p: 8.0
        import subprocess as sp
        real_run = sp.run

        def fake_run(cmd, **kw):
            out = kw.get("capture_output") and None
            # fabricate the frame file ffmpeg would write
            dest = cmd[-2] if cmd[-2] != "-y" else cmd[-1]
            with open(dest, "wb") as f:
                f.write(b"\xff\xd8\xff" + b"x" * 50)
            class R:
                stdout = ""

            return R()

        orig_run = sp.run
        orig_desc = video.visionmod.describe_data
        sp.run = fake_run
        video.visionmod.describe_data = lambda s, e, d, n: \
            "<<<UNTRUSTED VISION CAPTION>>>\na cat\n<<<END>>>"
        try:
            out = video.look(env.settings, env.env, vid)
            self.assertEqual(out["frames"], 4)
            self.assertIn("UNTRUSTED VIDEO FRAMES V.MP4",
                          out["summary"])
            self.assertIn("a cat", out["summary"])
            self.assertEqual(out["duration_s"], 8.0)
        finally:
            sp.run = orig_run
            video.visionmod.describe_data = orig_desc


if __name__ == "__main__":
    unittest.main()
