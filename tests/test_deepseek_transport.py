import io
import json
import socket
import ssl
import unittest
from urllib.request import Request
from unittest.mock import patch

from ai_daily_pipeline.deepseek import DeepSeekClient, DeepSeekError
from ai_daily_pipeline.deepseek_transport import open_request, proxy_settings


class Response:
    status = 200

    def __init__(self):
        self.body = io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}).encode())

    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, size=-1): return self.body.read(size)


class DeepSeekTransportTests(unittest.TestCase):
    def run_outcomes(self, outcomes):
        calls = []
        def transport(request, *, trace, proxies, connect_timeout, read_timeout):
            self.assertEqual(proxies, {})
            calls.append((connect_timeout, read_timeout))
            phase, outcome = outcomes[len(calls) - 1]
            trace["failure_phase"] = phase
            if phase == "response_body_read":
                trace.update(response_headers_received=True, http_status=200)
            if isinstance(outcome, Exception): raise outcome
            return outcome
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-secret", "DEEPSEEK_MODEL": "test-model"}, clear=True), \
             patch("ai_daily_pipeline.deepseek._read_existing_local_settings", return_value={}), \
             patch("ai_daily_pipeline.deepseek.open_request", side_effect=transport), \
             patch("ai_daily_pipeline.deepseek.time.sleep"):
            client = DeepSeekClient()
            client.request_context = {"batch_id": "main-1", "article_ids": ["a"]}
            try:
                result = client.complete_json(system_prompt="Return JSON", user_prompt="Short request", max_tokens=20)
                error = None
            except DeepSeekError as exc:
                result, error = None, exc
        return client, result, error, calls

    def test_tls_eof_connect_and_read_timeout_and_reset_retry_with_phase(self):
        for phase, error in [("tls_handshake", ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING")),
                             ("tcp_connect", TimeoutError("connect timeout")),
                             ("response_body_read", TimeoutError("read timeout")),
                             ("dns_resolve", socket.gaierror(socket.EAI_AGAIN, "temporary")),
                             ("request_body_upload", ConnectionResetError("reset"))]:
            with self.subTest(phase=phase):
                client, result, failure, calls = self.run_outcomes([(phase, error), ("response_headers", Response())])
                self.assertIsNone(failure)
                self.assertEqual(result[1]["total_tokens"], 10)
                self.assertEqual(calls, [(15.0, 90.0)] * 2)
                self.assertEqual(client.last_attempt_history[0]["failure_phase"], phase)
                self.assertEqual(client.last_attempt_history[0]["response_headers_received"], phase == "response_body_read")
                self.assertFalse(client.last_attempt_history[0]["usage_recorded"])
                client.mark_usage_recorded()
                self.assertEqual(sum(x["usage_recorded"] for x in client.last_attempt_history), 1)
                self.assertTrue(client.last_attempt_history[-1]["response_body_started"])

    def test_attempts_share_request_id_and_record_exact_payload_without_secrets(self):
        client, _, _, _ = self.run_outcomes([("tls_handshake", ssl.SSLEOFError(8, "test-secret")), ("response_headers", Response())])
        history = client.last_attempt_history
        self.assertEqual(history[0]["request_id"], history[1]["request_id"])
        self.assertEqual(history[0]["payload_bytes"], history[1]["payload_bytes"])
        self.assertGreater(history[0]["payload_bytes"], 0)
        self.assertIn("start_time", history[0])
        self.assertNotIn("test-secret", json.dumps(history))

    def test_partial_response_is_retried_on_a_fresh_transport(self):
        class BrokenResponse(Response):
            def read(self, size=-1):
                if size == 1: return b"{"
                raise ConnectionResetError("read reset")
        client, result, failure, calls = self.run_outcomes([("response_headers", BrokenResponse()), ("response_headers", Response())])
        self.assertIsNone(failure)
        self.assertEqual(len(calls), 2)
        self.assertEqual(client.last_attempt_history[0]["failure_phase"], "response_body_read")
        self.assertTrue(client.last_attempt_history[0]["response_body_started"])
        self.assertFalse(client.last_attempt_history[0]["complete_response_received"])

    def test_exhaustion_retains_all_phase_diagnostics(self):
        client, result, failure, calls = self.run_outcomes([("tls_handshake", ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))] * 3)
        self.assertIsNone(result)
        self.assertTrue(failure.model_request_failed)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(x["failure_phase"] == "tls_handshake" for x in failure.retry_history))
        self.assertTrue(all(not x["usage_recorded"] for x in failure.retry_history))

    def test_proxy_routing_is_explicit_and_scoped(self):
        with patch("ai_daily_pipeline.deepseek_transport.getproxies", return_value={"https": "http://127.0.0.1:7897"}) as detected:
            self.assertEqual(proxy_settings("direct"), {})
            detected.assert_not_called()
            self.assertEqual(proxy_settings("system"), {"https": "http://127.0.0.1:7897"})
        self.assertEqual(proxy_settings("explicit", "http://127.0.0.1:7897"),
                         {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
        for mode, url in [("unknown", ""), ("explicit", ""), ("explicit", "socks://localhost:1080")]:
            with self.assertRaises(ValueError): proxy_settings(mode, url)

    def test_transport_uses_distinct_timeouts_verified_tls_and_closes_connection(self):
        body = b'{"ok":true}'
        raw = b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
        with patch("ai_daily_pipeline.deepseek_transport.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]), \
             patch("ai_daily_pipeline.deepseek_transport.socket.socket") as socket_factory, \
             patch("ai_daily_pipeline.deepseek_transport.ssl.create_default_context") as tls_factory:
            sock = socket_factory.return_value
            sock.makefile.return_value = io.BytesIO(raw)
            tls_factory.return_value.wrap_socket.return_value = sock
            trace = {}
            with open_request(Request("https://api.deepseek.com/chat/completions", data=b"{}"),
                              trace=trace, proxies={}, connect_timeout=15, read_timeout=90) as response:
                self.assertEqual(response.read(), body)
            self.assertEqual([c.args[0] for c in sock.settimeout.call_args_list], [15, 90])
            tls_factory.return_value.wrap_socket.assert_called_once_with(sock, server_hostname="api.deepseek.com")
            self.assertTrue(trace["response_headers_received"])
            sock.close.assert_called()

    def test_transport_records_dns_failure_and_handshake_failure(self):
        for phase in ["dns_resolve", "tls_handshake"]:
            with self.subTest(phase=phase), \
                 patch("ai_daily_pipeline.deepseek_transport.socket.getaddrinfo") as dns, \
                 patch("ai_daily_pipeline.deepseek_transport.socket.socket") as sockets, \
                 patch("ai_daily_pipeline.deepseek_transport.ssl.create_default_context") as tls:
                if phase == "dns_resolve": dns.side_effect = socket.gaierror(socket.EAI_AGAIN, "temporary")
                else:
                    dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
                    tls.return_value.wrap_socket.side_effect = ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING")
                trace = {}
                with self.assertRaises(OSError):
                    open_request(Request("https://api.deepseek.com/chat/completions", data=b"{}"),
                                 trace=trace, proxies={}, connect_timeout=15, read_timeout=90)
                self.assertEqual(trace["failure_phase"], phase)

    def test_transport_identifies_tcp_upload_and_response_header_failures(self):
        for phase in ["tcp_connect", "request_headers", "request_body_upload", "response_headers"]:
            with self.subTest(phase=phase), \
                 patch("ai_daily_pipeline.deepseek_transport.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]), \
                 patch("ai_daily_pipeline.deepseek_transport.socket.socket") as sockets, \
                 patch("ai_daily_pipeline.deepseek_transport.ssl.create_default_context") as tls:
                sock = sockets.return_value
                tls.return_value.wrap_socket.return_value = sock
                if phase == "tcp_connect": sock.connect.side_effect = TimeoutError("connect timeout")
                elif phase == "request_headers": sock.sendall.side_effect = ConnectionResetError("headers reset")
                elif phase == "request_body_upload": sock.sendall.side_effect = [None, ConnectionResetError("upload reset")]
                else: sock.makefile.return_value.readline.side_effect = TimeoutError("response headers timeout")
                trace = {}
                with self.assertRaises(OSError):
                    open_request(Request("https://api.deepseek.com/chat/completions", data=b"{}"),
                                 trace=trace, proxies={}, connect_timeout=15, read_timeout=90)
                self.assertEqual(trace["failure_phase"], phase)
                self.assertFalse(trace.get("response_headers_received", False))

    def test_certificate_verification_failure_is_never_retried(self):
        client, result, error, calls = self.run_outcomes([("tls_handshake", ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))])
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertFalse(client.last_attempt_history[0]["retryable"])

    def test_payload_metrics_measure_submitted_clean_text_without_changing_payload(self):
        captured = {}
        def opener(request, timeout):
            captured.update(json.loads(request.data))
            return Response()
        prompt = 'Verified candidates (process every one):\n' + json.dumps({"candidates": [{"id": "a", "clean_text": "测试abc"}]}, ensure_ascii=False)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-secret", "DEEPSEEK_MODEL": "test-model"}, clear=True), \
             patch("ai_daily_pipeline.deepseek._read_existing_local_settings", return_value={}):
            client = DeepSeekClient(opener=opener)
            client.complete_json(system_prompt="Original system", user_prompt=prompt, max_tokens=123)
        self.assertEqual(captured["messages"], [{"role": "system", "content": "Original system"}, {"role": "user", "content": prompt}])
        self.assertEqual(captured["max_tokens"], 123)
        self.assertEqual(client.last_attempt_history[0]["clean_text_chars"], 5)
        self.assertEqual(client.last_attempt_history[0]["article_count"], 1)


if __name__ == "__main__":
    unittest.main()
