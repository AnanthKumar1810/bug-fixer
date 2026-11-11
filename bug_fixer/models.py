"""Pydantic data models for Bug Fixer."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


class RunState(str, Enum):
    ISSUE_LOADED = "issue_loaded"
    BASE_PINNED = "base_pinned"
    REPRODUCED = "reproduced"
    REPAIRING = "repairing"
    VERIFIED = "verified"
    READY_FOR_REVIEW = "ready_for_review"
    NOT_REPRODUCED = "not_reproduced"
    ENVIRONMENT_ERROR = "environment_error"
    REPAIR_FAILED = "repair_failed"
    PUBLICATION_PENDING = "publication_pending"
    PUBLISHED = "published"


class IssueInfo(BaseModel):
    url: str
    owner: str
    repo: str
    number: int
    title: str
    body: str
    default_branch: str = "main"
    base_commit: str = ""
    reproducer_test: str = ""


class TestCaseResult(BaseModel):
    nodeid: str
    file: str
    name: str
    outcome: Literal["passed", "failed", "skipped", "error"]
    duration_seconds: float = 0.0
    message: str = ""


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    duration_seconds: float = 0.0
    exit_code: int | None = None
    output: str = ""


class ValidationSummary(BaseModel):
    passed: bool
    checks: list[ValidationCheck] = Field(default_factory=list)
    test_cases: list[TestCaseResult] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    status: str = "failed"
    selected_tests: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    exit_code: int | None = None
    reproducer_outcome: str = ""
    failing_assertions: list[str] = Field(default_factory=list)
    traceback_frames: list[tuple[str, int, str]] = Field(default_factory=list)


class PromptExcerpt(BaseModel):
    path: str
    start_line: int
    end_line: int
    reason: str
    content: str
    content_hash: str
    attempt: int


class Diagnosis(BaseModel):
    suspected_cause: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    next_check_or_edit: str
    expected_observable_result: str


class EventRecord(BaseModel):
    event_id: str
    run_id: str
    attempt_number: int
    stage: str
    timestamp: str
    event_type: str
    inputs_ref: str | None = None
    outputs_ref: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ManifestEntry(BaseModel):
    rel_path: str
    sha256: str
    byte_size: int


class RunManifest(BaseModel):
    run_id: str
    base_sha: str
    provenance: str
    created_at: str
    candidate_tree_id: str | None = None
    challenge_status: str | None = None
    files: dict[str, ManifestEntry] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    issue_url: str
    issue_number: int
    issue_title: str | None = None
    repo_owner: str
    repo_name: str
    base_sha: str
    stage: RunState
    status: str
    reproducer_nodeid: str
    reproducer_hash: str
    candidate_patch_hash: str | None = None
    retry_budget: int = 2
    attempts_used: int = 0
    created_at: str
    updated_at: str
    provenance: str = "fixture"  # "fixture" or "live_github"
    origin_provenance: str = "fixture"  # immutable initial origin ("fixture", "simulated_local")
    base_branch: str = "main"
    reviewer: str | None = None
    reviewer_status: str | None = None  # "requested", "failed", "not_requested"
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    branch_protection: str | None = None  # "verified_required", "not_required", "unknown", "not_applicable"
    publication_mode: str | None = None  # "simulated_local", "published_github"
    verified_tree_id: str | None = None
    publication_commit_sha: str | None = None
    manifest_hash: str | None = None
    error_detail: str | None = None
    challenge_status: str | None = None
