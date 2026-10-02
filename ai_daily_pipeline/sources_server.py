"""Loopback-only viewer for saved source audits. No collection or model imports."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .sources_readonly import available_dates, sources_for_date, validate_date

ASSETS = {
    "/sources": ("sources_page.html", "text/html; charset=utf-8"),
    "/assets/sources.css": ("sources_page.css", "text/css; charset=utf-8"),
    "/assets/sources.js": ("sources_page.js", "text/javascript; charset=utf-8"),
}


class SourcesHandler(BaseHTTPRequestHandler):
    root: Path

    def log_message(self, _format: str, *_args) -> None:
        # Avoid copying article URLs or future owner data into server access logs.
        return

    def _send(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: int, value: dict) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if self.client_address[0] not in {"127.0.0.1", "::1"} or host not in {"127.0.0.1", "localhost"}:
            self._json(403, {"error": "local access only"})
            return
        request = urlsplit(self.path)
        if request.path == "/":
            self.send_response(302)
            self.send_header("Location", "/sources")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if request.path in ASSETS:
            filename, content_type = ASSETS[request.path]
            self._send(200, (Path(__file__).parent / filename).read_bytes(), content_type)
            return
        if request.path == "/api/sources/dates":
            self._json(200, {"dates": available_dates(self.root)})
            return
        if request.path == "/api/sources":
            query = parse_qs(request.query)
            try:
                dates = available_dates(self.root)
                day = validate_date(query.get("date", [dates[0] if dates else ""])[0])
                run_id = query.get("run_id", [None])[0]
                self._json(200, sources_for_date(self.root, day, run_id))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            return
        self._json(404, {"error": "route unavailable"})


def serve(root: Path, port: int = 8765) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("port is outside 1..65535")

    class BoundHandler(SourcesHandler):
        pass

    BoundHandler.root = root.resolve()
    with ThreadingHTTPServer(("127.0.0.1", port), BoundHandler) as server:
        print(f"MarieSpace Sources: http://127.0.0.1:{port}/sources", flush=True)
        server.serve_forever()
