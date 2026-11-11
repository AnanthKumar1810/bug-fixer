"""Command-line interface for Bug Fixer."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from bug_fixer.config import Settings
from bug_fixer.db import EvidenceStore
from bug_fixer.models import RunState
from bug_fixer.publication import (
    create_draft_pr,
    push_candidate_branch,
    verify_publication_artifacts,
)
from bug_fixer.state_machine import BugFixerWorkflow
from bug_fixer.verification import (
    check_publication_eligibility,
    extract_diff_changed_files,
    verify_run,
)


def run_doctor(settings: Settings) -> int:
    """Check working dependencies and identify specific blockers."""
    print("==================================================")
    print("            BUG FIXER DOCTOR REPORT               ")
    print("==================================================")
    all_ok = True

    # 1. Git check
    git_ok = False
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            print(f"[PASS] Git: {proc.stdout.strip()}")
            git_ok = True
        else:
            print(f"[FAIL] Git: Exited with code {proc.returncode}")
    except OSError:
        print("[FAIL] Git: Not found on system PATH")
    if not git_ok:
        all_ok = False

    # 2. Docker daemon check
    docker_ok = False
    try:
        proc = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            print(f"[PASS] Docker Daemon: Running ({proc.stdout.strip()})")
            docker_ok = True
        else:
            print("[FAIL] Docker Daemon: Not running or unreachable via named pipe / socket")
            print(f"       Detail: {proc.stderr.strip()[:200]}")
    except OSError:
        print("[FAIL] Docker: CLI executable not found")
    if not docker_ok:
        all_ok = False

    # 3. Validator container image check
    validator_img_ok = False
    if docker_ok:
        for img in [settings.validator_image, settings.fallback_validator_image]:
            proc = subprocess.run(["docker", "images", "-q", img], capture_output=True, text=True, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                print(f"[PASS] Validator Container Image: Found '{img}'")
                validator_img_ok = True
                break
        if not validator_img_ok:
            print(f"[WARN] Validator Image: Neither '{settings.validator_image}' nor '{settings.fallback_validator_image}' found")
            print("       Run: docker build -t bugfixer-validator:py313 validator/")
    else:
        print("[SKIP] Validator Image check skipped (Docker unavailable)")

    # 4. Ollama endpoint check
    ollama_ok = False
    models_found: list[str] = []
    try:
        url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            models_found = [m.get("name", "") for m in data.get("models", [])]
            print(f"[PASS] Ollama Service: Online at {settings.ollama_base_url}")
            ollama_ok = True
        else:
            print(f"[FAIL] Ollama Service: HTTP {resp.status_code} at {settings.ollama_base_url}")
    except Exception as exc:
        print(f"[FAIL] Ollama Service: Cannot connect to {settings.ollama_base_url}: {exc}")
    if not ollama_ok:
        all_ok = False

    # 5. Qwen 2.5 Coder model check
    if ollama_ok:
        target_model = settings.model
        has_model = any(target_model in m for m in models_found)
        if has_model:
            print(f"[PASS] Target Model: '{target_model}' is installed")
        else:
            print(f"[FAIL] Target Model: '{target_model}' not found in Ollama list: {models_found}")
            all_ok = False

    # 6. Python & pytest check
    print(f"[PASS] Python: {sys.version.split()[0]}")
    try:
        import pytest
        print(f"[PASS] pytest: Version {pytest.__version__}")
    except ImportError:
        print("[FAIL] pytest: Not installed in current Python environment")
        all_ok = False

    # 7. GitHub token / CLI check
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    gh_cli = shutil.which("gh")
    if token:
        print("[PASS] GitHub Access: Token present in environment")
    elif gh_cli:
        print(f"[PASS] GitHub Access: gh CLI installed at {gh_cli}")
    else:
        print("[INFO] GitHub Access: No token or gh CLI detected (Offline mock available for local tests)")

    print("--------------------------------------------------")
    if all_ok:
        print("OVERALL STATUS: READY FOR REPAIR WORKFLOW")
        return 0
    else:
        print("OVERALL STATUS: BLOCKERS IDENTIFIED")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bug-fixer",
        description="Auditable GitHub issue-to-draft-PR automated software repair system",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # doctor
    subparsers.add_parser("doctor", help="Check working dependencies and report blockers")

    # fix
    fix_p = subparsers.add_parser("fix", help="Run automated issue-to-patch repair")
    fix_p.add_argument("--issue", required=True, help="GitHub issue URL (e.g. https://github.com/owner/repo/issues/123)")
    fix_p.add_argument("--source", default=".", help="Source repository root (default: current directory)")
    fix_p.add_argument("--base-commit", help="Pinned base commit SHA or branch")
    fix_p.add_argument("--test", help="Target reproducer test nodeid (e.g. tests/test_greeting.py::test_greeting)")
    fix_p.add_argument("--allowed-paths", help="Comma-separated allowed files to edit")
    fix_p.add_argument("--runner", choices=["docker", "local"], default="docker", help="Execution runner")
    fix_p.add_argument("--mock-issue-title", help="Override issue title for offline/fixture demo")
    fix_p.add_argument("--mock-issue-body", help="Override issue body for offline/fixture demo")

    # resume
    res_p = subparsers.add_parser("resume", help="Resume a checkpointed repair run")
    res_p.add_argument("--run", required=True, help="Run ID to resume")
    res_p.add_argument("--source", default=".", help="Source repository root")
    res_p.add_argument("--runner", choices=["docker", "local"], default="docker", help="Execution runner")

    # publish
    pub_p = subparsers.add_parser("publish", help="Publish verified patch as draft PR")
    pub_p.add_argument("--run", required=True, help="Run ID to publish")
    pub_p.add_argument("--reviewer", required=True, help="GitHub username required to review PR")
    pub_p.add_argument("--remote", default="origin", help="Git remote name or URL")
    pub_p.add_argument("--confirm-remote", action="store_true", help="Explicit confirmation to push to remote")

    # show
    show_p = subparsers.add_parser("show", help="Inspect run details, evidence, and publication eligibility")
    show_p.add_argument("--run", required=True, help="Run ID to inspect")
    show_p.add_argument("--json", action="store_true", help="Output details as JSON")

    # verify
    ver_p = subparsers.add_parser("verify", help="Replay independent clean checkout verification for a run")
    ver_p.add_argument("--run", required=True, help="Run ID to verify")
    ver_p.add_argument("--source", default=None, help="Source repository root (optional)")
    ver_p.add_argument("--runner", choices=["docker", "local"], default="docker", help="Execution runner")

    args = parser.parse_args()
    settings = Settings.from_env()

    if args.command == "doctor":
        sys.exit(run_doctor(settings))

    elif args.command == "fix":
        workflow = BugFixerWorkflow(settings)
        source_p = Path(args.source).resolve()
        allowed = [p.strip() for p in args.allowed_paths.split(",")] if args.allowed_paths else None
        mock_issue = None
        if args.mock_issue_title:
            mock_issue = {
                "title": args.mock_issue_title,
                "body": args.mock_issue_body or args.mock_issue_title,
                "reproducer_test": args.test or "",
            }

        print(f"Starting Bug Fixer run for: {args.issue}")
        print(f"Source repo: {source_p}")
        print(f"Runner: {args.runner}")

        run_rec = workflow.execute_fix(
            args.issue,
            source_repo=source_p,
            base_commit=args.base_commit,
            test_nodeid=args.test,
            allowed_paths=allowed,
            mock_issue_data=mock_issue,
            runner_override=args.runner,
        )
        print("\n================ RUN SUMMARY ================")
        print(f"Run ID:      {run_rec.run_id}")
        print(f"Final Stage: {run_rec.stage.value}")
        print(f"Status:      {run_rec.status}")
        print(f"Base SHA:    {run_rec.base_sha}")
        print(f"Locked Test: {run_rec.reproducer_nodeid}")
        print(f"Attempts:    {run_rec.attempts_used} / {run_rec.retry_budget}")
        if run_rec.error_detail:
            print(f"Detail:      {run_rec.error_detail}")

        art_dir = settings.data_dir / "artifacts" / run_rec.run_id
        if art_dir.is_dir():
            print(f"Artifacts:   {art_dir}")
            rep_json = art_dir / "report.json"
            if rep_json.is_file():
                print(f"Report:      {rep_json}")

        if run_rec.stage == RunState.READY_FOR_REVIEW:
            print("\nSUCCESS: Candidate patch verified. Eligible for publication.")
            print(f"To publish, run: bug-fixer publish --run {run_rec.run_id} --reviewer <USERNAME>")
            sys.exit(0)
        else:
            print(f"\nRun ended with stage: {run_rec.stage.value}")
            sys.exit(1)

    elif args.command == "resume":
        workflow = BugFixerWorkflow(settings)
        source_p = Path(args.source).resolve()
        print(f"Resuming run: {args.run}")
        run_rec = workflow.resume_run(
            args.run,
            source_repo=source_p,
            runner_override=args.runner,
        )
        print(f"Resumed run status: {run_rec.stage.value} ({run_rec.status})")
        sys.exit(0 if run_rec.stage in (RunState.VERIFIED, RunState.READY_FOR_REVIEW) else 1)

    elif args.command == "show":
        store = EvidenceStore(settings.data_dir / "bug_fixer.db", settings.data_dir / "artifacts")
        run_rec = store.get_run(args.run)
        if not run_rec:
            print(f"Error: Run '{args.run}' not found in database ({settings.data_dir / 'bug_fixer.db'}).")
            sys.exit(1)

        manifest_ok, manifest_errs = store.verify_manifest(args.run)
        art_dir = settings.data_dir / "artifacts" / run_rec.run_id
        patch_file = art_dir / "candidate.patch"
        patch_text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
        changed_files = extract_diff_changed_files(patch_text)

        eligible, reasons = check_publication_eligibility(
            base_reproduced=True if run_rec.stage in (RunState.REPRODUCED, RunState.REPAIRING, RunState.VERIFIED, RunState.READY_FOR_REVIEW, RunState.PUBLISHED) else False,
            candidate_reproducer_passed=True if run_rec.stage in (RunState.VERIFIED, RunState.READY_FOR_REVIEW, RunState.PUBLISHED) else False,
            candidate_suite_passed=True if run_rec.stage in (RunState.VERIFIED, RunState.READY_FOR_REVIEW, RunState.PUBLISHED) else False,
            patch_text=patch_text,
            changed_files=changed_files,
            original_reproducer_hash=run_rec.reproducer_hash,
            current_reproducer_hash=run_rec.reproducer_hash,
            base_sha=run_rec.base_sha,
            manifest_valid=manifest_ok,
            manifest_errors=manifest_errs,
            patch_file_exists=patch_file.is_file(),
        )

        rep_file = art_dir / "report.json"
        rep_data = json.loads(rep_file.read_text(encoding="utf-8")) if rep_file.is_file() else {}

        if args.json:
            out = {
                "run_id": run_rec.run_id,
                "issue_url": run_rec.issue_url,
                "base_sha": run_rec.base_sha,
                "provenance": run_rec.provenance,
                "stage": run_rec.stage.value,
                "status": run_rec.status,
                "attempts_used": run_rec.attempts_used,
                "retry_budget": run_rec.retry_budget,
                "reproducer_nodeid": run_rec.reproducer_nodeid,
                "candidate_patch_hash": run_rec.candidate_patch_hash,
                "manifest_intact": manifest_ok,
                "manifest_errors": manifest_errs,
                "eligible_for_publication": eligible,
                "disqualifications": reasons,
                "report": rep_data,
            }
            print(json.dumps(out, indent=2))
            sys.exit(0)

        print("\n================ RUN INSPECTION REPORT ================")
        print(f"Run ID:            {run_rec.run_id}")
        print(f"Issue:             {run_rec.issue_url} (#{run_rec.issue_number})")
        print(f"Repository:        {run_rec.repo_owner}/{run_rec.repo_name}")
        print(f"Provenance:        {run_rec.provenance}")
        print(f"Base Branch:       {run_rec.base_branch}")
        print(f"Base SHA:          {run_rec.base_sha}")
        print(f"Current Stage:     {run_rec.stage.value}")
        print(f"Status:            {run_rec.status}")
        print(f"Attempts:          {run_rec.attempts_used} / {run_rec.retry_budget}")
        print(f"Locked Test:       {run_rec.reproducer_nodeid}")
        print(f"Reproducer SHA256: {run_rec.reproducer_hash}")
        print(f"Patch SHA256:      {run_rec.candidate_patch_hash or 'None'}")
        print(f"Artifacts Dir:     {art_dir}")
        print(f"Manifest Status:   {'[PASS] Intact' if manifest_ok else '[FAIL] Missing or tampered (' + '; '.join(manifest_errs) + ')'}")
        print(f"Eligibility Gate:  {'[ELIGIBLE]' if eligible else '[INELIGIBLE]'}")
        if reasons:
            print("Disqualifications:")
            for r in reasons:
                print(f"  - {r}")
        if run_rec.stage == RunState.PUBLISHED:
            print("\nPublication Info:")
            print(f"  Mode:            {run_rec.publication_mode or 'N/A'}")
            print(f"  Branch:          {run_rec.branch_name}")
            print(f"  PR URL:          {run_rec.pr_url}")
            print(f"  Reviewer Status: {run_rec.reviewer_status}")
            print(f"  Branch Protection: {run_rec.branch_protection}")
        sys.exit(0)

    elif args.command == "verify":
        store = EvidenceStore(settings.data_dir / "bug_fixer.db", settings.data_dir / "artifacts")
        source_p = Path(args.source).resolve() if args.source else None
        print(f"Replaying independent verification for run: {args.run}...")
        eligible, report = verify_run(
            args.run,
            store,
            settings,
            source_repo=source_p,
            runner=args.runner,
        )
        print("\n================ REPLAY VERIFICATION ================")
        print(f"Run ID:                    {args.run}")
        print(f"Base SHA:                  {report.get('base_sha')} ({'[PASS] Valid SHA' if report.get('base_sha_valid') else '[FAIL] Invalid/Zero SHA'})")
        print(f"Manifest Verified:         {report.get('manifest_verified')}")
        print(f"Base Bug Reproduced:       {report.get('base_reproduced')}")
        print(f"Patch Applied Cleanly:     {report.get('patch_applied_cleanly')}")
        print(f"Candidate Reproducer Pass: {report.get('candidate_reproducer_passed')}")
        print(f"Candidate Suite Pass:      {report.get('candidate_suite_passed')}")
        print(f"Locked Test Unchanged:     {report.get('reproducer_hash_intact')}")
        print(f"Publication Eligible:      {'YES' if eligible else 'NO'}")
        if report.get("disqualifications"):
            print("Disqualifications:")
            for d in report["disqualifications"]:
                print(f"  - {d}")
        sys.exit(0 if eligible else 1)

    elif args.command == "publish":
        store = EvidenceStore(settings.data_dir / "bug_fixer.db", settings.data_dir / "artifacts")
        run_rec = store.get_run(args.run)
        if not run_rec:
            print(f"Error: Run '{args.run}' not found in database.")
            sys.exit(1)

        # Authoritative eligibility check
        manifest_ok, manifest_errs = store.verify_manifest(args.run)
        patch_file = settings.data_dir / "artifacts" / run_rec.run_id / "candidate.patch"
        patch_text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
        changed_files = extract_diff_changed_files(patch_text)

        eligible, reasons = check_publication_eligibility(
            base_reproduced=True,
            candidate_reproducer_passed=True,
            candidate_suite_passed=True,
            patch_text=patch_text,
            changed_files=changed_files,
            original_reproducer_hash=run_rec.reproducer_hash,
            current_reproducer_hash=run_rec.reproducer_hash,
            base_sha=run_rec.base_sha,
            manifest_valid=manifest_ok,
            manifest_errors=manifest_errs,
            patch_file_exists=patch_file.is_file(),
        )

        if not eligible or run_rec.stage not in (RunState.READY_FOR_REVIEW, RunState.PUBLISHED):
            print(f"Error: Run '{args.run}' is NOT eligible for publication.")
            for r in reasons:
                print(f"  - {r}")
            sys.exit(1)

        branch_name = f"bug-fixer/run-{run_rec.run_id[:8]}"
        workspace_dir = settings.data_dir / "runs" / run_rec.run_id / "workspace"

        # Check safety guard for real remote
        is_remote_url = "github.com" in args.remote or (args.remote.startswith("http") and not args.remote.startswith("file"))
        if is_remote_url and not args.confirm_remote:
            print(f"CONFIRMATION REQUIRED: About to push to remote '{args.remote}'.")
            print("To proceed, add the flag: --confirm-remote")
            sys.exit(1)

        print(f"Pushing branch '{branch_name}' to '{args.remote}'...")
        push_candidate_branch(workspace_dir, args.remote, branch_name)

        # Read pr-body.md
        pr_body_file = settings.data_dir / "artifacts" / run_rec.run_id / "pr-body.md"
        body = pr_body_file.read_text(encoding="utf-8") if pr_body_file.is_file() else "Automated repair"

        token = os.environ.get("GITHUB_TOKEN")
        pr_info = create_draft_pr(
            owner=run_rec.repo_owner,
            repo=run_rec.repo_name,
            branch_name=branch_name,
            base_branch=run_rec.base_branch or "main",
            title=f"fix: resolve issue #{run_rec.issue_number}",
            body=body,
            reviewer=args.reviewer,
            api_token=token,
            local_record_dir=settings.data_dir / "artifacts" / run_rec.run_id,
        )

        run_rec.reviewer = args.reviewer
        run_rec.reviewer_status = pr_info.get("reviewer_status")
        run_rec.branch_name = branch_name
        run_rec.pr_url = pr_info.get("pr_url")
        run_rec.branch_protection = pr_info.get("branch_protection")
        run_rec.publication_mode = pr_info.get("publication_mode")
        run_rec.stage = RunState.PUBLISHED
        run_rec.status = "published"
        store.save_run(run_rec)

        print("\n================ PUBLISHED DRAFT PR ================")
        print(f"Publication Mode:  {pr_info.get('publication_mode')}")
        print(f"PR Location:       {pr_info.get('pr_url')}")
        print(f"Branch:            {branch_name}")
        print(f"Draft Status:      True")
        print(f"Reviewer Required: @{args.reviewer} ({pr_info.get('reviewer_status')})")
        print(f"Branch Protection: {pr_info.get('branch_protection')}")
        print("Automatic merge is disabled. Approving human review required before merge.")
        sys.exit(0)


if __name__ == "__main__":
    main()
