import json
import socket
import ssl
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from ai_daily_pipeline.deepseek import DeepSeekClient, DeepSeekError


class Response:
    def __init__(self, body=None):
        self.body = body or {"choices": [{"message": {"content": '{"items":[]}'}}],
                             "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}

    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def read(self): return json.dumps(self.body).encode("utf-8")


def http_error(status):
    return HTTPError("https://api.deepseek.com/chat/completions", status, "failure", {}, None)


class RetryTests(unittest.TestCase):
    def run_sequence(self, outcomes):
        calls = []
        delays = []
        def opener(_request, timeout):
            calls.append(timeout)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MODEL": "test-model"}, clear=True), \
             patch("ai_daily_pipeline.deepseek._read_existing_local_settings", return_value={}), \
             patch("ai_daily_pipeline.deepseek.time.sleep", side_effect=delays.append):
            client = DeepSeekClient(opener=opener)
            client.request_context = {"batch_id": "main-2", "article_ids": ["a", "b"]}
            try:
                result = client.complete_json(system_prompt="system", user_prompt="user", max_tokens=100)
                error = None
            except DeepSeekError as exc:
                result, error = None, exc
        return client, result, error, calls, delays

    def test_tls_eof_then_success(self):
        client, result, error, calls, delays = self.run_sequence([
            URLError(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING")), Response()])
        self.assertIsNone(error)
        self.assertEqual(result[1]["total_tokens"], 10)
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [2.0])
        self.assertEqual(client.last_attempt_history[0]["batch_id"], "main-2")
        self.assertEqual(client.last_attempt_history[0]["article_ids"], ["a", "b"])
        self.assertFalse(client.last_attempt_history[0]["complete_response_received"])

    def test_two_timeouts_then_success(self):
        client, result, error, calls, delays = self.run_sequence([
            URLError(TimeoutError("connect timeout")), TimeoutError("read timeout"), Response()])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [2.0, 5.0])
        self.assertEqual([item["attempt"] for item in client.last_attempt_history], [1, 2, 3])

    def test_http_429_and_500_retry(self):
        for status in (429, 500):
            with self.subTest(status=status):
                client, result, error, calls, delays = self.run_sequence([http_error(status), Response()])
                self.assertIsNone(error)
                self.assertEqual(len(calls), 2)
                self.assertEqual(client.last_attempt_history[0]["http_status"], status)
                self.assertEqual(delays, [2.0])

    def test_exhausted_retry_has_history_and_failure_marker(self):
        client, result, error, calls, delays = self.run_sequence([
            URLError(ConnectionResetError("reset")), URLError(TimeoutError("timeout")),
            URLError(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))])
        self.assertIsNone(result)
        self.assertTrue(error.model_request_failed)
        self.assertEqual(len(error.retry_history), 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [2.0, 5.0])
        self.assertTrue(all(not item["usage_recorded"] for item in error.retry_history))

    def test_http_400_and_auth_errors_do_not_retry(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                client, result, error, calls, delays = self.run_sequence([http_error(status), Response()])
                self.assertIsNone(result)
                self.assertEqual(len(calls), 1)
                self.assertEqual(delays, [])
                self.assertFalse(error.model_request_failed)

    def test_temporary_dns_retries_but_permanent_dns_does_not(self):
        _, result, error, calls, _ = self.run_sequence([URLError(socket.gaierror(socket.EAI_AGAIN, "temporary")), Response()])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        _, result, error, calls, delays = self.run_sequence([URLError(socket.gaierror(socket.EAI_NONAME, "permanent")), Response()])
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])

    def test_invalid_response_does_not_network_retry(self):
        class BadResponse(Response):
            def read(self): return b"not json"
        _, result, error, calls, delays = self.run_sequence([BadResponse(), Response()])
        self.assertIsNone(result)
        self.assertIsInstance(error, DeepSeekError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])

    def test_schema_validation_is_outside_network_retry(self):
        _, result, error, calls, delays = self.run_sequence([Response({"unexpected": "schema"}), Response()])
        self.assertIsNone(result)
        self.assertIsInstance(error, DeepSeekError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])


if __name__ == "__main__":
    unittest.main()
