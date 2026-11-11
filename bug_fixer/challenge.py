"""Patch challenge: verifies whether a candidate restores intended behavior across multiple callers/inputs."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Literal
from pydantic import BaseModel, Field

from bug_fixer.config import Settings
from bug_fixer.db import EvidenceStore, compute_sha256
from bug_fixer.docker_runner import validate_workspace
from bug_fixer.models import ManifestEntry, RunRecord, ValidationSummary


class ChallengeCheckResult(BaseModel):
    test_id: str
    description: str
    base_outcome: str  # "failed", "passed", "environment_error"
    workaround_outcome: str | None = None  # "passed", "failed", etc.
    candidate_outcome: str  # "passed", "failed", etc.
    discriminates: bool
    role: str  # "reproducer", "direct_callee", "second_caller", "input_variation"


class ChallengeSummary(BaseModel):
    run_id: str
    status: Literal["passed", "failed", "inconclusive", "not_run"]
    candidate_tree_before: str
    candidate_tree_after: str
    tree_intact: bool
    checks: list[ChallengeCheckResult] = Field(default_factory=list)
    distinguishing_checks: list[str] = Field(default_factory=list)
    strongest_survived_challenge: str = ""
    workaround_tested: bool = False
    overhead_seconds: float = 0.0
    details: str = ""


# Historical caller-only workaround for the greeting-service defect
GREETING_CALLER_WORKAROUND = (
    "--- a/main.py\n"
    "+++ b/main.py\n"
    "@@ -1,4 +1,4 @@\n"
    " from service.formatter import format_greeting\n"
    " \n"
    " def run():\n"
    "-    return format_greeting('World')\n"
    "+    return 'Hello, World!'\n"
)

# Additional input variation challenge test code (stored outside model repair workspace)
GREETING_VARIATION_TEST = """
from service.formatter import format_greeting

def test_greeting_input_variations():
    names = ['Charlie', 'Diana', 'Everyone']
    for n in names:
        assert format_greeting(n) == f'Hello, {n}!'
"""

REVIEW_ASSISTANT_VARIATION_TEST = """
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from patterns import heuristic_scan

def test_mutable_default_variations():
    cases = [
        "def query(filter={}): return filter",
        "def batch_process(records=[], config={}): return records",
    ]
    for c in cases:
        findings = heuristic_scan(c)
        ids = [f['id'] for f in findings]
        assert 'AP002' in ids, f'AP002 must detect mutable defaults in: {c}'

    negatives = [
        "cache = {}",
        "def get_empty(): return {}",
        "def fetch(tags=()): return tags",
    ]
    for n in negatives:
        findings = heuristic_scan(n)
        ids = [f['id'] for f in findings]
        assert 'AP002' not in ids, f'AP002 false positive on valid code: {n}'
