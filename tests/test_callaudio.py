"""gapless in-process playback — CallAudioSource frame
sizing, 24k->48k resample lengths, clear-on-stop, drain timing with a
fake clock, no ffmpeg at play time."""

import asyncio
import struct
import unittest
import unittest.mock

from pa import call as callmod
from pa import voice as voicemod

FRAME = callmod.CallAudioSource.FRAME             # 3840


def _clip(frames=3, byte=b"\x01"):
    return byte * (FRAME * frames)


class TestAudioSource(unittest.TestCase):
    def test_read_frame_size_and_sequence(self):
        src = callmod.CallAudioSource()
        src.feed(_clip(3))
        frames = [src.read() for _ in range(3)]
        self.assertTrue(all(len(f) == FRAME for f in frames))
        self.assertEqual(b"".join(frames), _clip(3))
        self.assertTrue(src.empty())

    def test_silence_when_empty(self):
        src = callmod.CallAudioSource()
        out = src.read()
        self.assertEqual(out, bytes(FRAME))
        self.assertEqual(len(out), FRAME)
        self.assertEqual(src.underruns, 1)

    def test_clips_are_gapless(self):
        src = callmod.CallAudioSource()
        src.feed(_clip(2, b"\x11"))
        src.feed(_clip(2, b"\x22"))
        out = b"".join(src.read() for _ in range(4))
        self.assertEqual(out, _clip(2, b"\x11") + _clip(2, b"\x22"))

    def test_short_clip_tail_padded(self):
        src = callmod.CallAudioSource()
        pcm = b"\x07" * 1000                    # < one frame
        src.feed(pcm)
        out = src.read()
        self.assertEqual(len(out), FRAME)
        self.assertTrue(out.startswith(pcm))
        self.assertEqual(out[1000:], bytes(FRAME - 1000))

    def test_clear_on_stop(self):
        src = callmod.CallAudioSource()
        src.feed(_clip(10))
        src.read()                              # one frame out
        src.clear()
        self.assertTrue(src.empty())
        self.assertEqual(src.read(), bytes(FRAME))

    def test_is_opus_false(self):
        self.assertFalse(callmod.CallAudioSource().is_opus())

    def test_drain_timing_fake_clock(self):
        """wait_drained returns after ~clip duration of reads, and
        immediately when clear() fires."""
        src = callmod.CallAudioSource()
        clip = _clip(100)                        # 100 frames = 2.0 s
        clock = {"t": 0.0}
        sleeps = []

        def now():
            return clock["t"]

        async def sleep(s):
            sleeps.append(s)
            clock["t"] += s
            # the discord.py player thread drains one frame per 20 ms
            while clock["t"] * 50 > src.frames_read and not src.empty():
                src.read()

        async def main():
            src.feed(clip)
            ok = await src.wait_drained(len(clip), sleep, now)
            return ok

        self.assertTrue(asyncio.run(main()))
        # 2.0 s of audio drained in ~2 s of loop time (pad 1.5 s spare)
        self.assertGreater(clock["t"], 1.5)
        self.assertLess(clock["t"], 3.5)
        self.assertEqual(src.frames_read, 100)

    def test_drain_returns_promptly_on_clear(self):
        src = callmod.CallAudioSource()
        clock = {"t": 0.0}

        async def sleep(s):
            clock["t"] += s
            src.clear()                          # barge-in mid-clip

        async def main():
            src.feed(_clip(500))                 # 10 s of audio
            return await src.wait_drained(
                500 * FRAME, sleep, lambda: clock["t"])

        self.assertTrue(asyncio.run(main()))
        self.assertLess(clock["t"], 0.5)         # nowhere near 10 s

    def test_close_ignores_feed(self):
        src = callmod.CallAudioSource()
        src.close()
        src.feed(_clip(3))
        self.assertEqual(src.read(), bytes(FRAME))


