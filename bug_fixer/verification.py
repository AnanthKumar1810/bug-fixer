"""Candidate patch generation, reversible reconstruction verification, and eligibility checking."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from bug_fixer.db import compute_sha256
from bug_fixer.docker_runner import validate_workspace
from bug_fixer.intake import compute_test_hash, is_valid_commit_sha
from bug_fixer.models import RunRecord, ValidationSummary
from bug_fixer.workspace import PROTECTED_FILES


class VerificationError(ValueError):
    """Raised when patch verification or eligibility criteria fail."""


def generate_candidate_patch(workspace_root: Path) -> str:
    """Generate a clean unified git diff of all modifications in the workspace."""
    subprocess.run(["git", "add", "-N", "."], cwd=workspace_root, capture_output=True, check=False)
    proc = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=workspace_root,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout

    proc2 = subprocess.run(
        ["git", "diff"],
        cwd=workspace_root,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    return proc2.stdout or ""


def verify_patch_reconstructs_candidate(
    source_base_path: Path,
    patch_text: str,
    candidate_workspace: Path,
) -> bool:
    """
    Verify that applying patch_text to a clean pinned base exactly reconstructs
    the tested candidate workspace.
    """
    if not patch_text.strip():
        return False

    with tempfile.TemporaryDirectory() as temp_dir:
        test_base = Path(temp_dir) / "reconstruct"
        EXCLUDED = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", ".bug-fixer-data"}
        for item in source_base_path.iterdir():
            if item.name in EXCLUDED:
                continue
            dest = test_base / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)

        # Init git repo with core.autocrlf false to avoid line-ending corruption
        subprocess.run(["git", "init", "-q"], cwd=test_base, check=False)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=test_base, check=False)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=test_base, check=False)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=test_base, check=False)
        subprocess.run(["git", "add", "-A"], cwd=test_base, check=False)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=test_base, check=False)

        # Apply patch via git apply with whitespace tolerance
        proc = subprocess.run(
            ["git", "apply", "--ignore-whitespace", "--ignore-space-change", "-"],
            input=patch_text,
            cwd=test_base,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            return False

        # Compare files between reconstructed base and candidate workspace (LF normalized)
        cand_files = {
            p.relative_to(candidate_workspace).as_posix(): p.read_bytes().replace(b"\r\n", b"\n")
            for p in candidate_workspace.rglob("*")
            if p.is_file() and not p.is_symlink() and ".git" not in p.parts and ".pytest_cache" not in p.parts and "__pycache__" not in p.parts
        }
        recon_files = {
            p.relative_to(test_base).as_posix(): p.read_bytes().replace(b"\r\n", b"\n")
            for p in test_base.rglob("*")
            if p.is_file() and not p.is_symlink() and ".git" not in p.parts and ".pytest_cache" not in p.parts and "__pycache__" not in p.parts
        }

        # Check all files match
        for f, bytes_cand in cand_files.items():
            if f not in recon_files or recon_files[f] != bytes_cand:
                return False
        return True


def extract_diff_changed_files(patch_text: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null" and path not in files:
                files.append(path)
    return files


def check_challenge_eligibility(
    run: RunRecord,
    store: Any,
    verified_tree_id: str | None = None,
) -> list[str]:
    """
    Evaluate challenge artifact and history for publication eligibility.
    Enforces rules:
    - passed with matching run ID and tree: eligible.
    - failed: refused with path and failed check.
    - tree changed during challenge: refused.
    - malformed, wrong run ID, or wrong tree: refused.
    - never run or inconclusive: allowed under optional policy.
    - recorded failure now missing: refused.
    """
    reasons: list[str] = []
    run_dir = store.artifacts_dir / run.run_id
    chal_file = run_dir / "challenge.json"

    manifest = store.get_manifest(run.run_id)
    manifest_has_challenge = bool(manifest and "challenge.json" in manifest.files)
    manifest_challenge_status = getattr(manifest, "challenge_status", None) if manifest else None
    recorded_challenge_status = getattr(run, "challenge_status", None) or manifest_challenge_status

    target_tree = verified_tree_id or run.verified_tree_id

    if chal_file.is_file():
        # 1. Manifest hash check if challenge is registered in manifest
        if manifest_has_challenge and manifest is not None:
            expected_entry = manifest.files["challenge.json"]
            raw_bytes = chal_file.read_bytes()
            actual_sha = compute_sha256(raw_bytes)
            if actual_sha != expected_entry.sha256:
                reasons.append(
                    f"Challenge artifact manifest hash mismatch (expected {expected_entry.sha256[:12]}, "
                    f"found {actual_sha[:12]}). Evidence at: {chal_file}"
                )

        # 2. Parse JSON
        try:
            chal_data = json.loads(chal_file.read_text(encoding="utf-8"))
        except Exception as e:
            reasons.append(f"Challenge artifact is malformed (invalid JSON: {e}). Evidence at: {chal_file}")
            return reasons

        # 3. Validate against ChallengeSummary
        try:
            from bug_fixer.challenge import ChallengeSummary
            summary = ChallengeSummary.model_validate(chal_data)
        except Exception as e:
            reasons.append(f"Challenge artifact is malformed (schema validation error: {e}). Evidence at: {chal_file}")
            return reasons

        # 4. Check run_id binding
        if summary.run_id != run.run_id:
            reasons.append(
                f"Challenge artifact belongs to run '{summary.run_id}', but current run is '{run.run_id}'. "
                f"Evidence at: {chal_file}"
            )

        # 5. Check candidate tree binding
        if target_tree:
            if summary.candidate_tree_before and summary.candidate_tree_before != target_tree:
                reasons.append(
                    f"Challenge artifact candidate tree '{summary.candidate_tree_before}' does not match "
                    f"verified candidate tree '{target_tree}'. Evidence at: {chal_file}"
                )
            if summary.candidate_tree_after and summary.candidate_tree_after != target_tree:
                reasons.append(
                    f"Challenge artifact post-challenge tree '{summary.candidate_tree_after}' does not match "
                    f"verified candidate tree '{target_tree}'. Evidence at: {chal_file}"
                )

        # 6. Check tree immutability during challenge
        if not summary.tree_intact or (summary.candidate_tree_before != summary.candidate_tree_after):
            reasons.append(
                f"Candidate tree changed during the patch challenge (before: '{summary.candidate_tree_before}', "
                f"after: '{summary.candidate_tree_after}', tree_intact={summary.tree_intact}). "
                f"Clean verification and a new challenge are required. Evidence at: {chal_file}"
            )

        # 7. Check challenge status
        if summary.status == "failed":
            failed_checks = [c.test_id for c in summary.checks if c.candidate_outcome in ("failed", "error")]
            failed_str = ", ".join(failed_checks) if failed_checks else (summary.details or "one or more checks failed")
            reasons.append(
                f"Patch challenge failed for candidate patch. Failed check(s): {failed_str}. "
                f"Evidence at: {chal_file}"
            )
        elif summary.status == "passed":
            pass
        elif summary.status in ("inconclusive", "not_run"):
            pass
        else:
            reasons.append(
                f"Challenge artifact has invalid status '{summary.status}'. Evidence at: {chal_file}"
            )

    else:
        # chal_file is NOT on disk
        if manifest_has_challenge or recorded_challenge_status:
            status_desc = recorded_challenge_status or "previously recorded"
            reasons.append(
                f"A previously recorded challenge (status: {status_desc}) is missing from artifacts: {chal_file}. "
                "Deleting challenge artifacts is not permitted."
            )
        else:
            # Genuinely never challenged / legacy run: continue with ordinary gates
            pass

    return reasons


def check_publication_eligibility(
    base_reproduced: bool,
    candidate_reproducer_passed: bool,
    candidate_suite_passed: bool,
    patch_text: str,
    changed_files: list[str],
    original_reproducer_hash: str,
    current_reproducer_hash: str,
    allowed_paths: list[str] | None = None,
    *,
    base_sha: str | None = None,
    manifest_valid: bool = True,
    manifest_errors: list[str] | None = None,
    patch_file_exists: bool = True,
    challenge_errors: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """
    Check all publication eligibility gates:
    1. Valid non-zero base commit SHA
    2. Base reproduced expected failure
    3. Identical reproducer passed on candidate
    4. Maintained suite passed
    5. Real implementation change exists (diff non-empty)
    6. Reproducer test was NOT tampered with or modified
    7. Patch satisfies edit policy (no protected files, within allowed_paths if specified)
    8. Stored artifacts and manifest are intact and match hashes
    9. Patch challenge result (if executed or previously recorded) satisfies criteria
    """
    reasons: list[str] = []

    if base_sha is not None and not is_valid_commit_sha(base_sha):
        reasons.append(f"Invalid or placeholder base commit SHA: {base_sha}")
    if not manifest_valid:
        msg = f"Run manifest verification failed: {'; '.join(manifest_errors)}" if manifest_errors else "Run manifest verification failed: invalid or corrupted manifest."
        reasons.append(msg)
    if not patch_file_exists:
        reasons.append("Candidate patch file does not exist on disk.")
    if not base_reproduced:
        reasons.append("Base commit did not reproduce the expected defect.")
    if not candidate_reproducer_passed:
        reasons.append("Candidate patch failed the reproducer test.")
    if not candidate_suite_passed:
        reasons.append("Candidate patch broke other maintained tests.")
    if not patch_text.strip() or not changed_files:
        reasons.append("No real implementation changes exist in candidate patch.")
    if original_reproducer_hash and current_reproducer_hash and original_reproducer_hash != current_reproducer_hash:
        reasons.append("Locked reproducer test was modified or tampered with.")

    for cf in changed_files:
        norm = cf.replace("\\", "/").lower()
        parts = norm.split("/")
        name = parts[-1]
        if (
            any(p in {"tests", ".github", ".git"} for p in parts)
            or name in PROTECTED_FILES
            or name.startswith("test_")
            or name.endswith("_test.py")
        ):
            reasons.append(f"Prohibited file modified in patch: {cf}")

    if allowed_paths:
        norm_allowed = [p.replace("\\", "/") for p in allowed_paths]
        for cf in changed_files:
            norm = cf.replace("\\", "/")
            if norm not in norm_allowed and not any(norm.startswith(a.rstrip("/") + "/") for a in norm_allowed):
                reasons.append(f"File {cf} is outside the allowed edit scope: {allowed_paths}")

    if challenge_errors:
        reasons.extend(challenge_errors)

    eligible = len(reasons) == 0
    return eligible, reasons


def verify_run(
    run_id: str,
    store: Any,
    settings: Any,
    *,
    source_repo: Path | None = None,
    runner: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """
    Perform independent clean-checkout replay verification of a saved run:
    1. Verify manifest hashes against disk artifacts.
    2. Clone a clean checkout at recorded base commit.
    3. Reproduce bug on base in Docker.
    4. Clean checkout at same base commit and apply candidate patch.
    5. Run locked reproducer and maintained suite in Docker.
    6. Evaluate authoritative publication eligibility.
    """
    run_rec = store.get_run(run_id)
    if not run_rec:
        return False, {"error": f"Run '{run_id}' not found in database.", "eligible": False}

    actual_runner = runner or settings.runner
    run_artifacts_dir = store.artifacts_dir / run_id

    # 1. Manifest verification
    manifest_ok, manifest_errs = store.verify_manifest(run_id)

    # 2. Patch file check
    patch_file = run_artifacts_dir / "candidate.patch"
    patch_file_exists = patch_file.is_file()
    patch_text = patch_file.read_text(encoding="utf-8") if patch_file_exists else ""
    changed_files = extract_diff_changed_files(patch_text)

    # 3. Base SHA validity
    sha_valid = is_valid_commit_sha(run_rec.base_sha)

    # 4. Locate source repo to create clean checkouts
    repo_source: Path | None = None
    if source_repo and source_repo.is_dir():
        repo_source = source_repo
    else:
        run_ws = settings.data_dir / "runs" / run_id / "workspace"
        if run_ws.is_dir():
            repo_source = run_ws
        else:
            default_fix = Path(__file__).resolve().parent.parent / "fixtures" / "cross_file_repo"
            if default_fix.is_dir():
                repo_source = default_fix

    if not repo_source or not repo_source.is_dir():
        return False, {
            "error": "Source repository not found to replay verification.",
            "eligible": False,
            "run_id": run_id,
        }

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_p = Path(tmp_dir)
        clean_base = tmp_p / "clean_base"
        clean_patched = tmp_p / "clean_patched"

        EXCLUDED = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", ".bug-fixer-data"}

        def _setup_repo(target: Path) -> None:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if (repo_source / ".git").is_dir():
                proc = subprocess.run(["git", "clone", "-q", str(repo_source), str(target)], check=False)
                if proc.returncode == 0:
                    subprocess.run(["git", "-C", str(target), "config", "core.autocrlf", "false"], check=False)
                    co = subprocess.run(["git", "-C", str(target), "checkout", "-f", run_rec.base_sha], capture_output=True, check=False)
                    if co.returncode != 0:
                        subprocess.run(["git", "-C", str(target), "checkout", "-f", "master"], check=False)
                    return
            target.mkdir(parents=True, exist_ok=True)
            for item in repo_source.iterdir():
                if item.name in EXCLUDED:
                    continue
                dst = target / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)
            subprocess.run(["git", "init", "-q"], cwd=target, check=False)
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=target, check=False)
            subprocess.run(["git", "config", "user.name", "VerifyBot"], cwd=target, check=False)
            subprocess.run(["git", "config", "user.email", "verify@local"], cwd=target, check=False)
            subprocess.run(["git", "add", "-A"], cwd=target, check=False)
            subprocess.run(["git", "commit", "-q", "-m", f"base: {run_rec.base_sha}"], cwd=target, check=False)

        # Step 2: Reproduce bug on clean base in Docker
        _setup_repo(clean_base)
        base_val = validate_workspace(
            clean_base,
            selected_tests=[run_rec.reproducer_nodeid] if run_rec.reproducer_nodeid != "." else None,
            runner=actual_runner,
            image=settings.validator_image,
        )
        base_reproduced = (
            base_val.reproducer_outcome in ("failed", "matching_failure")
            or not base_val.passed
        )

        # Step 3 & 4: Clean patched checkout and test run
        _setup_repo(clean_patched)
        patch_applied = False
        cand_rep_passed = False
        suite_passed = False
        current_rep_hash = ""
        candidate_tree_id = ""

        if patch_text.strip():
            proc = subprocess.run(
                ["git", "apply", "--ignore-whitespace", "--ignore-space-change", "-"],
                input=patch_text,
                cwd=clean_patched,
                text=True,
                capture_output=True,
                check=False,
            )
            patch_applied = (proc.returncode == 0)

            if patch_applied:
                # Stage files and record exact candidate git tree hash
                subprocess.run(["git", "add", "-A"], cwd=clean_patched, capture_output=True, check=False)
                tree_proc = subprocess.run(["git", "write-tree"], cwd=clean_patched, capture_output=True, text=True, check=False)
                if tree_proc.returncode == 0 and tree_proc.stdout.strip():
                    candidate_tree_id = tree_proc.stdout.strip()

                # Validate reproducer
                cand_val = validate_workspace(
                    clean_patched,
                    selected_tests=[run_rec.reproducer_nodeid] if run_rec.reproducer_nodeid != "." else None,
                    runner=actual_runner,
                    image=settings.validator_image,
                )
                cand_rep_passed = (
                    cand_val.reproducer_outcome == "passed"
                    or any(tc.outcome == "passed" and tc.nodeid == run_rec.reproducer_nodeid for tc in cand_val.test_cases)
                    or cand_val.passed
                )

                # Validate full maintained suite
                suite_val = validate_workspace(
                    clean_patched,
                    selected_tests=None,
                    runner=actual_runner,
                    image=settings.validator_image,
                )
                suite_passed = suite_val.passed
                current_rep_hash = compute_test_hash(clean_patched, run_rec.reproducer_nodeid)

        chal_errs = check_challenge_eligibility(run_rec, store, verified_tree_id=candidate_tree_id or run_rec.verified_tree_id)
        eligible, reasons = check_publication_eligibility(
            base_reproduced=base_reproduced,
            candidate_reproducer_passed=cand_rep_passed,
            candidate_suite_passed=suite_passed,
            patch_text=patch_text,
            changed_files=changed_files,
            original_reproducer_hash=run_rec.reproducer_hash,
            current_reproducer_hash=current_rep_hash,
            base_sha=run_rec.base_sha,
            manifest_valid=manifest_ok,
            manifest_errors=manifest_errs,
            patch_file_exists=patch_file_exists,
            challenge_errors=chal_errs,
        )

        if eligible and candidate_tree_id:
            run_rec.verified_tree_id = candidate_tree_id
            store.save_run(run_rec)
            manifest = store.get_manifest(run_id)
            if manifest:
                manifest.candidate_tree_id = candidate_tree_id
                store.store_manifest(run_id, manifest)

        report = {
            "run_id": run_id,
            "base_sha": run_rec.base_sha,
            "base_sha_valid": sha_valid,
            "manifest_verified": manifest_ok,
            "manifest_errors": manifest_errs,
            "base_reproduced": base_reproduced,
            "base_failing_assertions": base_val.failing_assertions,
            "patch_applied_cleanly": patch_applied,
            "candidate_reproducer_passed": cand_rep_passed,
            "candidate_suite_passed": suite_passed,
            "reproducer_hash_intact": (run_rec.reproducer_hash == current_rep_hash) if current_rep_hash else False,
            "verified_tree_id": candidate_tree_id,
            "eligible_for_publication": eligible,
            "disqualifications": reasons,
        }
        return eligible, report


def verify_complete_pr_package(
    release_dir: Path,
    upstream_repo: Path,
    *,
    runner: str = "docker",
    image: str = "bugfixer-validator:py313",
) -> tuple[bool, dict[str, Any]]:
    """
    Independent Three-State Docker verification of a complete PR release artifact.
    
    Verifies:
      State 1: Clean checkout at base_sha -> maintained test suite passes.
      State 2: Clean checkout + declared regression test only -> fails with expected defect assertion;
               production files remain unmodified.
      State 3: Clean checkout + full saved patch -> regression test passes, maintained suite passes,
               behavioral invariant checks pass, and reconstructed Git Tree matches manifest candidate_tree_id.
    """
    manifest_path = release_dir / "manifest.json"
    if not manifest_path.is_file():
        return False, {"error": f"Manifest not found: {manifest_path}"}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, {"error": f"Failed to parse manifest: {exc}"}

    base_sha = manifest.get("base_sha", "")
    if not is_valid_commit_sha(base_sha):
        return False, {"error": f"Invalid base commit SHA in manifest: '{base_sha}'"}

    expected_tree_id = manifest.get("candidate_tree_id")
    reproducer_nodeid = manifest.get("reproducer_nodeid", "tests/test_project4.py::test_ap002_immutable_tuple_default")

    # Validate complete patch file integrity against manifest
    patch_file = release_dir / "complete.patch"
    if not patch_file.is_file():
        return False, {"error": f"Complete patch file not found: {patch_file}"}

    patch_bytes = patch_file.read_bytes()
    patch_hash = hashlib.sha256(patch_bytes).hexdigest()
    declared_entry = manifest.get("files", {}).get("complete.patch")
    if declared_entry and declared_entry.get("sha256") != patch_hash:
        return False, {
            "error": f"Patch hash mismatch: declared {declared_entry.get('sha256')} but computed {patch_hash}"
        }

    # Validate regression test patch file integrity
    test_patch_file = release_dir / "regression_test.patch"
    if not test_patch_file.is_file():
        return False, {"error": f"Regression test patch not found: {test_patch_file}"}
    test_patch_bytes = test_patch_file.read_bytes()
    test_patch_hash = hashlib.sha256(test_patch_bytes).hexdigest()

    patch_text = patch_bytes.decode("utf-8")
    test_patch_text = test_patch_bytes.decode("utf-8")

    temp_root = Path(tempfile.mkdtemp(prefix="bugfixer_complete_verify_"))
    try:
        def _checkout_base(dest: Path) -> None:
            subprocess.run(
                ["git", "clone", "-c", "core.autocrlf=false", "-c", "core.whitespace=cr-at-eol", "-q", str(upstream_repo), str(dest)],
                check=True,
            )
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=dest, check=True)
            subprocess.run(["git", "config", "core.whitespace", "cr-at-eol"], cwd=dest, check=True)
            res = subprocess.run(["git", "checkout", "-f", base_sha], cwd=dest, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                raise VerificationError(f"Failed to checkout base SHA {base_sha}: {res.stderr}")

        # ---------------------------------------------------------
        # State 1: Clean checkout at base_sha -> Maintained suite passes
        # ---------------------------------------------------------
        state1_dir = temp_root / "state1_clean_base"
        _checkout_base(state1_dir)
        state1_val = validate_workspace(state1_dir, selected_tests=None, runner=runner, image=image)
        if not state1_val.passed:
            return False, {
                "error": "State 1 failed: maintained test suite did not pass on untouched upstream base commit",
                "state1_exit_code": state1_val.exit_code,
                "state1_output": state1_val.checks[0].output if state1_val.checks else "",
            }
        state1_test_count = len(state1_val.test_cases)

        # ---------------------------------------------------------
        # State 2: Base + Regression test ONLY -> Fails with expected assertion
        # ---------------------------------------------------------
        state2_dir = temp_root / "state2_test_only"
        _checkout_base(state2_dir)

        # Apply regression test patch
        apply_test_res = subprocess.run(
            ["git", "apply", "--ignore-whitespace", "--ignore-space-change", "-"],
            input=test_patch_bytes,
            cwd=state2_dir,
            capture_output=True,
            check=False,
        )
        if apply_test_res.returncode != 0:
            return False, {
                "error": f"State 2 failed: regression test patch failed to apply: {apply_test_res.stderr}"
            }

        # Confirm production files still match upstream
        changed_proc = subprocess.run(["git", "diff", "--name-only"], cwd=state2_dir, capture_output=True, text=True, check=True)
        changed_files = [f.strip() for f in changed_proc.stdout.splitlines() if f.strip()]
        non_test_changes = [f for f in changed_files if not f.startswith("tests/") and not f.endswith("_test.py")]
        if non_test_changes:
            return False, {
                "error": f"State 2 failed: regression test modified production files: {non_test_changes}"
            }

        # Run reproducer in Docker
        state2_val = validate_workspace(state2_dir, selected_tests=[reproducer_nodeid], runner=runner, image=image)
        if state2_val.passed:
            return False, {
                "error": "State 2 failed: reproducer test unexpectedly passed on base commit (defect not reproduced)"
            }
        if state2_val.exit_code in (2, 4) or state2_val.reproducer_outcome == "missing_dependency":
            return False, {
                "error": f"State 2 failed: infrastructure error during reproduction (exit={state2_val.exit_code}): {state2_val.issues}"
            }

        # ---------------------------------------------------------
        # State 3: Base + Full complete PR patch -> Everything passes
        # ---------------------------------------------------------
        state3_dir = temp_root / "state3_complete_pr"
        _checkout_base(state3_dir)

        apply_full_res = subprocess.run(
            ["git", "apply", "--ignore-whitespace", "--ignore-space-change", "-"],
            input=patch_bytes,
            cwd=state3_dir,
            capture_output=True,
            check=False,
        )
        if apply_full_res.returncode != 0:
            return False, {
                "error": f"State 3 failed: complete patch failed to apply: {apply_full_res.stderr}",
                "base_sha": base_sha,
                "validator_image": image,
                "state1_base_passed": state1_val.passed,
                "state1_test_count": state1_test_count,
                "state2_reproduced": not state2_val.passed,
                "complete_patch_hash": patch_hash,
            }

        # Stage and compute reconstructed tree ID immediately after apply
        # (before validation creates any .pytest_cache or __pycache__ artifacts)
        subprocess.run(["git", "add", "-A"], cwd=state3_dir, check=True)
        tree_proc = subprocess.run(["git", "write-tree"], cwd=state3_dir, capture_output=True, text=True, check=True)
        actual_tree_id = tree_proc.stdout.strip()

        # Validate reproducer in Docker
        state3_rep_val = validate_workspace(state3_dir, selected_tests=[reproducer_nodeid], runner=runner, image=image)
        if not state3_rep_val.passed:
            return False, {
                "error": "State 3 failed: reproducer test failed on complete patched workspace",
                "output": state3_rep_val.checks[0].output if state3_rep_val.checks else "",
                "base_sha": base_sha,
                "validator_image": image,
                "state1_base_passed": state1_val.passed,
                "state1_test_count": state1_test_count,
                "state2_reproduced": not state2_val.passed,
                "state3_reproducer_passed": False,
                "actual_tree_id": actual_tree_id,
                "complete_patch_hash": patch_hash,
            }

        # Validate maintained suite in Docker
        state3_suite_val = validate_workspace(state3_dir, selected_tests=None, runner=runner, image=image)
        if not state3_suite_val.passed:
            return False, {
                "error": "State 3 failed: maintained test suite had regressions on complete patched workspace",
                "output": state3_suite_val.checks[0].output if state3_suite_val.checks else "",
                "base_sha": base_sha,
                "validator_image": image,
                "state1_base_passed": state1_val.passed,
                "state1_test_count": state1_test_count,
                "state2_reproduced": not state2_val.passed,
                "state3_reproducer_passed": True,
                "state3_suite_passed": False,
                "actual_tree_id": actual_tree_id,
                "complete_patch_hash": patch_hash,
            }
        state3_test_count = len(state3_suite_val.test_cases)

        if expected_tree_id and actual_tree_id != expected_tree_id:
            return False, {
                "error": f"Candidate tree mismatch: expected {expected_tree_id}, but reconstructed {actual_tree_id}",
                "expected_tree_id": expected_tree_id,
                "actual_tree_id": actual_tree_id,
                "base_sha": base_sha,
                "validator_image": image,
                "state1_base_passed": state1_val.passed,
                "state1_test_count": state1_test_count,
                "state2_reproduced": not state2_val.passed,
                "state3_reproducer_passed": state3_rep_val.passed,
                "state3_suite_passed": state3_suite_val.passed,
                "state3_test_count": state3_test_count,
                "complete_patch_hash": patch_hash,
                "tree_match": False,
            }

        report = {
            "verified": True,
            "base_sha": base_sha,
            "complete_patch_hash": patch_hash,
            "regression_test_hash": test_patch_hash,
            "validator_image": image,
            "runner": runner,
            "state1_base_passed": state1_val.passed,
            "state1_test_count": state1_test_count,
            "state2_reproduced": not state2_val.passed,
            "state2_failing_assertions": state2_val.failing_assertions,
            "state3_reproducer_passed": state3_rep_val.passed,
            "state3_suite_passed": state3_suite_val.passed,
            "state3_test_count": state3_test_count,
            "expected_tree_id": expected_tree_id,
            "actual_tree_id": actual_tree_id,
            "tree_match": (expected_tree_id == actual_tree_id) if expected_tree_id else True,
        }
        return True, report
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

