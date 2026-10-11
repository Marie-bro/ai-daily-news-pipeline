"""Local loopback fixtures only; never opens saved production data."""
import json
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit
import unittest

from ai_daily_pipeline.run_audit import write_viewer
from ai_daily_pipeline.sources_server import SourcesHandler

ROOT = Path(__file__).resolve().parents[1]
EDGE = next((p for p in (Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                         Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")) if p.is_file()), None)
DAY = "2026-10-02"
IDENT = "a" * 64
PROBE = """
let count=0;
const timer=setInterval(()=>{
  if(!document.querySelector('#inspection table') && document.querySelector('#inspection') && ++count<80)return;
  clearInterval(timer);
  document.querySelectorAll('details').forEach(node=>node.open=true);
  setTimeout(()=>{
    document.querySelectorAll('details').forEach(node=>node.open=true);
    document.body.dataset.auditReady=String(document.body.textContent.includes('Today / Reserve'));
    document.body.dataset.overflow=String(document.documentElement.scrollWidth>document.documentElement.clientWidth);
    document.body.dataset.traceLink=String(Boolean(document.querySelector('a[href*="trace_id="],a[href^="#"]')));
  },100);
},25);
"""


@unittest.skipUnless(EDGE, "Edge required for actual layout check")
class AuditBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        root = Path(cls.temp.name)
        (root / "config").mkdir()
        shutil.copyfile(ROOT / "config/sources.json", root / "config/sources.json")
        (root / "data/run-audits").mkdir(parents=True)
        data = {"run_id":"fixture","date":DAY,"end_time":DAY+"T01:00:00+00:00",
                "sources":[{"source_id":"deepseek-news","status":"ok","items":1}],
                "flows":{"inventory_candidate":{"selected_candidate":{"count":1,"article_ids":[IDENT]},
                                                     "candidate_pool":{"count":1,"article_ids":[IDENT]}}},
                "metrics":{"today_locked":1,"today_lock_preserved":True,"published_article_ids":[]},
                "articles":[{"article_id":IDENT,"trace_id":IDENT,"source_id":"deepseek-news",
                    "source":"Test source","title":"中文标题 " + "long"*80,
                    "published_at":DAY+"T00:00:00+00:00","original_url":"https://example.com/article",
                    "events":[{"stage":"final_selection","status":"kept","reason":"selected",
                               "detail":{"supply_layer":"today","locked_today":True,"long":IDENT*25}}]}]}
        target = root / "data/run-audits/fixture.json"
        target.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
        viewer = write_viewer(target,data)

        class Handler(SourcesHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/test-probe.js":
                    return self._send(200,PROBE.encode(),"text/javascript")
                if path in {"/sources","/audit"}:
                    filename = ROOT / "ai_daily_pipeline/sources_page.html" if path == "/sources" else viewer
                    body = filename.read_text(encoding="utf-8").replace("</body>",'<script src="/test-probe.js"></script></body>')
                    if path == "/audit":
                        body += '<script src="/test-probe.js"></script>'
                        # Standalone viewer has inline styles, unlike Sources CSP.
                        self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.end_headers()
                        self.wfile.write(body.encode("utf-8"));return
                    return self._send(200,body.encode("utf-8"),"text/html; charset=utf-8")
                return super().do_GET()
        Handler.root = root
        cls.server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
        cls.thread = Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2);cls.temp.cleanup()

    def test_sources_and_standalone_audit_have_no_page_overflow(self):
        for route in (f"/sources?date={DAY}&trace_id={IDENT}","/audit"):
            for width in (390,800,1280):
                with self.subTest(route=route,width=width), TemporaryDirectory() as profile:
                    result = subprocess.run([str(EDGE),"--headless=new","--disable-gpu","--no-first-run",
                        f"--user-data-dir={profile}",f"--window-size={width},1200","--virtual-time-budget=4000",
                        "--dump-dom",f"http://127.0.0.1:{self.server.server_port}{route}"],capture_output=True,timeout=25)
                    self.assertEqual(result.returncode,0)
                    html = result.stdout.decode("utf-8",errors="replace")
                    for name,value in (("audit-ready","true"),("overflow","false"),("trace-link","true")):
                        self.assertRegex(html,fr'data-{name}="{value}"')
