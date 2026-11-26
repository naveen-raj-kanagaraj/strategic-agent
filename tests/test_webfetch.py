"""Web jobs: Liquipedia through its API, images grouped by section/tab/table, run_python with web helpers + zip.

Run from the my-agent folder:  python -m unittest tests.test_webfetch -v      (no internet needed)
"""
import functools
import http.server
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import webfetch  # noqa: E402

# Trimmed copy of how a Liquipedia tournament page looks through the API (team templates, tabs, group tables).
LP_HTML = """
<div class="mw-parser-output">
<div class="mw-heading mw-heading2"><h2 id="Participants">Participants</h2></div>
<div class="mw-heading mw-heading3"><h3 id="Grand_Finals">Grand Finals</h3></div>
<div class="teamcard"><center><b>
 <span class="team-template-team-standard">
  <span class="team-template-image-icon team-template-lightmode"><a href="/pubgmobile/ULF_Esports" title="ULF Esports">
   <img alt="ULF Esports" src="/commons/images/thumb/1/1a/ULF_Esports_lightmode.png/50px-ULF_Esports_lightmode.png"></a></span>
  <span class="team-template-image-icon team-template-darkmode"><a href="/pubgmobile/ULF_Esports" title="ULF Esports">
   <img alt="ULF Esports" src="/commons/images/thumb/2/2b/ULF_Esports_darkmode.png/50px-ULF_Esports_darkmode.png"></a></span>
 </span></b></center>
 <span class="flag"><img alt="Turkey" src="/commons/images/3/3c/Tr_hd.png"></span>
</div>
<div class="mw-heading mw-heading3"><h3 id="Gauntlet">Gauntlet</h3></div>
<div class="tabs-dynamic">
 <ul class="nav nav-tabs tabs"><li class="tab1 active">Group Green</li><li class="tab2">Group Red</li></ul>
 <div class="tabs-content">
  <div class="content1 active"><table class="wikitable"><tr><th colspan="2">Group Green</th></tr>
   <tr><td><span class="team-template-team-short"><span class="team-template-image-icon team-template-lightmode">
    <a href="/pubgmobile/Alter_Ego" title="Alter Ego"><img alt="Alter Ego" src="/commons/images/thumb/4/4d/Alter_Ego_allmode.png/25px-Alter_Ego_allmode.png"></a>
   </span></span></td></tr></table></div>
  <div class="content2"><table class="wikitable"><tr><th colspan="2">Group Red</th></tr>
   <tr><td><span class="team-template-team-short"><span class="team-template-image-icon team-template-lightmode">
    <a href="/pubgmobile/D%27Xavier" title="D'Xavier"><img alt="D'Xavier" src="/commons/images/thumb/5/5e/DXavier_lightmode.png/25px-DXavier_lightmode.png"></a>
   </span></span></td></tr></table></div>
 </div>
</div>
<div class="mw-heading mw-heading2"><h2 id="Prize_Pool">Prize Pool</h2></div>
<img alt="" class="lazy" src="data:image/gif;base64,R0lGOD" data-src="/commons/images/6/6f/Trophy.png">
</div>
"""
URL = "https://liquipedia.net/pubgmobile/PUBG_Mobile_Global_Championship/2026"


class Liquipedia(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="wfcache_")
        self.p = [mock.patch.object(webfetch, "CACHE_DIR", self.cache), mock.patch.object(webfetch, "_public", lambda u: None)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_page_links(self):
        self.assertEqual(webfetch.liquipedia_page(URL), ("pubgmobile", "PUBG_Mobile_Global_Championship/2026"))
        self.assertIsNone(webfetch.liquipedia_page("https://liquipedia.net/commons/images/1/1a/X.png"))
        self.assertIsNone(webfetch.liquipedia_page("https://example.com/pubgmobile/X"))

    def test_full_size(self):
        self.assertEqual(webfetch.full_size("https://liquipedia.net/commons/images/thumb/1/1a/A_b.png/50px-A_b.png"),
                         "https://liquipedia.net/commons/images/1/1a/A_b.png")
        self.assertEqual(webfetch.full_size("https://x.com/a.png"), "https://x.com/a.png")

    def test_reads_through_api_with_its_rules_and_caches(self):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None, **kw):
            calls.append((url, params, headers))
            r = mock.Mock(status_code=200)
            r.json.return_value = {"parse": {"title": "x", "text": LP_HTML}}
            return r
        waits = []
        with mock.patch.object(webfetch.requests, "get", fake_get), \
                mock.patch.object(webfetch, "_wait", lambda k, g: waits.append((k, g))):
            html = webfetch.get(URL)
            again = webfetch.get(URL)
        self.assertEqual(html, again)
        self.assertEqual(len(calls), 1, "second read should come from the cache")
        url, params, headers = calls[0]
        self.assertEqual(url, "https://liquipedia.net/pubgmobile/api.php")
        self.assertEqual(params["action"], "parse")
        self.assertEqual(params["page"], "PUBG_Mobile_Global_Championship/2026")
        self.assertIn("PesAgent", headers["User-Agent"])
        self.assertIn("gzip", headers["Accept-Encoding"])
        self.assertEqual(waits, [("liquipedia_parse", 30)])

    def test_images_grouped_by_stage(self):
        with mock.patch.object(webfetch, "get", lambda url, **k: LP_HTML):
            imgs = webfetch.images_by_section(URL)
            teams = [i for i in imgs if i["kind"] == "team"]
            outline = webfetch.outline(URL, kind="team")
        self.assertEqual([t["name"] for t in teams], ["ULF Esports", "Alter Ego", "D'Xavier"])
        ulf, ego, dx = teams
        self.assertEqual(ulf["section"], "Participants > Grand Finals")
        self.assertEqual(ulf["variant"], "light")                   # dark duplicate dropped
        self.assertEqual(ulf["src"], "https://liquipedia.net/commons/images/1/1a/ULF_Esports_lightmode.png")
        self.assertEqual((ego["section"], ego["tab"], ego["table"]), ("Participants > Gauntlet", "Group Green", "Group Green"))
        self.assertEqual((dx["tab"], dx["table"]), ("Group Red", "Group Red"))
        flag = [i for i in imgs if i["name"] == "Turkey"][0]
        self.assertEqual(flag["kind"], "flag")
        trophy = [i for i in imgs if i["src"].endswith("Trophy.png")][0]   # lazy image: real address used
        self.assertEqual(trophy["section"], "Prize Pool")
        self.assertIn("Participants > Grand Finals  [1]: ULF Esports", outline)
        self.assertIn("Group Red | Group Red  [1]: D'Xavier", outline)
        with mock.patch.object(webfetch, "get", lambda url, **k: LP_HTML):
            raw = webfetch.images_by_section(URL, dedupe=False)
        self.assertEqual([i["variant"] for i in raw if i["name"] == "ULF Esports"], ["light", "dark"])


