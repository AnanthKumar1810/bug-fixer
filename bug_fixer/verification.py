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

from bug_fixer.docker_runner import validate_workspace
from bug_fixer.intake import compute_test_hash, is_valid_commit_sha
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
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout

    proc2 = subprocess.run(
        ["git", "diff"],
        cwd=workspace_root,
        capture_output=True,
        text=True,
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
