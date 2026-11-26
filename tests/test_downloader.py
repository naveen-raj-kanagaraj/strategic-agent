"""Downloader tests: format choice for YouTube, Instagram and X, and a real download + merge from a local server.

Run from the my-agent folder:  python -m unittest tests.test_downloader -v
No internet needed. The download tests need ffmpeg (brew install ffmpeg) and are skipped without it.
"""
import functools
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import downloader  # noqa: E402

MB = 1024 * 1024
BUDGET = 48 * MB


def v(fid, w, h, codec="avc1.640028", size=None, tbr=None, ext="mp4"):
    return {"format_id": fid, "width": w, "height": h, "vcodec": codec, "acodec": "none", "ext": ext,
            "filesize": size, "tbr": tbr, "protocol": "https"}


def a(fid, codec="mp4a.40.2", abr=128, size=None, ext="m4a", lang_pref=None):
    return {"format_id": fid, "vcodec": "none", "acodec": codec, "abr": abr, "tbr": abr, "ext": ext,
            "filesize": size, "language_preference": lang_pref, "protocol": "https"}


def both(fid, w, h, vcodec="avc1.42001E", acodec="mp4a.40.2", size=None, tbr=None):
    return {"format_id": fid, "width": w, "height": h, "vcodec": vcodec, "acodec": acodec, "ext": "mp4",
            "filesize": size, "tbr": tbr, "protocol": "https"}