class ReadUrl(unittest.TestCase):
    def test_liquipedia_goes_through_api(self):
        from core import web
        with mock.patch.object(web, "_is_public_host", lambda h: True), \
                mock.patch.object(webfetch, "get", lambda url, **k: LP_HTML * 3):
            out = web.read_url(URL)
        self.assertIn("read through its API", out)
        self.assertIn("Grand Finals", out)

    def test_refused_site_gets_a_second_try(self):
        from core import web
        refused = mock.Mock(status_code=403, is_redirect=False, headers={}, url="https://example.com/a")
        refused.iter_content = lambda n: [b""]
        page = "<html><head><title>Hi</title></head><body><p>" + "Real article text. " * 30 + "</p></body></html>"
        with mock.patch.object(web, "_is_public_host", lambda h: True), \
                mock.patch.object(web.requests, "get", lambda *a, **k: refused), \
                mock.patch.object(webfetch, "get", lambda url, **k: page):
            out = web.read_url("https://example.com/a")
        self.assertIn("Real article text", out)


class Downloads(unittest.TestCase):
    """save() over real HTTP from a local server."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="wf_srv_")
        with open(os.path.join(cls.dir, "logo.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 5000)

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=cls.dir))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_save_into_new_folders(self):
        out = tempfile.mkdtemp(prefix="wf_out_")
        try:
            with mock.patch.object(webfetch, "_public", lambda u: None):
                path = webfetch.save(f"{self.base}/logo.png", os.path.join(out, "Logos", "Grand Finals", "ULF Esports"))
            self.assertTrue(path.endswith("ULF Esports.png"))
            self.assertEqual(os.path.getsize(path), 5008)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_private_addresses_refused(self):
        with self.assertRaises(webfetch.FetchError):
            webfetch.get(f"{self.base}/logo.png", binary=True)


class RunPython(unittest.TestCase):
    """The agent's computer: helpers are there, folders stay out, the zip is sent."""

    def test_zip_job(self):
        from core import analyst, context
        root = tempfile.mkdtemp(prefix="wf_files_")
        try:
            with mock.patch.object(analyst, "ROOT", root):
                context.set_ctx(context.Ctx(uid="owner", platform="telegram", is_owner=True))
                code = '''
import shutil
print("helpers:", all(callable(f) for f in (get, page, images_by_section, outline, save, safe_name)))
for stage, team in [("Grand Finals", "ULF Esports"), ("Gauntlet/Group Green", "Alter Ego")]:
    os.makedirs(f"PMGC_logos/{stage}", exist_ok=True)
    open(f"PMGC_logos/{stage}/{safe_name(team)}.png", "wb").write(b"png")
for i in range(8):
    open(f"scratch_{i}.txt", "w").write("x")
shutil.make_archive("PMGC_2026_logos", "zip", "PMGC_logos")
print("done")
'''
                out = analyst.run_python(code)
                sent = [os.path.basename(o["path"]) for o in context.get().outbox]
        finally:
            context.set_ctx(context.Ctx())
            shutil.rmtree(root, ignore_errors=True)
        self.assertIn("helpers: True", out)
        self.assertEqual(sent[0], "PMGC_2026_logos.zip")          # the deliverable first, even with clutter
        self.assertNotIn("PMGC_logos", sent)                         # folders are not sent

    def test_zip_contents(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(f"{tmp}/src/Grand Finals")
            open(f"{tmp}/src/Grand Finals/ULF Esports.png", "wb").write(b"png")
            shutil.make_archive(f"{tmp}/out", "zip", f"{tmp}/src")
            self.assertEqual(zipfile.ZipFile(f"{tmp}/out.zip").namelist(), ["Grand Finals/", "Grand Finals/ULF Esports.png"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Routing(unittest.TestCase):
    def test_jobs_use_the_deeper_model_with_more_steps(self):
        from core import agent, context
        model, _ = agent.pick_model(True, f"{URL} download logos, sort them by group and send as zip")
        self.assertEqual(model, agent.SMART_MODEL)
        self.assertEqual(agent.pick_model(True, "hi")[0], agent.FAST_MODEL)
        self.assertEqual(agent.pick_model(False, "download all logos")[0], agent.FAST_MODEL)
        context.set_ctx(context.Ctx(is_owner=True))
        self.assertEqual(agent._rounds(), agent.OWNER_TOOL_ROUNDS)
        context.set_ctx(context.Ctx(is_owner=False))
        self.assertEqual(agent._rounds(), agent.MAX_TOOL_ROUNDS)
        context.set_ctx(context.Ctx())


if __name__ == "__main__":
    unittest.main()
