import contextlib
import base64
import io
import json
import os
import runpy
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import run_daily_delivery
from ai_daily_pipeline.delivery import DeliveryResult, DeliveryError, run_scheduled_delivery
from ai_daily_pipeline.diagnostics import mark_failure
from ai_daily_pipeline.exit_status import ExitCode, delivery_exit_outcome
from ai_daily_pipeline.run_audit import RunAudit
from test_delivery import NOW, DATE, ITEM, public_opener, send_existing_report

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "run_daily_delivery.py"
RUNNER = ROOT / "deploy/run-phase6-task.cmd"


def result(status="sent", reason=None):
    return DeliveryResult(DATE, "report", status, reason, "message" if status == "sent" else None, "https://news.mariespace.cn/", 0)


class ExitStatusTests(unittest.TestCase):
    def test_failure_mapping_and_cli_use_the_same_codes(self):
        cases = [("collection_failed", ExitCode.COLLECTION_FAILURE),
                 ("normalization_failed", ExitCode.COLLECTION_FAILURE),
                 ("model_request_failed", ExitCode.ENRICHMENT_FAILURE),
                 ("enrichment_failed", ExitCode.ENRICHMENT_FAILURE),
                 ("token_budget_exhausted", ExitCode.TOKEN_BUDGET_FAILURE),
                 ("publication_or_deployment_failed", ExitCode.PUBLISH_FAILURE),
                 ("publish_verification_failed", ExitCode.READINESS_FAILURE),
                 ("feishu_send_failed_or_uncertain", ExitCode.FEISHU_FAILURE),
                 ("validation_failed", ExitCode.VALIDATION_FAILURE),
                 ("persistence_failed", ExitCode.PERSISTENCE_FAILURE)]
        for reason, code in cases:
            with self.subTest(reason=reason), patch("run_daily_delivery.run_scheduled_delivery", return_value=result("skipped", reason)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_daily_delivery.main(["--scheduled"]), code)
                self.assertEqual(delivery_exit_outcome(result("skipped", reason)).final_status, "daily_failed")

    def test_explicit_failure_cannot_be_masked_by_success_status(self):
        self.assertNotEqual(delivery_exit_outcome(result("sent", "token_budget_exhausted")).code, 0)

    def test_unknown_skip_and_empty_daily_are_not_success(self):
        for status, reason in [("skipped", "unknown"), ("daily_failed", None), ("skipped", "no_qualified_tech_news")]:
            self.assertNotEqual(delivery_exit_outcome(result(status, reason)).code, 0)

    def test_explicit_normal_skips_are_success(self):
        for status, reason in [("skipped", "already_sent_or_pending"), ("dry_run", "dry_run"),
                               ("dry_run", "feishu_send_dry_run"), ("verified", "verified_without_send")]:
            outcome = delivery_exit_outcome(result(status, reason))
            self.assertEqual(outcome.code, 0)
            self.assertEqual(outcome.final_status, "normal_skip")

    def test_normal_degraded_and_minimal_success_and_idempotency_in_real_delivery(self):
        for mode in ["normal", "graceful_degraded", "minimal_daily"]:
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "site/data/daily/ai" / (DATE + ".json")
                path.parent.mkdir(parents=True)
                count = {"normal": 14, "graceful_degraded": 7, "minimal_daily": 1}[mode]
                items = [dict(ITEM, original_url=f"https://example.com/news/{index}") for index in range(count)]
                path.write_text(json.dumps({"schema_version": 3, "report_date": DATE, "article_count": count,
                                            "estimated_reading_minutes": 1, "daily_mode": mode, "items": items}), encoding="utf-8")
                sends = []
                def sender(*_):
                    sends.append("sent")
                    return "message", 0
                kwargs = dict(now=NOW, sender=sender, target=("open_id", "test-owner"), opener=public_opener)
                first = send_existing_report(root, root / "site", DATE, **kwargs)
                second = send_existing_report(root, root / "site", DATE, **kwargs)
                self.assertEqual(delivery_exit_outcome(first).code, 0)
                self.assertEqual(delivery_exit_outcome(second).code, 0)
                self.assertEqual(second.reason, "already_sent_or_pending")
                self.assertEqual(sends, ["sent"])
                logs = [json.loads(line) for line in (root / "data/delivery-runs.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual([row["final_status"] for row in logs], ["daily_success", "normal_skip"])
                self.assertEqual([row["exit_code"] for row in logs], [0, 0])

    def test_scheduled_audit_is_consistent_with_returned_result(self):
        for status, reason in [("sent", None), ("skipped", "already_sent_or_pending"),
                               ("skipped", "model_request_failed"), ("daily_failed", "token_budget_exhausted"),
                               ("skipped", "publish_verification_failed"), ("uncertain", "feishu_send_failed_or_uncertain")]:
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                root = Path(directory)
                with patch("ai_daily_pipeline.delivery._run_scheduled_delivery", return_value=result(status, reason)):
                    returned = run_scheduled_delivery(root, root / "site", now=NOW)
                audit = json.loads(next((root / "data/run-audits").glob("*.json")).read_text(encoding="utf-8"))
                outcome = delivery_exit_outcome(returned)
                self.assertEqual(audit["final_status"], outcome.final_status)
                self.assertEqual(audit["exit_code"], outcome.code)
                self.assertEqual(audit["exit_reason"], outcome.reason)
                self.assertEqual(audit["metrics"]["delivery_status"], status)

    def test_actual_caught_collection_failure_reaches_audit_and_exit_mapping(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("ai_daily_pipeline.delivery.run_collection", side_effect=RuntimeError("private details")), \
                 patch("ai_daily_pipeline.delivery.run_enrichment") as enrich:
                returned = run_scheduled_delivery(root, root / "site", now=NOW)
            enrich.assert_not_called()
            audit = json.loads(next((root / "data/run-audits").glob("*.json")).read_text(encoding="utf-8"))
            log = json.loads((root / "data/delivery-runs.jsonl").read_text(encoding="utf-8"))
            for row in [audit, log]:
                self.assertEqual(row["final_status"], "daily_failed")
                self.assertEqual(row["exit_code"], ExitCode.COLLECTION_FAILURE)
            self.assertEqual(delivery_exit_outcome(returned).code, ExitCode.COLLECTION_FAILURE)

    def test_unhandled_exception_still_writes_failed_audit_and_cli_nonzero(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            error = mark_failure(RuntimeError("secret=must-not-print"), "persistence")
            with patch("ai_daily_pipeline.delivery._run_scheduled_delivery", side_effect=error), \
                 patch("run_daily_delivery.run_scheduled_delivery", side_effect=lambda *_: run_scheduled_delivery(root, root / "site", now=NOW)), \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(run_daily_delivery.main(["--scheduled"]), ExitCode.PERSISTENCE_FAILURE)
            self.assertNotIn("must-not-print", stderr.getvalue())
            audit = json.loads(next((root / "data/run-audits").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(audit["exit_code"], ExitCode.PERSISTENCE_FAILURE)
            self.assertEqual(audit["final_status"], "daily_failed")

    def test_inconsistent_audit_metadata_is_rejected(self):
        with TemporaryDirectory() as directory:
            for status, code in [("daily_failed", 0), ("daily_success", 3), ("normal_skip", 7)]:
                with self.assertRaises(ValueError):
                    RunAudit(Path(directory), NOW).save(status=status, exit_code=code)
            self.assertFalse((Path(directory) / "data/run-audits").exists())

    def test_real_main_guard_calls_sys_exit(self):
        for outcome in [result(), result("skipped", "model_request_failed")]:
            with patch.object(sys, "argv", [str(SCRIPT), "--scheduled"]), \
                 patch("ai_daily_pipeline.delivery.run_scheduled_delivery", return_value=outcome), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(SCRIPT), run_name="__main__")
            self.assertEqual(stopped.exception.code, delivery_exit_outcome(outcome).code)

    def test_python_process_exit_not_only_function_return(self):
        for status, reason in [("sent", None), ("skipped", "model_request_failed"), ("uncertain", "feishu_send_failed_or_uncertain")]:
            with self.subTest(reason=reason):
                # Import the real entrypoint, replacing only its pipeline with a local result.
                bootstrap = ("import runpy,sys; from unittest.mock import patch; from types import SimpleNamespace; "
                             f"r=SimpleNamespace(status={status!r},reason={reason!r},report_date='test',url=None,message_id=None,retry_count=0); "
                             f"sys.argv=[{str(SCRIPT)!r},'--scheduled']; "
                             "p=patch('ai_daily_pipeline.delivery.run_scheduled_delivery',return_value=r);p.start(); "
                             f"runpy.run_path({str(SCRIPT)!r},run_name='__main__')")
                completed = subprocess.run([sys.executable, "-c", bootstrap], cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(completed.returncode, delivery_exit_outcome(result(status, reason)).code, completed.stderr)

    def test_invalid_arguments_have_separate_semantics(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            run_daily_delivery.main([])
        self.assertEqual(stopped.exception.code, ExitCode.INVALID_ARGUMENTS)


@unittest.skipUnless(os.name == "nt", "Windows CMD integration")
class CmdPropagationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix="radar-exit-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.exists():
            raise unittest.SkipTest("local C# compiler unavailable for isolated py.exe stub")
        source = cls.root / "Stub.cs"
        source.write_text('using System; using System.IO; class Stub { static int Main(string[] args) {'
                          'if(args.Length!=3 || args[0]!="-3" || !args[1].EndsWith("run_daily_delivery.py") || args[2]!="--scheduled") return 99;'
                          'File.WriteAllText(Environment.GetEnvironmentVariable("RADAR_TEST_MARKER"), "stub-only");'
                          'return int.Parse(Environment.GetEnvironmentVariable("RADAR_TEST_EXIT")); }}', encoding="utf-8")
        built = subprocess.run([str(compiler), "/nologo", f"/out:{cls.root / 'py.exe'}", str(source)], capture_output=True, timeout=30)
        if built.returncode:
            raise RuntimeError("isolated exit stub compilation failed")

    def test_actual_cmd_runner_propagates_success_and_failure(self):
        for code in [0, 2, 3, 4, 5, 6, 7, 8, 9]:
            with self.subTest(code=code):
                marker = self.root / f"stub-{code}.txt"
                env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ.get("PATH", ""),
                           RADAR_TEST_EXIT=str(code), RADAR_TEST_MARKER=str(marker))
                command = f'"{os.environ["COMSPEC"]}" /d /c ""{RUNNER}""'
                completed = subprocess.run(command, cwd=self.root, env=env, capture_output=True, timeout=15)
                self.assertTrue(marker.exists(), "stub not used; production must never be invoked")
                self.assertEqual(completed.returncode, code, completed.stderr)

    def test_temporary_on_demand_scheduler_reports_stub_results(self):
        # No trigger, a unique task name, isolated executable, and unconditional cleanup.
        task_name = "MarieSpace Exit Test " + uuid.uuid4().hex
        harness = self.root / "scheduler-stub.cmd"
        marker = self.root / "scheduler-marker.txt"
        def ps_quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        script = f"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
$testTaskName = {ps_quote(task_name)}
$testHarness = {ps_quote(harness)}
$testRoot = {ps_quote(self.root)}
$testMarker = {ps_quote(marker)}
$testRunner = {ps_quote(RUNNER)}
$testRegistered = $false
try {{
    $action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument ('/d /c ""' + $testHarness + '""') -WorkingDirectory $testRoot
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
    try {{
        Register-ScheduledTask -TaskName $testTaskName -Action $action -Settings $settings -Description 'Local exit stub only; no network or daily pipeline' | Out-Null
        $testRegistered = $true
    }} catch {{
        @{{available=$false; reason='temporary task registration unavailable'}} | ConvertTo-Json -Compress
        exit 0
    }}
    $results = @()
    foreach ($expected in @(0, 7)) {{
        $currentMarker = $testMarker + '.' + $expected
        $lines = @('@echo off', ('set "PATH=' + $testRoot + ';%PATH%"'), ('set "RADAR_TEST_EXIT=' + $expected + '"'),
                   ('set "RADAR_TEST_MARKER=' + $currentMarker + '"'), ('call "' + $testRunner + '"'), 'exit /b %ERRORLEVEL%')
        [System.IO.File]::WriteAllLines($testHarness, $lines, [System.Text.Encoding]::Default)
        Start-ScheduledTask -TaskName $testTaskName
        $limit = [DateTime]::UtcNow.AddSeconds(15)
        do {{
            Start-Sleep -Milliseconds 200
            $task = Get-ScheduledTask -TaskName $testTaskName
            $info = Get-ScheduledTaskInfo -TaskName $testTaskName
            $done = (Test-Path -LiteralPath $currentMarker) -and $task.State -ne 'Running'
        }} while (-not $done -and [DateTime]::UtcNow -lt $limit)
        if (-not $done) {{ throw 'isolated scheduler simulation did not finish' }}
        $results += @{{expected=$expected; result=$info.LastTaskResult}}
    }}
    @{{available=$true; results=$results}} | ConvertTo-Json -Depth 4 -Compress
}} finally {{
    if ($testRegistered) {{
        Stop-ScheduledTask -TaskName $testTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $testTaskName -Confirm:$false
    }}
}}
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=50)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        observation = json.loads(completed.stdout.strip())
        if not observation["available"]:
            self.skipTest(observation["reason"])
        self.assertEqual(observation["results"], [{"expected": 0, "result": 0}, {"expected": 7, "result": 7}])
