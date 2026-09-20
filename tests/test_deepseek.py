import json
import unittest
from unittest.mock import patch

from ai_daily_pipeline.deepseek import DeepSeekClient, DeepSeekError


class DeepSeekRequestTests(unittest.TestCase):
    def test_request_uses_current_model_and_json_mode(self):
        captured = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self):
                return json.dumps({"choices": [{"message": {"content": '{"items":[]}'}}],
                                   "usage": {"total_tokens": 3}, "model": "configured-test-model"}).encode()

        def opener(request, timeout):
            captured.update(json.loads(request.data.decode("utf-8")))
            return Response()

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MODEL": "configured-test-model"}, clear=True), \
             patch("ai_daily_pipeline.deepseek._read_existing_local_settings", return_value={}):
            content, usage, model = DeepSeekClient(opener=opener).complete_json(
                system_prompt="Return JSON", user_prompt="One verified item", max_tokens=300
            )
        self.assertEqual(captured["model"], "configured-test-model")
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertEqual((content, usage["total_tokens"], model), ('{"items":[]}', 3, "configured-test-model"))

    def test_model_must_be_configured(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}, clear=True), \
             patch("ai_daily_pipeline.deepseek._read_existing_local_settings", return_value={}):
            with self.assertRaisesRegex(DeepSeekError, "DEEPSEEK_MODEL is missing"):
                DeepSeekClient()

    def test_truncated_response_preserves_usage_on_error(self):
        usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
                 "prompt_tokens_details": {"cached_tokens": 4}}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self):
                return json.dumps({"choices": [{"finish_reason": "length", "message": {"content": '{}'}}],
                                   "usage": usage, "model": "configured-test-model"}).encode()

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MODEL": "configured-test-model"}, clear=True), \
             patch("ai_daily_pipeline.deepseek._read_existing_local_settings", return_value={}):
            with self.assertRaisesRegex(DeepSeekError, "not fully generated") as caught:
                DeepSeekClient(opener=lambda *_args, **_kwargs: Response()).complete_json(
                    system_prompt="Return JSON", user_prompt="One verified item", max_tokens=300
                )
        self.assertEqual(caught.exception.usage, usage)
        self.assertEqual(caught.exception.model, "configured-test-model")
