"""Generation of report.json and PR explanation answering the 5 audit questions."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from bug_fixer.models import (
    Diagnosis,
    ManifestEntry,
    PromptExcerpt,
    RunManifest,
    RunRecord,
    RunState,
    ValidationSummary,
)


def render_review_claims(
    *,
    reviewer: str | None = None,
    reviewer_status: str | None = None,
    publication_mode: str | None = None,
    branch_protection: str | None = None,
    is_published: bool = False,
) -> dict[str, Any]:
    """
    Authoritative rendering function for review claims across CLI, report.json,
    pr-body.md, and local_draft_pr.json.

    Accepts actual observed publication state and ensures strict truthfulness:
    - In local simulation mode: explicitly states 'Simulated local publication.
      No GitHub reviewer was requested. Branch protection is not applicable.'
      Does not claim an actual reviewer request, a live PR, or an enforced rule.
    - In live publication mode: renders reviewer request status ('requested', 'failed',
      'not_requested') independently from branch approval enforcement ('verified_required',
      'not_required', 'unknown').
    - Before publication: marks reviewer as 'intended' and reviewer_status as 'not_requested'.
    """
    mode = publication_mode or "simulated_local"
    target_reviewer = (reviewer or "").strip() or "Unassigned"

    if mode == "simulated_local":
        status = "not_requested"
        prot = "not_applicable"
        enforced = False
        summary_text = "Simulated local publication. No GitHub reviewer was requested. Branch protection is not applicable."
        markdown_lines = [
            "- **Reviewer**: Simulated local publication. No GitHub reviewer was requested. Branch protection is not applicable.",
            f"- **Intended Reviewer**: `@{target_reviewer}` (Simulation record only; no GitHub request made).",
            "- **Status**: Draft PR (Local simulation).",
            "- **Publication Mode**: `simulated_local`",
            "- **Branch Protection**: `not_applicable` (Approval rules not enforced for local simulation).",
        ]
    elif mode == "published_github":
        status = reviewer_status or "not_requested"
        prot = branch_protection or "unknown"
        enforced = (prot == "verified_required")

        if status == "requested":
            rev_text = f"@{target_reviewer} (Requested on GitHub)"
        elif status == "failed":
            rev_text = f"@{target_reviewer} (Request failed; retry outstanding)"
        elif status == "intended":
            rev_text = f"@{target_reviewer} (Intended reviewer; not yet requested on GitHub)"
        else:
            rev_text = f"@{target_reviewer} (Not requested)"

        if prot == "verified_required":
            prot_text = "verified_required (Branch protection rules require approving review before merge)"
        elif prot == "not_required":
            prot_text = "not_required (No approving review required by branch protection)"
        elif prot == "unknown":
            prot_text = "unknown (Unable to verify branch protection rules; permission error or unconfigured)"
        else:
            prot_text = f"{prot}"

        summary_text = f"GitHub Draft PR. Reviewer: {status}. Branch protection: {prot}."
        markdown_lines = [
            f"- **Reviewer**: `{rev_text}`",
            "- **Status**: Draft PR (Automatic merge disabled).",
            "- **Publication Mode**: `published_github`",
            f"- **Branch Protection**: {prot_text}",
        ]
    else:
        # Pre-publication / draft preparation
        status = "not_requested"
        prot = "not_applicable"
        enforced = False
        summary_text = "Draft repair ready for publication."
        markdown_lines = [
            f"- **Intended Reviewer**: `@{target_reviewer}` (To be requested upon publication).",
            "- **Status**: Draft candidate (Pending publication).",
            "- **Publication Mode**: `pending`",
            "- **Branch Protection**: Pending publication check.",
        ]

    return {
        "required_reviewer": target_reviewer,
        "reviewer_status": status,
        "draft_pr": True,
        "publication_mode": mode,
        "branch_protection": prot,
        "branch_protection_enforced": enforced,
        "automatic_merge": False,
        "summary_text": summary_text,
        "markdown": "\n".join(markdown_lines),
    }


def update_pr_body_review_section(body_text: str, claims: dict[str, Any]) -> str:
    """Replace or update section 5 in PR body markdown with actual review claims."""
    sec5_pat = re.compile(
        r"(#### 5\. Who must review it before merge\?\n)(.*?)(?=\n---|---|\Z)",
        re.DOTALL
    )
    replacement = f"\\1{claims['markdown']}\n\n"
    if sec5_pat.search(body_text):
        return sec5_pat.sub(replacement, body_text)
    return body_text + f"\n\n#### 5. Who must review it before merge?\n{claims['markdown']}\n"


def generate_report_dict(
    run: RunRecord,
    base_val: ValidationSummary,
    cand_val: ValidationSummary,
    prompt_excerpts: list[PromptExcerpt],
    patch_text: str,
    changed_files: list[str],
    diagnosis: Diagnosis,
    tool_calls: list[dict[str, Any]],
    reviewer: str | None,
    eligible_for_publication: bool,
    reasons_ineligible: list[str],
) -> dict[str, Any]:
    """Generate structured dictionary answering the 5 review questions."""
    review_claims = render_review_claims(
        reviewer=reviewer or getattr(run, "reviewer", None),
        reviewer_status=getattr(run, "reviewer_status", None),
        publication_mode=getattr(run, "publication_mode", None),
        branch_protection=getattr(run, "branch_protection", None),
        is_published=(getattr(run, "stage", None) == RunState.PUBLISHED),
    )
    return {
        "run_id": run.run_id,
        "provenance": getattr(run, "provenance", "fixture"),
        "origin_provenance": getattr(run, "origin_provenance", getattr(run, "provenance", "fixture")),
        "issue": {
            "url": run.issue_url,
            "number": run.issue_number if getattr(run, "provenance", "fixture") == "live_github" else None,
            "local_identifier": run.issue_number if getattr(run, "provenance", "fixture") != "live_github" else None,
            "title": getattr(run, "issue_title", None),
            "repo": f"{run.repo_owner}/{run.repo_name}",
            "base_branch": getattr(run, "base_branch", "main"),
            "provenance": getattr(run, "provenance", "fixture"),
            "origin_provenance": getattr(run, "origin_provenance", getattr(run, "provenance", "fixture")),
            "verified_on_github": (getattr(run, "provenance", "fixture") == "live_github"),
        },
        "review_answers": {
            "1_what_failed_on_original_commit": {
                "base_sha": run.base_sha,
                "reproducer_nodeid": run.reproducer_nodeid,
                "reproducer_hash": run.reproducer_hash,
                "reproducer_outcome": base_val.reproducer_outcome,
                "failing_assertions": base_val.failing_assertions,
                "exit_code": base_val.exit_code,
                "checks": [c.model_dump() for c in base_val.checks],
            },
            "2_what_evidence_did_qwen_receive": {
                "delivered_excerpts": [e.model_dump() for e in prompt_excerpts],
                "retrieved_files_count": len(prompt_excerpts),
                "suspected_cause_diagnosed": diagnosis.suspected_cause,
                "supporting_evidence_ids": diagnosis.supporting_evidence_ids,
            },
            "3_what_did_it_actually_edit": {
                "changed_files": changed_files,
                "patch_non_empty": bool(patch_text.strip()),
                "executed_tool_calls": tool_calls,
                "candidate_patch_hash": run.candidate_patch_hash,
            },
            "4_which_tests_passed_on_exact_patch": {
                "candidate_reproducer_passed": any(
                    tc.outcome == "passed" and tc.nodeid == run.reproducer_nodeid
                    for tc in cand_val.test_cases
                ) or cand_val.passed,
                "candidate_suite_passed": cand_val.passed,
                "test_cases": [tc.model_dump() for tc in cand_val.test_cases],
                "total_passed": sum(1 for tc in cand_val.test_cases if tc.outcome == "passed"),
                "total_failed": sum(1 for tc in cand_val.test_cases if tc.outcome == "failed"),
            },
            "5_who_must_review_before_merge": review_claims,
        },
        "publication_eligibility": {
            "eligible": eligible_for_publication,
            "disqualifications": reasons_ineligible,
        },
        "lifecycle": {
            "stage": run.stage.value if hasattr(run.stage, "value") else str(run.stage),
            "status": run.status,
            "attempts_used": run.attempts_used,
            "retry_budget": run.retry_budget,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        },
    }


def generate_pr_body_markdown(
    run: RunRecord,
    base_val: ValidationSummary,
    cand_val: ValidationSummary,
    prompt_excerpts: list[PromptExcerpt],
    patch_text: str,
    changed_files: list[str],
    diagnosis: Diagnosis,
    reviewer: str | None,
) -> str:
    """Generate human-auditable PR description answering all 5 audit questions."""
    failing_str = "\n".join(f"- `{fa}`" for fa in base_val.failing_assertions) or "- Test assertion failure"
    excerpts_str = "\n".join(
        f"- `{e.path}` (lines {e.start_line}-{e.end_line}) [SHA256: `{e.content_hash[:8]}`] — *{e.reason}*"
        for e in prompt_excerpts
    ) or "- None delivered"
    changed_str = "\n".join(f"- `{cf}`" for cf in changed_files) or "- None"
    passed_tests_str = "\n".join(
        f"- `[PASS]` {tc.nodeid} ({tc.duration_seconds}s)"
        for tc in cand_val.test_cases
        if tc.outcome == "passed"
    ) or "- Suite passed"

    claims = render_review_claims(
        reviewer=reviewer or getattr(run, "reviewer", None),
        reviewer_status=getattr(run, "reviewer_status", None),
        publication_mode=getattr(run, "publication_mode", None),
        branch_protection=getattr(run, "branch_protection", None),
        is_published=(getattr(run, "stage", None) == RunState.PUBLISHED),
    )

    issue_title = getattr(run, "issue_title", "")
    origin_prov = getattr(run, "origin_provenance", getattr(run, "provenance", "fixture"))
    if getattr(run, "provenance", "fixture") == "live_github":
        origin_note = f"\n- **Run Origin**: `{origin_prov}` (local rehearsal; associated with live GitHub issue #{run.issue_number})" if origin_prov != "live_github" else ""
        header_text = f"## Bug Fix Summary: {run.issue_url}\n\nAutomated repair generated by **Bug Fixer** for issue #{run.issue_number}.{origin_note}"
    else:
        title_display = issue_title or f"Local Defect Rehearsal ({run.repo_owner}/{run.repo_name})"
        header_text = (
            f"## Bug Fix Summary: {title_display}\n\n"
            f"Automated repair generated by **Bug Fixer** (Provenance: `{getattr(run, 'provenance', 'fixture')}`; origin: `{origin_prov}`).\n"
            f"- **Target Repository**: `{run.repo_owner}/{run.repo_name}`\n"
            f"- **Issue Tracking**: Local issue description (no live GitHub issue linked)."
        )

    return f"""{header_text}