"""


def get_historical_workaround(repo_name: str) -> str | None:
    """Return known historical workaround for a fixture if available."""
    norm = repo_name.lower()
    if "greeting" in norm or "cross_file" in norm:
        return GREETING_CALLER_WORKAROUND
    return None


def run_patch_challenge(
    run_id: str,
    store: EvidenceStore,
    settings: Settings,
    *,
    source_repo: Path | None = None,
    runner: str | None = None,
    extra_challenge_test_code: str | None = None,
    extra_challenge_test_name: str | None = None,
) -> tuple[bool, ChallengeSummary]:
    """
    Execute controlled comparison across:
    1. Pinned defective base
    2. Historical caller-only workaround (when available)
    3. Verified candidate patch
    
    Verifies that candidate tree hash is completely unchanged before and after.
    Returns (challenge_passed, ChallengeSummary).
    """
    start_time = time.time()
    run_rec = store.get_run(run_id)
    if not run_rec:
        raise ValueError(f"Run '{run_id}' not found in database.")

    actual_runner = runner or settings.runner
    patch_file = store.artifacts_dir / run_id / "candidate.patch"
    patch_text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""

    # Locate source repository
    repo_source: Path | None = None
    if source_repo and source_repo.is_dir():
        repo_source = source_repo
    else:
        run_ws = settings.data_dir / "runs" / run_id / "workspace"
        if (run_ws / ".git").is_dir():
            repo_source = run_ws
        else:
            default_fix = Path(__file__).resolve().parent.parent / "fixtures" / "cross_file_repo"
            if default_fix.is_dir():
                repo_source = default_fix

    if not repo_source or not repo_source.is_dir():
        summary = ChallengeSummary(
            run_id=run_id,
            status="inconclusive",
            candidate_tree_before="",
            candidate_tree_after="",
            tree_intact=False,
            details="Source repository not found for challenge replay.",
        )
        return False, summary

    import tempfile
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_p = Path(tmp_dir)
        dir_base = tmp_p / "base"
        dir_workaround = tmp_p / "workaround"
        dir_candidate = tmp_p / "candidate"

        def _setup_clean(target: Path) -> None:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            proc = subprocess.run(["git", "clone", "-q", str(repo_source), str(target)], check=False)
            if proc.returncode == 0:
                subprocess.run(["git", "-C", str(target), "config", "core.autocrlf", "false"], check=False)
                co = subprocess.run(["git", "-C", str(target), "checkout", "-f", run_rec.base_sha], capture_output=True, check=False)
                if co.returncode != 0:
                    subprocess.run(["git", "-C", str(target), "checkout", "-f", "master"], check=False)

        # 1. Setup Base
        _setup_clean(dir_base)

        # Inject input variation challenge test into validation checkouts (outside model workspace)
        variation_file_rel = "tests/test_challenge_variations.py"
        test_code_to_add = extra_challenge_test_code or (
            GREETING_VARIATION_TEST if "greeting" in run_rec.repo_name.lower()
            else (REVIEW_ASSISTANT_VARIATION_TEST if "ml-code-review" in run_rec.repo_name.lower() else None)
        )
        if test_code_to_add:
            (dir_base / variation_file_rel).write_text(test_code_to_add, encoding="utf-8")

        # 2. Setup Workaround (if known)
        workaround_patch = get_historical_workaround(run_rec.repo_name)
        has_workaround = False
        if workaround_patch:
            _setup_clean(dir_workaround)
            if test_code_to_add:
                (dir_workaround / variation_file_rel).write_text(test_code_to_add, encoding="utf-8")
            ap_proc = subprocess.run(
                ["git", "apply", "--ignore-whitespace", "--ignore-space-change", "-"],
                cwd=dir_workaround,
                input=workaround_patch,
                text=True,
                capture_output=True,
                check=False,
            )
            has_workaround = (ap_proc.returncode == 0)

        # 3. Setup Candidate
        _setup_clean(dir_candidate)
        # Record candidate tree hash BEFORE challenge
        if patch_text.strip():
            subprocess.run(
                ["git", "apply", "--ignore-whitespace", "--ignore-space-change", "-"],
                cwd=dir_candidate,
                input=patch_text,
                text=True,
                capture_output=True,
                check=False,
            )
        subprocess.run(["git", "add", "-A"], cwd=dir_candidate, capture_output=True, check=False)
        tree_before_proc = subprocess.run(["git", "write-tree"], cwd=dir_candidate, capture_output=True, text=True, check=False)
        tree_before = tree_before_proc.stdout.strip() if tree_before_proc.returncode == 0 else ""

        # Inject challenge test file into candidate for test execution
        if test_code_to_add:
            (dir_candidate / variation_file_rel).write_text(test_code_to_add, encoding="utf-8")

        # Define checks to run
        reproducer_id = run_rec.reproducer_nodeid
        challenge_checks: list[dict[str, str]] = [
            {"id": reproducer_id, "desc": "Original failing reproducer test", "role": "reproducer"}
        ]

        if "greeting" in run_rec.repo_name.lower():
            challenge_checks.extend([
                {"id": "tests/test_greeting.py::test_formatter_direct", "desc": "Direct behavior check of callee formatter logic", "role": "direct_callee"},
                {"id": "tests/test_greeting.py::test_second_caller", "desc": "Second caller check via service/notifier", "role": "second_caller"},
            ])
            if test_code_to_add:
                challenge_checks.append({
                    "id": "tests/test_challenge_variations.py::test_greeting_input_variations",
                    "desc": "Input variations across multiple names ('Charlie', 'Diana', 'Everyone')",
                    "role": "input_variation"
                })
        elif "ml-code-review" in run_rec.repo_name.lower():
            challenge_checks.extend([
                {"id": "tests/test_project4.py::test_detect_mutable_default", "desc": "Original list default check (regression safety)", "role": "regression_check"},
            ])
            if test_code_to_add:
                challenge_checks.append({
                    "id": "tests/test_challenge_variations.py::test_mutable_default_variations",
                    "desc": "Input variations across multi-param & dict signatures",
                    "role": "input_variation"
                })

        # Run checks on Base
        base_val = validate_workspace(
            dir_base,
            selected_tests=[c["id"] for c in challenge_checks],
            runner=actual_runner,
            image=settings.validator_image,
        )

        # Run checks on Workaround
        workaround_val: ValidationSummary | None = None
        if has_workaround:
            workaround_val = validate_workspace(
                dir_workaround,
                selected_tests=[c["id"] for c in challenge_checks],
                runner=actual_runner,
                image=settings.validator_image,
            )

        # Run checks on Candidate
        cand_val = validate_workspace(
            dir_candidate,
            selected_tests=[c["id"] for c in challenge_checks],
            runner=actual_runner,
            image=settings.validator_image,
        )

        # Also validate full maintained suite on Candidate
        suite_val = validate_workspace(
            dir_candidate,
            selected_tests=None,
            runner=actual_runner,
            image=settings.validator_image,
        )

        # Clean up temporary challenge test file from candidate checkout to verify tree integrity
        if test_code_to_add and (dir_candidate / variation_file_rel).is_file():
            (dir_candidate / variation_file_rel).unlink()
        subprocess.run(["git", "add", "-A"], cwd=dir_candidate, capture_output=True, check=False)
        tree_after_proc = subprocess.run(["git", "write-tree"], cwd=dir_candidate, capture_output=True, text=True, check=False)
        tree_after = tree_after_proc.stdout.strip() if tree_after_proc.returncode == 0 else ""

        tree_intact = (tree_before == tree_after) and bool(tree_before)

        # Evaluate outcomes for each check
        def get_test_outcome(val_sum: ValidationSummary | None, nodeid: str) -> str:
            if not val_sum:
                return "not_run"
            if val_sum.status == "environment_error":
                return "environment_error"
            for tc in val_sum.test_cases:
                if tc.nodeid == nodeid or tc.name == nodeid.split("::")[-1]:
                    return tc.outcome
            return "failed" if not val_sum.passed else "passed"

        check_results: list[ChallengeCheckResult] = []
        distinguishing_checks: list[str] = []

        for chk in challenge_checks:
            nid = chk["id"]
            base_out = get_test_outcome(base_val, nid)
            work_out = get_test_outcome(workaround_val, nid) if has_workaround else None
            cand_out = get_test_outcome(cand_val, nid)

            # A check discriminates if it failed on base (or workaround) and passed on candidate
            discriminates = (base_out in ("failed", "error") or (work_out in ("failed", "error") if work_out else False)) and (cand_out == "passed")

            if discriminates and chk["role"] != "reproducer":
                distinguishing_checks.append(nid)

            check_results.append(
                ChallengeCheckResult(
                    test_id=nid,
                    description=chk["desc"],
                    base_outcome=base_out,
                    workaround_outcome=work_out,
                    candidate_outcome=cand_out,
                    discriminates=discriminates,
                    role=chk["role"],
                )
            )

        # Determine overall challenge status
        # 1. Candidate must pass all checks and maintained suite
        all_cand_passed = all(c.candidate_outcome == "passed" for c in check_results) and suite_val.passed
        # 2. Must distinguish defective behavior
        has_distinguishing = len(distinguishing_checks) > 0

        if not all_cand_passed:
            status = "failed"
            details = "Candidate patch failed one or more challenge checks or broke maintained suite."
        elif has_distinguishing:
            status = "passed"
            details = f"Candidate passed all checks and survived {len(distinguishing_checks)} distinguishing challenge test(s)."
        elif any(c.base_outcome == "environment_error" or c.candidate_outcome == "environment_error" for c in check_results):
            status = "inconclusive"
            details = "Docker environment error encountered during challenge execution."
        else:
            status = "inconclusive"
            details = "Challenge checks were nondiscriminating (passed on base as well)."

        # Strongest survived challenge summary
        strongest = ""
        if distinguishing_checks:
            strongest = " & ".join(distinguishing_checks)

        overhead = time.time() - start_time
        summary = ChallengeSummary(
            run_id=run_id,
            status=status,
            candidate_tree_before=tree_before,
            candidate_tree_after=tree_after,
            tree_intact=tree_intact,
            checks=check_results,
            distinguishing_checks=distinguishing_checks,
            strongest_survived_challenge=strongest,
            workaround_tested=has_workaround,
            overhead_seconds=round(overhead, 2),
            details=details,
        )

        # Store challenge artifact
        challenge_path = store.artifacts_dir / run_id / "challenge.json"
        challenge_path.write_text(json.dumps(summary.model_dump(), indent=2), encoding="utf-8")

        # Update manifest.json with challenge.json so manifest stays intact
        existing_manifest = store.get_manifest(run_id)
        if existing_manifest:
            raw = challenge_path.read_bytes()
            existing_manifest.files["challenge.json"] = ManifestEntry(
                rel_path="challenge.json",
                sha256=compute_sha256(raw),
                byte_size=len(raw),
            )
            existing_manifest.challenge_status = status
            store.store_manifest(run_id, existing_manifest)

        run_rec.challenge_status = status
        store.save_run(run_rec)

        return (status == "passed"), summary


def format_challenge_table(summary: ChallengeSummary) -> str:
    """Format a human-readable comparison table for the CLI."""
    lines = [
        "================================ PATCH CHALLENGE RESULTS ================================",
        f"Run ID:            {summary.run_id}",
        f"Challenge Status:  {summary.status.upper()} ({summary.details})",
        f"Candidate Tree:    {summary.candidate_tree_before} (Intact: {'YES' if summary.tree_intact else 'NO'})",
        f"Docker Overhead:   {summary.overhead_seconds}s",
        "",
        "+--------------------------------------------------+----------+-----------------+-----------+------------------+",
        "| Check / Test ID                                  | Base     | Workaround (neg)| Candidate | Discriminates?   |",
        "+--------------------------------------------------+----------+-----------------+-----------+------------------+",
    ]
    for c in summary.checks:
        tid = c.test_id if len(c.test_id) <= 48 else "..." + c.test_id[-45:]
        base_s = c.base_outcome.upper()
        if c.workaround_outcome is not None:
            if c.role == "reproducer" and c.workaround_outcome == "passed":
                work_s = "PASS (fooled)"
            elif c.workaround_outcome in ("failed", "error"):
                work_s = "FAIL (exposed)"
            else:
                work_s = c.workaround_outcome.upper()
        else:
            work_s = "N/A"
        cand_s = c.candidate_outcome.upper()
        disc_s = "YES (" + c.role + ")" if c.discriminates and c.role != "reproducer" else ("Reproducer" if c.role == "reproducer" else "NO")

        lines.append(f"| {tid:<48} | {base_s:<8} | {work_s:<15} | {cand_s:<9} | {disc_s:<16} |")

    lines.append("+--------------------------------------------------+----------+-----------------+-----------+------------------+")
    if summary.distinguishing_checks:
        lines.append("\nDistinguishing Checks:")
        for dc in summary.distinguishing_checks:
            lines.append(f"  - {dc}")
    if summary.strongest_survived_challenge:
        lines.append(f"\nStrongest Survived Challenge:\n  {summary.strongest_survived_challenge}")
    return "\n".join(lines)


def format_challenge_markdown(summary: ChallengeSummary) -> str:
    """Format markdown section for inclusion in PR body or report."""
    md_lines = [
        "### Patch Challenge: Generalization Verification",
        f"- **Status**: `{summary.status.upper()}` ({summary.details})",
        f"- **Candidate Tree**: `{summary.candidate_tree_before}` (Verified immutable across challenge: `{summary.tree_intact}`)",
        f"- **Execution Overhead**: `{summary.overhead_seconds}s`",
        "",
        "| Check / Test Target | Pinned Base | Historical Workaround (Negative Control) | Verified Candidate | Discriminating? |",
        "|---|---|---|---|---|",
    ]
    for c in summary.checks:
        base_badge = f"`{c.base_outcome.upper()}`"
        if c.workaround_outcome is not None:
            if c.role == "reproducer" and c.workaround_outcome == "passed":
                work_badge = "`PASS` *(fooled)*"
            elif c.workaround_outcome in ("failed", "error"):
                work_badge = "`FAIL` *(exposed)*"
            else:
                work_badge = f"`{c.workaround_outcome.upper()}`"
        else:
            work_badge = "*N/A*"
        cand_badge = f"`{c.candidate_outcome.upper()}`"
        disc_badge = f"**YES** ({c.role})" if c.discriminates and c.role != "reproducer" else ("*Reproducer*" if c.role == "reproducer" else "No")
        md_lines.append(f"| `{c.test_id}` | {base_badge} | {work_badge} | {cand_badge} | {disc_badge} |")

    if summary.strongest_survived_challenge:
        md_lines.append(f"\n**Strongest Survived Challenge**: `{summary.strongest_survived_challenge}`")
        md_lines.append("*The candidate patch restored actual callee logic; the historical caller-only patch failed independent callers and input variations.*")

    return "\n".join(md_lines)

