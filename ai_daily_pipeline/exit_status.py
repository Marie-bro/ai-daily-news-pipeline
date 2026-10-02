"""One source of truth for delivery outcomes and process exit semantics."""
from dataclasses import dataclass
from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERIC_FAILURE = 1
    COLLECTION_FAILURE = 2
    ENRICHMENT_FAILURE = 3
    TOKEN_BUDGET_FAILURE = 4
    PUBLISH_FAILURE = 5
    READINESS_FAILURE = 6
    FEISHU_FAILURE = 7
    VALIDATION_FAILURE = 8
    PERSISTENCE_FAILURE = 9
    INVALID_ARGUMENTS = 10


FAILURE_REASONS = {
    "collection_failed": ExitCode.COLLECTION_FAILURE,
    "normalization_failed": ExitCode.COLLECTION_FAILURE,
    "enrichment_failed": ExitCode.ENRICHMENT_FAILURE,
    "model_request_failed": ExitCode.ENRICHMENT_FAILURE,
    "token_budget_exhausted": ExitCode.TOKEN_BUDGET_FAILURE,
    "publication_or_deployment_failed": ExitCode.PUBLISH_FAILURE,
    "publish_failed": ExitCode.PUBLISH_FAILURE,
    "publish_verification_failed": ExitCode.READINESS_FAILURE,
    "feishu_send_failed_or_uncertain": ExitCode.FEISHU_FAILURE,
    "validation_failed": ExitCode.VALIDATION_FAILURE,
    "persistence_failed": ExitCode.PERSISTENCE_FAILURE,
    "no_qualified_tech_news": ExitCode.GENERIC_FAILURE,
}
NORMAL_SKIPS = {"already_sent_or_pending", "dry_run", "feishu_send_dry_run", "verified_without_send"}


@dataclass(frozen=True)
class ExitOutcome:
    code: ExitCode
    reason: str
    final_status: str

    def audit_fields(self):
        return {"final_status": self.final_status, "exit_code": int(self.code), "exit_reason": self.reason}


def delivery_exit_outcome(result) -> ExitOutcome:
    status, reason = result.status, result.reason
    # Explicit failure wins even when a caller incorrectly pairs it with a success status.
    if reason in FAILURE_REASONS:
        return ExitOutcome(FAILURE_REASONS[reason], reason, "daily_failed")
    if status == "sent" and reason is None:
        return ExitOutcome(ExitCode.SUCCESS, "delivery_completed", "daily_success")
    if status in {"skipped", "dry_run", "verified"} and reason in NORMAL_SKIPS:
        return ExitOutcome(ExitCode.SUCCESS, reason, "normal_skip")
    if status == "uncertain":
        return ExitOutcome(ExitCode.FEISHU_FAILURE, "feishu_send_failed_or_uncertain", "daily_failed")
    # Unknown skips/future statuses must not silently become scheduler successes.
    return ExitOutcome(ExitCode.GENERIC_FAILURE, "unclassified_delivery_failure", "daily_failed")


def exception_exit_outcome(exc: Exception) -> ExitOutcome:
    if getattr(exc, "model_request_failed", False):
        reason = "model_request_failed"
    else:
        reason = f"{getattr(exc, 'radar_failure_stage', 'unhandled')}_failed"
    return ExitOutcome(FAILURE_REASONS.get(reason, ExitCode.GENERIC_FAILURE),
                       reason if reason in FAILURE_REASONS else "unhandled_failure", "daily_failed")


def validate_exit_audit(status, code):
    """Reject inconsistent audit metadata rather than saving a false successful run."""
    if status not in {"daily_success", "normal_skip", "daily_failed"}:
        raise ValueError("invalid exit audit final status")
    if (code == ExitCode.SUCCESS) != (status in {"daily_success", "normal_skip"}):
        raise ValueError("exit code and final status are inconsistent")