def youtube(duration=60):
    return {"duration": duration, "formats": [
        {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none", "protocol": "mhtml"},
        a("139", abr=48, size=0.4 * MB), a("140", abr=129, size=1 * MB), a("140-drc", abr=129, size=1 * MB),
        a("251", codec="opus", abr=140, size=1.1 * MB, ext="webm"),
        both("18", 640, 360, size=3 * MB),
        v("134", 640, 360, size=2 * MB), v("136", 1280, 720, size=6 * MB), v("137", 1920, 1080, size=12 * MB),
        v("248", 1920, 1080, codec="vp9", size=8 * MB, ext="webm"),
        v("401", 3840, 2160, codec="av01.0.12M.08", size=60 * MB),
    ]}


class ChooseFormats(unittest.TestCase):
    def test_youtube_gets_1080p_h264_with_aac(self):
        picks = downloader.choose_formats(youtube(), True, BUDGET)
        self.assertEqual(picks[0][0], "137+140")          # 1080p H.264 + AAC, not VP9, not 4K, not the drc track
        self.assertIn("1080p", picks[0][1])

    def test_youtube_without_ffmpeg_falls_back_to_single_file(self):
        picks = downloader.choose_formats(youtube(), False, BUDGET)
        self.assertEqual(picks[0][0], "18")

    def test_long_video_steps_down_to_fit(self):
        info = youtube()
        for f in info["formats"]:
            if f["format_id"] == "137":
                f["filesize"] = 60 * MB
        picks = downloader.choose_formats(info, True, BUDGET)
        self.assertEqual(picks[0][0], "136+140")          # 1080p too big: 720p
        self.assertNotIn("137+140", [p[0] for p in picks])

    def test_youtube_original_language_audio_wins(self):
        info = youtube()
        info["formats"] += [a("140-dub", abr=129, size=1 * MB, lang_pref=-1)]
        for f in info["formats"]:
            if f["format_id"] == "140":
                f["language_preference"] = 10
        self.assertEqual(downloader.choose_formats(info, True, BUDGET)[0][0], "137+140")

    def test_instagram_dash_reel_is_merged(self):
        info = {"duration": 30, "formats": [
            both("progressive", 480, 852, vcodec=None, acodec=None, tbr=900),
            {"format_id": "dash-audio", "vcodec": "none", "acodec": "mp4a.40.5", "tbr": 64, "ext": "m4a"},
            {"format_id": "dash-720", "width": 720, "height": 1280, "vcodec": "avc1.4d401f", "acodec": "none",
             "tbr": 1500, "ext": "mp4"},
            {"format_id": "dash-1080", "width": 1080, "height": 1920, "vcodec": "avc1.640028", "acodec": "none",
             "tbr": 3500, "ext": "mp4"},
        ]}
        self.assertEqual(downloader.choose_formats(info, True, BUDGET)[0][0], "dash-1080+dash-audio")

    def test_x_picks_highest_bitrate_single_file(self):
        info = {"duration": 30, "formats": [
            {"format_id": "hls-256", "width": 480, "height": 270, "tbr": 256, "ext": "mp4", "protocol": "m3u8_native"},
            {"format_id": "http-832", "width": 640, "height": 360, "tbr": 832, "ext": "mp4", "protocol": "https"},
            {"format_id": "http-2176", "width": 1280, "height": 720, "tbr": 2176, "ext": "mp4", "protocol": "https"},
            {"format_id": "http-10368", "width": 1920, "height": 1080, "tbr": 10368, "ext": "mp4",
             "protocol": "https"},
        ]}
        self.assertEqual(downloader.choose_formats(info, True, BUDGET)[0][0], "http-10368")   # 30 s 1080p ~39 MB
        info["duration"] = 120   # 2 minutes: 1080p would be ~155 MB, 720p ~33 MB fits
        self.assertEqual(downloader.choose_formats(info, True, BUDGET)[0][0], "http-2176")


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class RealDownload(unittest.TestCase):
    """Serves small generated files over HTTP and runs the real yt-dlp download + ffmpeg merge."""

    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.mkdtemp(prefix="dltest_media_")
        ff = ["ffmpeg", "-loglevel", "error", "-y"]
        run = functools.partial(subprocess.run, check=True)
        run(ff + ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-t", "4", "-an", "-c:v", "libx264",
                  "-b:v", "6M", "-pix_fmt", "yuv420p", f"{cls.media}/v720.mp4"])
        run(ff + ["-f", "lavfi", "-i", "sine=frequency=440", "-t", "4", "-vn", "-c:a", "aac", "-b:a", "128k",
                  f"{cls.media}/a.m4a"])
        run(ff + ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i", "sine", "-t", "4",
                  "-c:v", "libx264", "-b:v", "300k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                  f"{cls.media}/both360.mp4"])
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(Quiet, directory=cls.media)
        class Server(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass   # yt-dlp hangs up on purpose when a file is over the size limit
        cls.server = Server(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.media, ignore_errors=True)

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="dltest_out_")
        self._max = downloader.MAX_MB, downloader.MAX_FILE_SIZE

    def tearDown(self):
        downloader.MAX_MB, downloader.MAX_FILE_SIZE = self._max
        shutil.rmtree(self.out, ignore_errors=True)

    def info(self, sizes=None):
        """A processed info dict, as extract_info(download=False) returns it."""
        sizes = sizes or {}
        size = lambda name: sizes.get(name, os.path.getsize(f"{self.media}/{name}"))  # noqa: E731
        raw = {"id": "testvid", "title": "Test video", "duration": 4, "extractor": "test", "extractor_key": "Generic",
               "webpage_url": f"{self.base}/page", "formats": [
                   {**both("360", 640, 360, size=size("both360.mp4")), "url": f"{self.base}/both360.mp4",
                    "protocol": "http"},
                   {**v("720", 1280, 720, size=size("v720.mp4")), "url": f"{self.base}/v720.mp4", "protocol": "http"},
                   {**a("aac", size=size("a.m4a")), "url": f"{self.base}/a.m4a", "protocol": "http"},
               ]}
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.process_ie_result(raw, download=False)

    def streams(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,height", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True).stdout.split()
        return sorted(out)

    def test_merges_720p_video_and_audio(self):
        path, _, label = downloader.download_best(self.info(), self.out, downloader._base_opts(self.out))
        self.assertTrue(str(path).endswith(".mp4"), path)
        self.assertEqual(self.streams(path), ["audio", "video,720"])
        self.assertIn("720p video + audio", label)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["testvid.mp4"])   # pieces cleaned up

    def test_without_ffmpeg_uses_single_file(self):
        base = downloader._base_opts(self.out)
        base.pop("ffmpeg_location", None)
        base.pop("merge_output_format", None)
        path, _, label = downloader.download_best(self.info(), self.out, base)
        self.assertEqual(self.streams(path), ["audio", "video,360"])

    def test_size_was_understated_so_it_steps_down(self):
        real = os.path.getsize(f"{self.media}/v720.mp4")
        downloader.MAX_MB = 3                 # aim for ~1 MB
        downloader.MAX_FILE_SIZE = 2 * MB      # hard limit 2 MB
        self.assertGreater(real, 2 * MB, "test video should be bigger than the limit")
        # the site claims the 720p stream is tiny; the real file is over the limit, so 360p is used instead
        path, _, label = downloader.download_best(self.info({"v720.mp4": 100_000}), self.out,
                                                  {**downloader._base_opts(self.out), "max_filesize": 2 * MB})
        self.assertEqual(self.streams(path), ["audio", "video,360"])
        self.assertLessEqual(path.stat().st_size, 2 * MB)


class OwnerOnly(unittest.TestCase):
    def test_tool_hidden_from_other_people(self):
        from core import tools
        self.assertNotIn("download_video", [t["name"] for t in tools.tools_for(False)])
        self.assertIn("download_video", [t["name"] for t in tools.tools_for(True)])

    def test_tool_refuses_other_people(self):
        from core import context
        context.set_ctx(context.Ctx(platform="telegram", is_owner=False))
        try:
            self.assertIn("only available to the bot owner", downloader.tool_download_video("https://x.com/a/status/1"))
        finally:
            context.set_ctx(context.Ctx())

    def test_telegram_check(self):
        os.environ["OWNER_TELEGRAM_ID"] = "111"
        from platforms import telegram_bot
        owner = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=111))
        other = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=222))
        self.assertTrue(telegram_bot.can_download(owner))
        self.assertFalse(telegram_bot.can_download(other))


if __name__ == "__main__":
    unittest.main()
