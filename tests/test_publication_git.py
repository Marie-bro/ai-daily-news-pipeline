from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import subprocess
import unittest

from ai_daily_pipeline.publication_git import PublicationGit, PublicationGitError, deploy, redact, error_class
from ai_daily_pipeline.delivery import run_scheduled_delivery
from ai_daily_pipeline.exit_status import delivery_exit_outcome


class DiagnosticsTests(unittest.TestCase):
    def test_error_classes(self):
        for message, expected in [("Could not resolve host", "dns_network"), ("SSL certificate failure", "tls"),
                                  ("fatal: Unsupported SSL backend 'openssl'", "tls_backend_unavailable"),
                                  ("Authentication failed", "authentication"), ("non-fast-forward", "non_fast_forward"),
                                  ("remote rejected", "remote_rejected"), ("Repository not found", "repository_unavailable"),
                                  ("timed out", "timeout"), ("unexpected", "unknown")]:
            with self.subTest(message=message): self.assertEqual(error_class(message), expected)

    def test_credentials_redacted_from_all_streams(self):
        raw = "https://alice:password123@github.com/repo?token=abc123 authorization: Bearer topsecret"
        safe = redact(raw)
        for secret in ["alice", "password123", "abc123", "topsecret"]: self.assertNotIn(secret, safe)
        self.assertNotIn("ghp_abcd1234", redact("ghp_abcd1234"))
        with patch.dict("os.environ", {"PRIVATE_TOKEN": "secret-from-env"}):
            self.assertNotIn("secret-from-env", redact("fatal secret-from-env"))

    def test_push_receipt_and_durable_redacted_log(self):
        with TemporaryDirectory() as directory:
            log = Path(directory) / "git.jsonl"
            git = PublicationGit(Path(directory), diagnostic_path=log)
            git.context.update(local_head="a" * 40, upstream_head="b" * 40)
            with patch("ai_daily_pipeline.publication_git.subprocess.run", return_value=SimpleNamespace(returncode=128, stdout="", stderr="Authentication failed for https://user:secret@host/repo")):
                with self.assertRaises(PublicationGitError) as raised: git.run(["push", "origin", "main"])
            receipt = raised.exception.diagnostics
            self.assertEqual(receipt["publish_push_error_class"], "authentication")
            self.assertEqual(receipt["publish_push_exit_code"], 128)
            record = json.loads(log.read_text())
            for field in ["command", "cwd", "remote", "branch", "local_head", "upstream_head", "exit_code", "stdout", "stderr", "start_time", "end_time", "elapsed_ms", "environment_proxy_mode"]:
                self.assertIn(field, record)
            self.assertNotIn("user:secret", log.read_text())

    def test_proxy_config_logs_presence_only(self):
        git = PublicationGit(Path("."))
        with patch("ai_daily_pipeline.publication_git.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="http://private-proxy:7890", stderr="")):
            git.run(["config", "--get", "http.proxy"], presence_only=True)
        self.assertEqual(git.records[-1]["stdout"], "present")
        self.assertNotIn("private-proxy", json.dumps(git.records))

    def test_timeout_is_bounded_and_classified(self):
        git = PublicationGit(Path("."))
        with patch("ai_daily_pipeline.publication_git.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 120, stderr=b"partial")) as call:
            with self.assertRaises(PublicationGitError) as raised: git.run(["push", "origin", "main"])
        self.assertEqual(raised.exception.diagnostics["publish_push_error_class"], "timeout")
        self.assertIsNone(raised.exception.diagnostics["publish_push_exit_code"])
        self.assertEqual(call.call_args.kwargs["timeout"], 120)
        self.assertEqual(call.call_count, 1)

    def test_scheduled_failure_records_diagnostics_and_never_sends(self):
        from ai_daily_pipeline.delivery import DeliveryError
        failure = DeliveryError("site data deployment failed")
        failure.diagnostics = {"publish_git_commit_sha": "a" * 40, "publish_push_status": "failed", "publish_push_exit_code": 128,
                               "publish_push_error_class": "tls", "publish_push_stderr_safe": "SSL EOF"}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("ai_daily_pipeline.delivery.run_collection", return_value=SimpleNamespace(accepted=8)), \
                 patch("ai_daily_pipeline.delivery.run_enrichment", return_value=SimpleNamespace(saved=8, candidates=14, output_path=root / "enrichment.json")), \
                 patch("ai_daily_pipeline.delivery.publish_latest_report", return_value=root / "site/data/daily/ai/2026-10-07.json"), \
                 patch("ai_daily_pipeline.delivery.deploy_site_data", side_effect=failure), \
                 patch("ai_daily_pipeline.delivery.send_existing_report") as send:
                result = run_scheduled_delivery(root, root / "site")
            self.assertEqual(int(delivery_exit_outcome(result).code), 5)
            send.assert_not_called()
            audit = json.loads(next((root / "data/run-audits").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(audit["metrics"]["publish_push_exit_code"], 128)
            record = json.loads((root / "data/delivery-runs.jsonl").read_text().splitlines()[-1])
            self.assertEqual(record["publish_push_error_class"], "tls")


class LocalRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        self.site.mkdir()
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("remote", "add", "origin", str(self.remote))
        (self.site / "seed").write_text("seed")
        self.git("add", "seed")
        self.git("commit", "-m", "seed")
        self.git("push", "-u", "origin", "main")
        self.report = self.site / "data/daily/ai/2026-10-07.json"
        self.report.parent.mkdir(parents=True)
        self.report.write_text('{"items": []}')
        (self.site / "data/reports.json").write_text("[]")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.site, check=True, capture_output=True, text=True).stdout.strip()

    def test_new_commit_success_is_not_deployment_completion(self):
        receipt = deploy(self.site, self.report)
        self.assertEqual(receipt["publish_push_status"], "pushed")
        self.assertEqual(receipt["publish_push_exit_code"], 0)
        self.assertIsNotNone(receipt["deploy_push_finished_time"])
        self.assertEqual(receipt["deployment_status"], "unknown")
        self.assertEqual(receipt["publish_tls_backend"], "openssl")
        self.assertEqual(receipt["publish_local_head_after_push"], self.git("rev-parse", "HEAD"))
        self.assertEqual(receipt["publish_upstream_head_after_push"], self.git("rev-parse", "origin/main"))
        push = next(item for item in receipt["publish_git_commands"] if "push" in item["command"])
        self.assertEqual(push["command"], ["git", "-c", "http.sslBackend=openssl", "-c", "http.sslVerify=true",
                                           "push", "origin", "main"])
        self.assertEqual(self.git("config", "--local", "--get", "user.name"), "Test")
        backend = subprocess.run(["git", "config", "--local", "--get", "http.sslBackend"],
                                 cwd=self.site, capture_output=True, text=True)
        self.assertEqual(backend.returncode, 1)  # Command-level override did not persist.
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "origin/main"))

    def test_existing_git_proxy_is_reused_without_logging_its_address(self):
        self.git("config", "http.proxy", "http://name:secret@127.0.0.1:7897")
        receipt = deploy(self.site, self.report)
        self.assertEqual(receipt["publish_proxy_mode"], "existing_git_config")
        self.assertNotIn("name:secret", json.dumps(receipt))
        self.assertNotIn("7897", json.dumps(receipt))

    def test_proxy_address_in_git_error_is_masked(self):
        self.git("config", "http.proxy", "http://name:secret@127.0.0.1:7897")
        git = PublicationGit(self.site, push_tls_backend="openssl")
        git.inspect()
        with patch("ai_daily_pipeline.publication_git.subprocess.run",
                   return_value=SimpleNamespace(returncode=128, stdout="",
                                                stderr="Failed to connect to 127.0.0.1 port 7897")):
            with self.assertRaises(PublicationGitError) as raised:
                git.run(["push", "origin", "main"])
        self.assertIn("[PROXY]", raised.exception.diagnostics["publish_push_stderr_safe"])
        self.assertNotIn("7897", json.dumps(raised.exception.diagnostics))

    def test_missing_openssl_fails_explicitly_without_schannel_fallback(self):
        real_run = subprocess.run
        pushes = []
        def unsupported(command, **kwargs):
            if "push" in command:
                pushes.append(command)
                return SimpleNamespace(returncode=128, stdout="", stderr="fatal: Unsupported SSL backend 'openssl'")
            return real_run(command, **kwargs)
        with patch("ai_daily_pipeline.publication_git.subprocess.run", side_effect=unsupported):
            with self.assertRaises(PublicationGitError) as raised:
                deploy(self.site, self.report)
        receipt = raised.exception.diagnostics
        self.assertEqual(receipt["publish_push_status"], "failed")
        self.assertEqual(receipt["publish_push_error_class"], "tls_backend_unavailable")
        self.assertEqual(len(pushes), 1)
        self.assertIn("http.sslBackend=openssl", pushes[0])
        self.assertFalse(any("schannel" in str(arg).lower() for arg in pushes[0]))

    def test_push_ignores_no_verify_environment_only_for_this_command(self):
        git = PublicationGit(self.site, push_tls_backend="openssl")
        with patch.dict("os.environ", {"GIT_SSL_NO_VERIFY": "1"}), \
             patch("ai_daily_pipeline.publication_git.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            git.run(["push", "origin", "main"])
        self.assertNotIn("GIT_SSL_NO_VERIFY", run.call_args.kwargs["env"])
        self.assertIn("http.sslVerify=true", run.call_args.args[0])

    def test_failed_push_then_same_data_pushes_existing_commit(self):
        real_run = subprocess.run
        def fail_push(command, **kwargs):
            if "push" in command: return SimpleNamespace(returncode=128, stdout="", stderr="SSL EOF")
            return real_run(command, **kwargs)
        with patch("ai_daily_pipeline.publication_git.subprocess.run", side_effect=fail_push):
            with self.assertRaises(PublicationGitError): deploy(self.site, self.report)
        head = self.git("rev-parse", "HEAD")
        count = self.git("rev-list", "--count", "HEAD")
        receipt = deploy(self.site, self.report)
        self.assertEqual(receipt["publish_push_status"], "pushed")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), count)
        self.assertEqual(self.git("rev-parse", "origin/main"), head)
        again = deploy(self.site, self.report)
        self.assertEqual(again["publish_push_status"], "not_attempted_no_changes")
        self.assertIsNone(again["deploy_push_finished_time"])

    def test_unrelated_staged_work_is_not_committed(self):
        (self.site / "private").write_text("unrelated")
        self.git("add", "private")
        head = self.git("rev-parse", "HEAD")
        with self.assertRaises(PublicationGitError): deploy(self.site, self.report)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_expected_head_guards_manual_verification(self):
        with self.assertRaises(PublicationGitError): PublicationGit(self.site).push_existing("wrong")


if __name__ == "__main__": unittest.main()
