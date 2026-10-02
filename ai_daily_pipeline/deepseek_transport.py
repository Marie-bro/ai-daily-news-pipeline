"""Small urllib transport with explicit routing and credential-free phase telemetry."""
from __future__ import annotations

import http.client
import socket
import ssl
from urllib.request import HTTPSHandler, ProxyHandler, build_opener, getproxies
from urllib.parse import urlsplit


def proxy_settings(mode: str, proxy_url: str = "") -> dict[str, str]:
    if mode == "direct":
        return {}
    if mode == "system":
        return getproxies()
    if mode != "explicit" or not proxy_url:
        raise ValueError("DEEPSEEK_PROXY_MODE must be direct, system, or explicit with DEEPSEEK_PROXY_URL")
    parts = urlsplit(proxy_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("DEEPSEEK_PROXY_URL must be an HTTP(S) proxy URL")
    return {"http": proxy_url, "https": proxy_url}


def open_request(request, *, trace: dict[str, object], proxies: dict[str, str],
                 connect_timeout: float, read_timeout: float):
    """Build a fresh connection for each attempt; never change global proxy state."""
    class Connection(http.client.HTTPSConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._headers_sent = False
            self._create_connection = self._connect_socket

        def _connect_socket(self, address, timeout, source_address=None):
            trace["failure_phase"] = "dns_resolve"
            addresses = socket.getaddrinfo(*address, 0, socket.SOCK_STREAM)
            last_error = None
            for family, kind, protocol, _, endpoint in addresses:
                sock = None
                trace["failure_phase"] = "tcp_connect"
                try:
                    sock = socket.socket(family, kind, protocol)
                    sock.settimeout(timeout)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(endpoint)
                    return sock
                except OSError as exc:
                    last_error = exc
                    if sock is not None:
                        sock.close()
            if last_error is not None:
                raise last_error
            raise OSError("DNS returned no connectable address")

        def _tunnel(self):
            trace["failure_phase"] = "proxy_connect"
            try:
                return super()._tunnel()
            except Exception:
                trace["failure_phase"] = "proxy_connect"
                raise
            finally:
                self._headers_sent = False

        def connect(self):
            http.client.HTTPConnection.connect(self)
            trace["failure_phase"] = "tls_handshake"
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host)
            self.sock.settimeout(read_timeout)

        def send(self, data):
            if self.sock is None:
                self.connect()
            trace["failure_phase"] = "request_body_upload" if self._headers_sent else "request_headers"
            result = super().send(data)
            self._headers_sent = True
            return result

        def getresponse(self):
            trace["failure_phase"] = "response_headers"
            response = super().getresponse()
            trace["response_headers_received"] = True
            trace["http_status"] = response.status
            return response

    class Handler(HTTPSHandler):
        def https_open(self, req):
            return self.do_open(Connection, req, context=self._context)

    # Certificate and hostname verification remain enabled. No shared connection pool.
    opener = build_opener(ProxyHandler(proxies), Handler(context=ssl.create_default_context()))
    return opener.open(request, timeout=connect_timeout)


def read_response(response, trace: dict[str, object], *, measured: bool) -> bytes:
    trace["response_headers_received"] = True
    trace["http_status"] = getattr(response, "status", 200)
    trace["failure_phase"] = "response_body_read"
    if measured:
        first = response.read(1)
        trace["response_body_started"] = bool(first)
        # read() retains Content-Length truncation detection (IncompleteRead).
        return first + response.read()
    body = response.read()  # Preserve the injected opener interface used by existing tests.
    trace["response_body_started"] = bool(body)
    return body