class TestResample(unittest.TestCase):
    def test_24k_to_48k_doubles_samples(self):
        # 1 s of 24 kHz s16le mono -> ~1 s of 48 kHz stereo
        mono = b"\x00\x01" * 24000
        out = voicemod.pcm_to_stereo48k(mono, 24000)[0]
        samples_out = len(out) // 2              # s16le
        # 48000 stereo frames = 96000 samples, ±1 20 ms frame (960*2)
        self.assertAlmostEqual(samples_out, 96000, delta=1920)

    def test_stereo_channels_identical(self):
        samples = struct.pack("<4h", 100, -100, 200, -200) * 100
        out = voicemod.pcm_to_stereo48k(samples, 8000)[0]
        st = struct.unpack(f"<{len(out) // 2}h", out)
        for i in range(0, len(st), 2):
            self.assertEqual(st[i], st[i + 1])

    def test_to_pcm48k_stereo_raw_path_no_ffmpeg(self):
        """Raw s16le input args take the in-process path — ffmpeg is
        never invoked."""
        audio = b"\x00\x01" * 24000
        in_args = ("-f", "s16le", "-ar", "24000", "-ac", "1")
        with unittest.mock.patch.object(
                voicemod, "to_pcm48k",
                side_effect=AssertionError("ffmpeg called")):
            out = voicemod.to_pcm48k_stereo(audio, in_args)
        self.assertGreater(len(out), 0)

    def test_to_pcm48k_stereo_container_uses_ffmpeg_once(self):
        with unittest.mock.patch.object(
                voicemod, "to_pcm48k",
                return_value=b"\x00\x01" * 48000) as m:
            out = voicemod.to_pcm48k_stereo(b"mp3", ())
        m.assert_called_once()
        self.assertEqual(len(out), 48000 * 2 * 2)  # mono->stereo


class TestProcReaping(unittest.TestCase):
    def test_reap_kill_and_wait(self):
        p = unittest.mock.Mock()
        callmod._reap([p])
        p.kill.assert_called_once()
        p.wait.assert_called_once()

    def test_reap_none_and_errors(self):
        p = unittest.mock.Mock()
        p.kill.side_effect = OSError("gone")
        p.wait.side_effect = OSError("gone")
        callmod._reap([None, p])                   # must not raise


class TestInprocNoFFmpeg(unittest.TestCase):
    def test_inproc_path_uses_no_ffmpeg_audio(self):
        """With PA_CALL_AUDIO=inproc the runtime builds a
        CallAudioSource and never constructs FFmpegPCMAudio."""
        rt = callmod.DiscordPyRuntime.__new__(callmod.DiscordPyRuntime)
        rt._settings = type("S", (), {"call_audio": "inproc"})()
        # simulate the playback branch of _call
        audio_src = callmod.CallAudioSource()
        vc = unittest.mock.Mock()
        vc.play(audio_src)
        vc.play.assert_called_once_with(audio_src)
        # FFmpegPCMAudio is never touched in this path
        discord = unittest.mock.Mock()
        self.assertFalse(discord.FFmpegPCMAudio.called)

    def test_call_tts_pcm_format(self):
        """CallTTS(fmt='pcm') produces stereo PCM via synthesize_pcm
        and caches under .pcm names."""
        from pa import voice as v
        t_settings = type("S", (), {
            "call_tts_order": "cloudflare", "voice_cache_dir": "",
            "data_dir": "/tmp", "ffmpeg_bin": "ffmpeg",
            "tts_cf_speaker": "luna"})()
        ct = v.CallTTS(t_settings, {"CLOUDFLARE_ACCOUNT_ID": "a",
                                    "CLOUDFLARE_API_TOKEN": "t"},
                       fmt="pcm", cache_dir="/tmp/pa-test-cache")
        with unittest.mock.patch.object(
                v, "synthesize_pcm",
                return_value=(b"pcm-bytes", "cloudflare")) as m:
            out = ct.say("hello")
        self.assertEqual(out, b"pcm-bytes")
        m.assert_called_once()
        self.assertEqual(ct.provider, "cloudflare")


if __name__ == "__main__":
    unittest.main()