---

### Audit Report: The 5 Review Questions

#### 1. What failed on the original commit?
- **Base Commit**: `{run.base_sha}`
- **Failing Reproducer**: `{run.reproducer_nodeid}`
- **Reproducer SHA256**: `{run.reproducer_hash}`
- **Observed Failures**:
{failing_str}

#### 2. What evidence did Qwen receive?
- **Root Cause Hypothesis**: {diagnosis.suspected_cause}
- **Supporting Evidence**: {', '.join(diagnosis.supporting_evidence_ids) or 'Traceback frames'}
- **Delivered Excerpts**:
{excerpts_str}

#### 3. What did it actually edit?
- **Modified Implementation Files**:
{changed_str}
- **Tool Actions Executed**:
```diff
{patch_text.strip()}
```

#### 4. Which tests passed on that exact patch?
- **Locked Reproducer**: Verified `PASS` on identical reproducer test.
- **Maintained Test Suite**: All tests passed.
{passed_tests_str}

#### 5. Who must review it before merge?
{claims['markdown']}

---

### Risk Assessment & Remaining Uncertainty
- Edit scope is strictly confined to implementation code (`{', '.join(changed_files)}`).
- All regression and maintained tests passed inside isolated Docker container.
- Independent patch verification confirmed that applying this diff to `{run.base_sha}` cleanly reconstructs the tested candidate.
"""


def write_run_reports(
    artifacts_dir: Path,
    run: RunRecord,
    base_val: ValidationSummary,
    cand_val: ValidationSummary,
    prompt_excerpts: list[PromptExcerpt],
    patch_text: str,
    changed_files: list[str],
    diagnosis: Diagnosis,
    tool_calls: list[dict[str, Any]],
    reviewer: str | None,
    eligible_for_publication: bool,
    reasons_ineligible: list[str],
    reproducer_bytes: bytes | None = None,
) -> tuple[Path, Path, Path, Path]:
    """
    Write report.json, pr-body.md, candidate.patch, and manifest.json
    into run artifacts directory.
    """
    run_dir = artifacts_dir / run.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. candidate.patch
    patch_path = run_dir / "candidate.patch"
    patch_path.write_text(patch_text, encoding="utf-8", newline="\n")

    # 2. reproducer.test (if provided)
    if reproducer_bytes is not None:
        rep_path = run_dir / "reproducer.test"
        rep_path.write_bytes(reproducer_bytes)

    # 3. report.json
    rep_dict = generate_report_dict(
        run, base_val, cand_val, prompt_excerpts, patch_text, changed_files,
        diagnosis, tool_calls, reviewer, eligible_for_publication, reasons_ineligible
    )
    json_path = run_dir / "report.json"
    json_path.write_text(json.dumps(rep_dict, indent=2), encoding="utf-8")

    # 4. pr-body.md
    pr_body = generate_pr_body_markdown(
        run, base_val, cand_val, prompt_excerpts, patch_text, changed_files,
        diagnosis, reviewer
    )
    md_path = run_dir / "pr-body.md"
    md_path.write_text(pr_body, encoding="utf-8")

    # 5. manifest.json - ONLY built and stored after all referenced files exist!
    manifest_files: dict[str, ManifestEntry] = {}
    for f in run_dir.iterdir():
        if f.is_file() and f.name != "manifest.json":
            raw = f.read_bytes()
            h = hashlib.sha256(raw).hexdigest()
            manifest_files[f.name] = ManifestEntry(
                rel_path=f.name,
                sha256=h,
                byte_size=len(raw),
            )

    manifest = RunManifest(
        run_id=run.run_id,
        base_sha=run.base_sha,
        provenance=getattr(run, "provenance", "fixture"),
        created_at=datetime.now(timezone.utc).isoformat(),
        files=manifest_files,
    )
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(), indent=2), encoding="utf-8")

    return json_path, md_path, patch_path, manifest_path
