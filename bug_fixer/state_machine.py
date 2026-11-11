"""Controlled workflow state machine and orchestrator for Bug Fixer."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from bug_fixer.config import Settings
from bug_fixer.db import EvidenceStore, compute_sha256
from bug_fixer.docker_runner import validate_workspace
from bug_fixer.intake import (
    compute_test_hash,
    fetch_github_issue,
    parse_issue_url,
    pin_and_checkout_base,
)
from bug_fixer.llm_adapter import call_qwen_repair
from bug_fixer.models import (
    Diagnosis,
    IssueInfo,
    PromptExcerpt,
    RunRecord,
    RunState,
    ValidationSummary,
)
from bug_fixer.reporting import write_run_reports
from bug_fixer.retrieval import build_compact_repair_packet, collect_evidence_excerpts
from bug_fixer.verification import (
    check_publication_eligibility,
    generate_candidate_patch,
    verify_patch_reconstructs_candidate,
)
from bug_fixer.workspace import Workspace


class StateMachineError(RuntimeError):
    """Raised when an illegal state transition or invariant violation occurs."""


class BugFixerWorkflow:
    def __init__(self, settings: Settings, store: EvidenceStore | None = None) -> None:
        self.settings = settings
        db_file = self.settings.data_dir / "bug_fixer.db"
        art_dir = self.settings.data_dir / "artifacts"
        self.store = store or EvidenceStore(db_file, art_dir)

    def execute_fix(
        self,
        issue_url: str,
        *,
        source_repo: Path,
        base_commit: str | None = None,
        test_nodeid: str | None = None,
        allowed_paths: list[str] | None = None,
        mock_issue_data: dict[str, Any] | None = None,
        mock_llm_response: dict[str, Any] | None = None,
        runner_override: str | None = None,
    ) -> RunRecord:
        """Run the end-to-end bug fixing state machine."""
        run_id = "run_" + uuid.uuid4().hex[:12]
        runner = runner_override or self.settings.runner

        # STAGE 1: ISSUE_LOADED
        owner, repo, number = parse_issue_url(issue_url)
        issue_info = fetch_github_issue(owner, repo, number, mock_data=mock_issue_data)
        if test_nodeid:
            issue_info.reproducer_test = test_nodeid

        run_workspace_dir = self.settings.data_dir / "runs" / run_id / "workspace"
        is_live = (mock_issue_data is None and bool(getattr(issue_info, "base_commit", "")))
        provenance = "live_github" if is_live else "fixture"
        base_branch = getattr(issue_info, "default_branch", "main") or "main"

        run_record = RunRecord(
            run_id=run_id,
            issue_url=issue_info.url,
            issue_number=issue_info.number,
            repo_owner=issue_info.owner,
            repo_name=issue_info.repo,
            base_sha="",
            stage=RunState.ISSUE_LOADED,
            status="running",
            provenance=provenance,
            base_branch=base_branch,
            reproducer_nodeid=test_nodeid or issue_info.reproducer_test or "",
            reproducer_hash="",
            retry_budget=self.settings.max_repair_attempts,
            attempts_used=0,
            created_at="",
            updated_at="",
        )
        self.store.save_run(run_record)
        self.store.record_event(
            run_id, RunState.ISSUE_LOADED.value, "issue_parsed",
            data={"url": issue_url, "title": issue_info.title, "number": issue_info.number}
        )

        # STAGE 2: BASE_PINNED
        base_sha, workspace_path = pin_and_checkout_base(
            source_repo, run_workspace_dir, base_commit=base_commit
        )
        workspace = Workspace(workspace_path, max_file_bytes=self.settings.max_file_bytes)

        # Determine reproducer test if not explicitly passed
        reproducer_test = test_nodeid or issue_info.reproducer_test
        if not reproducer_test:
            # Discover first test file
            found_tests = list(workspace.root.rglob("test_*.py"))
            if found_tests:
                reproducer_test = found_tests[0].relative_to(workspace.root).as_posix()
            else:
                reproducer_test = "."

        reproducer_hash = compute_test_hash(workspace.root, reproducer_test)

        run_record.base_sha = base_sha
        run_record.reproducer_nodeid = reproducer_test
        run_record.reproducer_hash = reproducer_hash
        run_record.stage = RunState.BASE_PINNED
        self.store.save_run(run_record, workspace.compute_workspace_hash())
        self.store.record_event(
            run_id, RunState.BASE_PINNED.value, "base_pinned",
            data={"base_sha": base_sha, "reproducer": reproducer_test, "reproducer_hash": reproducer_hash}
        )

        # STAGE 3: REPRODUCED (Run reproducer on clean pinned base)
        base_val = validate_workspace(
            workspace.root,
            timeout_seconds=self.settings.validation_timeout_seconds,
            selected_tests=[reproducer_test] if reproducer_test != "." else None,
            runner=runner,
            image=self.settings.validator_image,
        )

        # Store base validation artifact
        base_art_hash, _ = self.store.store_artifact(
            run_id, "base_validation", json.dumps(base_val.model_dump(), default=str), "base_validation.json"
        )

        if base_val.status == "environment_error" or base_val.reproducer_outcome == "environment_error":
            run_record.stage = RunState.ENVIRONMENT_ERROR
            run_record.status = "environment_error"
            run_record.error_detail = "Docker validator unavailable or crashed"
            self.store.save_run(run_record)
            self.store.record_event(
                run_id, RunState.ENVIRONMENT_ERROR.value, "environment_error",
                outputs_ref=base_art_hash, data={"issues": base_val.issues}
            )
            return run_record

        if base_val.reproducer_outcome == "passed":
            # Test already passes on base -> not_reproduced
            run_record.stage = RunState.NOT_REPRODUCED
            run_record.status = "not_reproduced"
            run_record.error_detail = f"Reproducer {reproducer_test} already passes on base {base_sha}"
            self.store.save_run(run_record)
            self.store.record_event(
                run_id, RunState.NOT_REPRODUCED.value, "not_reproduced",
                outputs_ref=base_art_hash, data={"reproducer": reproducer_test}
            )
            return run_record

        if base_val.reproducer_outcome != "matching_failure":
            run_record.stage = RunState.ENVIRONMENT_ERROR
            run_record.status = "environment_error"
            run_record.error_detail = f"Collection error or timeout on base: {base_val.issues}"
            self.store.save_run(run_record)
            self.store.record_event(
                run_id, RunState.ENVIRONMENT_ERROR.value, "reproduction_aborted",
                outputs_ref=base_art_hash, data={"outcome": base_val.reproducer_outcome}
            )
            return run_record

        # Reproducer confirmed failing! Lock reproducer and transition
        run_record.stage = RunState.REPRODUCED
        run_record.status = "reproduced"
        self.store.save_run(run_record, workspace.compute_workspace_hash())
        self.store.record_event(
            run_id, RunState.REPRODUCED.value, "defect_reproduced",
            inputs_ref=base_sha,
            outputs_ref=base_art_hash,
            data={
                "failing_assertions": base_val.failing_assertions,
                "exit_code": base_val.exit_code,
                "reproducer_nodeid": reproducer_test,
                "reproducer_hash": reproducer_hash,
            }
        )

        # STAGE 4: REPAIRING
        run_record.stage = RunState.REPAIRING
        self.store.save_run(run_record)

        latest_cand_val = base_val
        latest_diag = Diagnosis(
            suspected_cause="Initial defect state",
            supporting_evidence_ids=[],
            next_check_or_edit="Diagnose via Qwen",
            expected_observable_result="Pass reproducer",
        )
        latest_patch = ""
        earlier_attempts: list[dict[str, Any]] = []
        all_prompt_excerpts: list[PromptExcerpt] = []

        while run_record.attempts_used < run_record.retry_budget:
            run_record.attempts_used += 1
            attempt = run_record.attempts_used

            # 1. Retrieve evidence
            prompt_excerpts = collect_evidence_excerpts(
                workspace, latest_cand_val, issue_info, attempt, earlier_attempts=earlier_attempts
            )
            all_prompt_excerpts.extend(prompt_excerpts)

            # Store context packet artifact
            packet_text = build_compact_repair_packet(
                issue_info, reproducer_test, latest_cand_val, prompt_excerpts, attempt, earlier_attempts
            )
            packet_hash, _ = self.store.store_artifact(
                run_id, "prompt_packet", packet_text, f"packet_attempt_{attempt}.txt"
            )

            # 2. Call Qwen
            diag, raw_resp, tool_results = call_qwen_repair(
                packet_text,
                workspace,
                model=self.settings.model,
                base_url=self.settings.ollama_base_url,
                timeout_seconds=self.settings.validation_timeout_seconds * 2,
                mock_response=mock_llm_response,
            )
            latest_diag = diag

            # Record diagnosis & edits
            self.store.record_event(
                run_id, RunState.REPAIRING.value, "hypothesis_and_edit",
                attempt_number=attempt,
                inputs_ref=packet_hash,
                data={
                    "diagnosis": diag.model_dump(),
                    "executed_tools": tool_results,
                    "changed_files": workspace.changed_files,
                }
            )

            # 3. Check if any actual tool was called
            has_tool_edit = any(t.get("tool") not in {"none", "read_file", "search_files"} for t in tool_results)
            if not has_tool_edit or not workspace.changed_files:
                # Model produced reasoning without tool call -> no_change
                earlier_attempts.append({
                    "attempt": attempt,
                    "changed_files": [],
                    "status": "no_change",
                    "reason": "Model proposed reasoning without invoking an edit tool",
                })
                self.store.record_event(
                    run_id, RunState.REPAIRING.value, "no_change_recorded",
                    attempt_number=attempt,
                    data={"reason": "No workspace edits executed"}
                )
                self.store.save_run(run_record, workspace.compute_workspace_hash())
                continue

            # 4. Independent candidate validation
            cand_val = validate_workspace(
                workspace.root,
                timeout_seconds=self.settings.validation_timeout_seconds,
                selected_tests=[reproducer_test] if reproducer_test != "." else None,
                runner=runner,
                image=self.settings.validator_image,
            )
            latest_cand_val = cand_val

            cand_art_hash, _ = self.store.store_artifact(
                run_id, "candidate_validation", json.dumps(cand_val.model_dump(), default=str), f"cand_val_attempt_{attempt}.json"
            )

            latest_patch = generate_candidate_patch(workspace.root)
            patch_hash = compute_sha256(latest_patch)
            run_record.candidate_patch_hash = patch_hash

            if cand_val.reproducer_outcome == "passed":
                # Reproducer passed! Now validate full maintained test suite
                full_val = validate_workspace(
                    workspace.root,
                    timeout_seconds=self.settings.validation_timeout_seconds,
                    selected_tests=None,
                    runner=runner,
                    image=self.settings.validator_image,
                )
                latest_cand_val = full_val
                if full_val.passed:
                    # Verified repair!
                    run_record.stage = RunState.VERIFIED
                    run_record.status = "verified"
                    self.store.save_run(run_record, workspace.compute_workspace_hash())
                    self.store.record_event(
                        run_id, RunState.VERIFIED.value, "candidate_verified",
                        attempt_number=attempt,
                        outputs_ref=cand_art_hash,
                        data={"patch_hash": patch_hash, "changed_files": workspace.changed_files}
                    )
                    break

            # If failed, record contradicted prediction
            earlier_attempts.append({
                "attempt": attempt,
                "changed_files": list(workspace.changed_files),
                "status": "failed",
                "failures": cand_val.failing_assertions,
            })
            self.store.record_event(
                run_id, RunState.REPAIRING.value, "repair_attempt_failed",
                attempt_number=attempt,
                outputs_ref=cand_art_hash,
                data={"failing_assertions": cand_val.failing_assertions}
            )
            self.store.save_run(run_record, workspace.compute_workspace_hash())

        # If loop finished without verification
        if run_record.stage != RunState.VERIFIED:
            run_record.stage = RunState.REPAIR_FAILED
            run_record.status = "repair_failed"
            run_record.error_detail = "Exhausted retry budget without passing verification"
            self.store.save_run(run_record)
            self.store.record_event(
                run_id, RunState.REPAIR_FAILED.value, "repair_exhausted",
                data={"attempts_used": run_record.attempts_used}
            )
            return run_record

        # STAGE 5: READY_FOR_REVIEW
        reconstructs = verify_patch_reconstructs_candidate(source_repo, latest_patch, workspace.root)
        current_rep_hash = compute_test_hash(workspace.root, reproducer_test)

        # Get reproducer bytes if file exists
        rep_file_rel = reproducer_test.split("::")[0].replace("\\", "/")
        rep_file_path = workspace.root / rep_file_rel
        rep_bytes = rep_file_path.read_bytes() if rep_file_path.is_file() else None

        eligible, disq_reasons = check_publication_eligibility(
            base_reproduced=True,
            candidate_reproducer_passed=True,
            candidate_suite_passed=latest_cand_val.passed,
            patch_text=latest_patch,
            changed_files=workspace.changed_files,
            original_reproducer_hash=reproducer_hash,
            current_reproducer_hash=current_rep_hash,
            allowed_paths=allowed_paths,
            base_sha=run_record.base_sha,
        )
        if not reconstructs:
            eligible = False
            disq_reasons.append("Patch application to clean pinned base failed to reconstruct candidate workspace.")

        # Write reports, candidate.patch, reproducer.test, and manifest.json
        write_run_reports(
            self.settings.data_dir / "artifacts",
            run_record,
            base_val,
            latest_cand_val,
            all_prompt_excerpts,
            latest_patch,
            workspace.changed_files,
            latest_diag,
            workspace.tool_calls,
            reviewer=None,
            eligible_for_publication=eligible,
            reasons_ineligible=disq_reasons,
            reproducer_bytes=rep_bytes,
        )

        # Re-verify manifest on disk to ensure store integrity
        manifest_ok, manifest_errs = self.store.verify_manifest(run_id)
        if not manifest_ok:
            eligible = False
            disq_reasons.extend(manifest_errs)

        if eligible:
            run_record.stage = RunState.READY_FOR_REVIEW
            run_record.status = "ready_for_review"
        else:
            run_record.stage = RunState.VERIFIED
            run_record.status = "ineligible_for_review"
            run_record.error_detail = "; ".join(disq_reasons)

        self.store.save_run(run_record, workspace.compute_workspace_hash())
        self.store.record_event(
            run_id, run_record.stage.value, "ready_for_review" if eligible else "ineligible_for_review",
            data={"eligible": eligible, "disqualifications": disq_reasons}
        )

        return run_record

    def resume_run(
        self,
        run_id: str,
        *,
        source_repo: Path,
        mock_llm_response: dict[str, Any] | None = None,
        runner_override: str | None = None,
    ) -> RunRecord:
        """Resume a checkpointed run without repeating completed stages or resetting budget."""
        run = self.store.get_run(run_id)
        if not run:
            raise StateMachineError(f"Run ID '{run_id}' not found in SQLite store.")

        workspace_dir = self.settings.data_dir / "runs" / run_id / "workspace"
        if not workspace_dir.is_dir():
            raise StateMachineError(f"Workspace directory for run '{run_id}' not found.")

        workspace = Workspace(workspace_dir, max_file_bytes=self.settings.max_file_bytes)
        stored_ws_hash = self.store.get_workspace_hash(run_id)
        current_ws_hash = workspace.compute_workspace_hash()

        if stored_ws_hash and stored_ws_hash != current_ws_hash:
            raise StateMachineError(
                f"Workspace mismatch on resume for run '{run_id}'. "
                f"Stored hash: {stored_ws_hash[:8]}, current: {current_ws_hash[:8]}. Revalidation required."
            )

        self.store.record_event(
            run_id, run.stage.value, "run_resumed",
            data={"stage": run.stage.value, "attempts_used": run.attempts_used}
        )

        if run.stage in (RunState.VERIFIED, RunState.READY_FOR_REVIEW, RunState.PUBLISHED):
            return run

        if run.stage in (RunState.NOT_REPRODUCED, RunState.ENVIRONMENT_ERROR, RunState.REPAIR_FAILED):
            return run

        # If paused after reproduction or during repair, continue repair loop
        # Build IssueInfo from stored record
        issue_info = IssueInfo(
            url=run.issue_url,
            owner=run.repo_owner,
            repo=run.repo_name,
            number=run.issue_number,
            title=f"Issue #{run.issue_number}",
            body="",
            reproducer_test=run.reproducer_nodeid,
        )

        # Retrieve base validation
        events = self.store.list_events(run_id)
        base_event = next((e for e in events if e.event_type == "defect_reproduced"), None)
        base_val = ValidationSummary(
            passed=False,
            reproducer_outcome="matching_failure",
            failing_assertions=base_event.data.get("failing_assertions", []) if base_event else [],
            exit_code=base_event.data.get("exit_code") if base_event else 1,
            selected_tests=[run.reproducer_nodeid],
        )

        # Resume repair loop with remaining retry budget
        runner = runner_override or self.settings.runner
        earlier_attempts: list[dict[str, Any]] = []
        all_prompt_excerpts: list[PromptExcerpt] = []
        latest_cand_val = base_val
        latest_diag = Diagnosis(
            suspected_cause="Resumed repair state",
            supporting_evidence_ids=[],
            next_check_or_edit="Resume diagnosis",
            expected_observable_result="Pass reproducer",
        )
        latest_patch = ""

        while run.attempts_used < run.retry_budget:
            run.attempts_used += 1
            attempt = run.attempts_used

            prompt_excerpts = collect_evidence_excerpts(
                workspace, latest_cand_val, issue_info, attempt, earlier_attempts=earlier_attempts
            )
            all_prompt_excerpts.extend(prompt_excerpts)

            packet_text = build_compact_repair_packet(
                issue_info, run.reproducer_nodeid, latest_cand_val, prompt_excerpts, attempt, earlier_attempts
            )
            packet_hash, _ = self.store.store_artifact(
                run_id, "prompt_packet", packet_text, f"packet_attempt_{attempt}.txt"
            )

            diag, raw_resp, tool_results = call_qwen_repair(
                packet_text,
                workspace,
                model=self.settings.model,
                base_url=self.settings.ollama_base_url,
                timeout_seconds=self.settings.validation_timeout_seconds * 2,
                mock_response=mock_llm_response,
            )
            latest_diag = diag

            cand_val = validate_workspace(
                workspace.root,
                timeout_seconds=self.settings.validation_timeout_seconds,
                selected_tests=[run.reproducer_nodeid] if run.reproducer_nodeid != "." else None,
                runner=runner,
                image=self.settings.validator_image,
            )
            latest_cand_val = cand_val

            cand_art_hash, _ = self.store.store_artifact(
                run_id, "candidate_validation", json.dumps(cand_val.model_dump(), default=str), f"cand_val_attempt_{attempt}.json"
            )

            latest_patch = generate_candidate_patch(workspace.root)
            patch_hash = compute_sha256(latest_patch)
            run.candidate_patch_hash = patch_hash

            if cand_val.reproducer_outcome == "passed":
                full_val = validate_workspace(
                    workspace.root,
                    timeout_seconds=self.settings.validation_timeout_seconds,
                    selected_tests=None,
                    runner=runner,
                    image=self.settings.validator_image,
                )
                latest_cand_val = full_val
                if full_val.passed:
                    run.stage = RunState.VERIFIED
                    run.status = "verified"
                    self.store.save_run(run, workspace.compute_workspace_hash())
                    break

            earlier_attempts.append({
                "attempt": attempt,
                "changed_files": list(workspace.changed_files),
                "status": "failed",
                "failures": cand_val.failing_assertions,
            })
            self.store.save_run(run, workspace.compute_workspace_hash())

        if run.stage == RunState.VERIFIED:
            current_rep_hash = compute_test_hash(workspace.root, run.reproducer_nodeid)
            rep_file_rel = run.reproducer_nodeid.split("::")[0].replace("\\", "/")
            rep_file_path = workspace.root / rep_file_rel
            rep_bytes = rep_file_path.read_bytes() if rep_file_path.is_file() else None

            eligible, disq_reasons = check_publication_eligibility(
                base_reproduced=True,
                candidate_reproducer_passed=True,
                candidate_suite_passed=latest_cand_val.passed,
                patch_text=latest_patch,
                changed_files=workspace.changed_files,
                original_reproducer_hash=run.reproducer_hash,
                current_reproducer_hash=current_rep_hash,
                base_sha=run.base_sha,
            )

            write_run_reports(
                self.settings.data_dir / "artifacts",
                run,
                base_val,
                latest_cand_val,
                all_prompt_excerpts,
                latest_patch,
                workspace.changed_files,
                latest_diag,
                workspace.tool_calls,
                reviewer=None,
                eligible_for_publication=eligible,
                reasons_ineligible=disq_reasons,
                reproducer_bytes=rep_bytes,
            )

            manifest_ok, manifest_errs = self.store.verify_manifest(run_id)
            if not manifest_ok:
                eligible = False
                disq_reasons.extend(manifest_errs)

            if eligible:
                run.stage = RunState.READY_FOR_REVIEW
                run.status = "ready_for_review"
            else:
                run.stage = RunState.VERIFIED
                run.status = "ineligible_for_review"
                run.error_detail = "; ".join(disq_reasons)
            self.store.save_run(run, workspace.compute_workspace_hash())
        else:
            run.stage = RunState.REPAIR_FAILED
            run.status = "repair_failed"
            self.store.save_run(run)

        return run
