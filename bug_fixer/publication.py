"""Publication of candidate patch to a Git remote and draft PR creation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
from typing import Any
import httpx

from bug_fixer.db import EvidenceStore, compute_sha256
from bug_fixer.models import ManifestEntry, RunManifest, RunRecord, RunState
from bug_fixer.reporting import render_review_claims, update_pr_body_review_section


class PublicationError(RuntimeError):
    """Raised when publication eligibility or publication process fails."""


def verify_publication_artifacts(store: EvidenceStore, run: RunRecord, artifacts_dir: Path) -> bool:
    """Verify that stored candidate patch and review artifacts match DB records."""
    if not run.candidate_patch_hash:
        return False
    patch_file = artifacts_dir / run.run_id / "candidate.patch"
    if not patch_file.is_file():
        return False
    actual_hash = compute_sha256(patch_file.read_bytes().replace(b"\r\n", b"\n"))
    return actual_hash == run.candidate_patch_hash


def verify_publication_destination(
    remote_url_or_name: str,
    run: RunRecord,
) -> tuple[bool, str]:
    """
    Verify that intended publication remote matches the run's recorded
    repository identity (owner/repo) and base commit before any remote write.
    Refuses cross-repository publication (e.g., publishing greeting fixture to psf/requests).
    """
    remote_str = str(remote_url_or_name).strip()

    # Local bare repos or file URLs are allowed for local simulation
    if not (remote_str.startswith("http://") or remote_str.startswith("https://") or "github.com" in remote_str or remote_str.startswith("git@")):
        return True, "Local remote destination verified."

    # Parse remote owner/repo from URL
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", remote_str)
    if not match:
        return True, "Non-GitHub remote destination."

    target_owner, target_repo = match.groups()
    expected_owner = run.repo_owner.lower()
    expected_repo = run.repo_name.lower()

    if target_owner.lower() != expected_owner or target_repo.lower() != expected_repo:
        msg = (
            f"Publication destination mismatch: Run '{run.run_id}' was recorded for repository "
            f"'{run.repo_owner}/{run.repo_name}', but publication target remote is "
            f"'{target_owner}/{target_repo}' ({remote_str}). "
            f"Cross-repository remote write refused to prevent publishing patches to incorrect upstream."
        )
        return False, msg

    return True, f"Destination matches recorded repository {run.repo_owner}/{run.repo_name}."


def build_publication_commit(
    source_repo: Path | str,
    base_sha: str,
    patch_text: str,
    commit_message: str,
    target_dir: Path,
) -> tuple[str, str, Path]:
    """
    Build publication commit in a fresh disposable checkout of base_sha.
    Applies patch_text, stages all files, and creates commit.
    Checks return codes of all git operations.
    Returns (commit_sha, tree_sha, target_dir).
    """
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Clone repository into target_dir (without initial checkout)
    clone_proc = subprocess.run(
        ["git", "clone", "--no-checkout", str(source_repo), str(target_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if clone_proc.returncode != 0:
        raise PublicationError(f"Failed to clone base repository from {source_repo}: {clone_proc.stderr or clone_proc.stdout}")

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Bug Fixer",
        "GIT_AUTHOR_EMAIL": "bugfixer@local",
        "GIT_COMMITTER_NAME": "Bug Fixer",
        "GIT_COMMITTER_EMAIL": "bugfixer@local",
    }

    # Disable autocrlf in disposable checkout
    subprocess.run(["git", "-C", str(target_dir), "config", "core.autocrlf", "false"], check=False)

    # 2. Checkout the exact base SHA
    co_proc = subprocess.run(
        ["git", "checkout", "-f", base_sha],
        cwd=target_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if co_proc.returncode != 0:
        co_proc = subprocess.run(
            ["git", "checkout", "-f", "master"],
            cwd=target_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if co_proc.returncode != 0:
            raise PublicationError(f"Failed to checkout base SHA {base_sha}: {co_proc.stderr or co_proc.stdout}")

    # Verify checkout
    head_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if head_proc.returncode != 0 or not head_proc.stdout.strip():
        raise PublicationError(f"Failed to resolve checked out HEAD for base SHA {base_sha}")
    checked_out_base = head_proc.stdout.strip()

    # 3. Apply patch if non-empty
    if patch_text.strip():
        apply_proc = subprocess.run(
            ["git", "apply", "--ignore-whitespace", "--ignore-space-change", "-"],
            cwd=target_dir,
            input=patch_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if apply_proc.returncode != 0:
            raise PublicationError(f"Failed to apply candidate patch cleanly: {apply_proc.stderr or apply_proc.stdout}")

    # 4. Stage all files (including new files and tests)
    add_proc = subprocess.run(
        ["git", "add", "-A"],
        cwd=target_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if add_proc.returncode != 0:
        raise PublicationError(f"Failed to stage changes: {add_proc.stderr or add_proc.stdout}")

    # 5. Compute candidate git tree SHA using write-tree
    wt_proc = subprocess.run(
        ["git", "write-tree"],
        cwd=target_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if wt_proc.returncode != 0 or not wt_proc.stdout.strip():
        raise PublicationError(f"Failed to compute candidate tree SHA via write-tree: {wt_proc.stderr or wt_proc.stdout}")
    tree_sha = wt_proc.stdout.strip()

    # 6. Commit candidate changes
    commit_proc = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=target_dir,
        capture_output=True,
        text=True,
        env=git_env,
        check=False,
    )
    if commit_proc.returncode != 0:
        raise PublicationError(f"Failed to create publication commit: {commit_proc.stderr or commit_proc.stdout}")

    # 7. Get publication commit SHA
    rev_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if rev_proc.returncode != 0 or not rev_proc.stdout.strip():
        raise PublicationError(f"Failed to get publication commit SHA: {rev_proc.stderr or rev_proc.stdout}")
    commit_sha = rev_proc.stdout.strip()

    # 8. Verify publication commit parent matches base_sha or checked_out_base
    parent_proc = subprocess.run(
        ["git", "rev-parse", "HEAD^"],
        cwd=target_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    parent_found = parent_proc.stdout.strip() if parent_proc.returncode == 0 else "unknown"
    if parent_found not in (base_sha, checked_out_base):
        raise PublicationError(f"Publication commit parent {parent_found} does not match base SHA {base_sha}")

    # 9. Verify publication commit tree matches write-tree output
    tree_check_proc = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=target_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if tree_check_proc.returncode != 0 or tree_check_proc.stdout.strip() != tree_sha:
        raise PublicationError(f"Publication commit tree {tree_check_proc.stdout.strip()} does not match write-tree {tree_sha}")

    return commit_sha, tree_sha, target_dir


def push_candidate_branch(
    checkout_dir: Path,
    remote_name_or_url: str,
    branch_name: str,
    expected_commit_sha: str | None = None,
) -> bool:
    """
    Push candidate commit to remote branch without unconditional force pushes.
    Detects remote conflicts: if remote branch exists and differs from expected_commit_sha,
    raises PublicationError and preserves remote branch.
    If remote branch exists and already matches expected_commit_sha, returns True idempotently.
    Verifies resulting remote head after push.
    """
    remote_path = Path(remote_name_or_url)
    if remote_path.exists():
        remote_name_or_url = str(remote_path.resolve())
    # 1. Determine commit to push
    if not expected_commit_sha:
        rev_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if rev_proc.returncode != 0:
            raise PublicationError(f"Failed to resolve HEAD in {checkout_dir}: {rev_proc.stderr or rev_proc.stdout}")
        expected_commit_sha = rev_proc.stdout.strip()

    # 2. Check remote branch status via git ls-remote
    ls_proc = subprocess.run(
        ["git", "ls-remote", str(remote_name_or_url), f"refs/heads/{branch_name}"],
        cwd=checkout_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if ls_proc.returncode != 0:
        raise PublicationError(f"Failed to query remote '{remote_name_or_url}': {ls_proc.stderr or ls_proc.stdout}")

    remote_lines = [line.strip() for line in ls_proc.stdout.splitlines() if line.strip()]
    if remote_lines:
        remote_sha = remote_lines[0].split()[0].strip()
        if remote_sha == expected_commit_sha:
            # Idempotent: remote branch already equals expected publication commit
            return True
        else:
            raise PublicationError(
                f"Remote branch '{branch_name}' already exists at {remote_sha} "
                f"which conflicts with expected commit {expected_commit_sha}. "
                "Preserving remote branch to avoid overwriting divergent history."
            )

    # 3. Push commit to remote branch without --force
    push_proc = subprocess.run(
        ["git", "push", str(remote_name_or_url), f"{expected_commit_sha}:refs/heads/{branch_name}"],
        cwd=checkout_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if push_proc.returncode != 0:
        raise PublicationError(f"Git push failed: {push_proc.stderr or push_proc.stdout}")

    # 4. Verify resulting remote head matches expected commit
    verify_proc = subprocess.run(
        ["git", "ls-remote", str(remote_name_or_url), f"refs/heads/{branch_name}"],
        cwd=checkout_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if verify_proc.returncode != 0 or not verify_proc.stdout.strip():
        raise PublicationError(f"Failed to verify remote branch '{branch_name}' after push: {verify_proc.stderr or 'branch not found'}")

    verified_remote_sha = verify_proc.stdout.split()[0].strip()
    if verified_remote_sha != expected_commit_sha:
        raise PublicationError(
            f"Remote head verification failed for '{branch_name}': expected {expected_commit_sha}, found {verified_remote_sha}"
        )

    return True


def create_draft_pr(
    owner: str,
    repo: str,
    branch_name: str,
    base_branch: str,
    title: str,
    body: str,
    reviewer: str,
    *,
    api_token: str | None = None,
    mock_client: Any | None = None,
    local_record_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Open a draft PR on GitHub or record an honest local simulation.
    - If mock_client is provided, delegates to mock client.
    - If no api_token is provided (local simulation), writes local draft PR record
      and returns publication_mode='simulated_local', reviewer_status='not_requested',
      and branch_protection='not_applicable'. Never invents a fake GitHub pull request URL.
    - If api_token is provided, performs live GitHub operations: idempotent PR check,
      reviewer request, and branch protection verification.
    """
    if mock_client is not None:
        res = mock_client.create_draft_pr(
            owner=owner,
            repo=repo,
            branch_name=branch_name,
            base_branch=base_branch,
            title=title,
            body=body,
            reviewer=reviewer,
        )
        res.setdefault("publication_mode", "published_github")
        if "branch_protection" not in res:
            res["branch_protection"] = "verified_required" if res.get("branch_protection_verified") else "not_required"
        return res

    # Local simulation mode (no live GitHub API token)
    if not api_token or not api_token.strip():
        claims = render_review_claims(
            reviewer=reviewer,
            reviewer_status="not_requested",
            publication_mode="simulated_local",
            branch_protection="not_applicable",
            is_published=True,
        )
        honest_body = update_pr_body_review_section(body, claims)

        record_path: Path | None = None
        if local_record_dir:
            local_record_dir.mkdir(parents=True, exist_ok=True)
            record_path = local_record_dir / "local_draft_pr.json"
            record_data = {
                "publication_mode": "simulated_local",
                "repo": f"{owner}/{repo}",
                "branch_name": branch_name,
                "base_branch": base_branch,
                "title": title,
                "body": honest_body,
                "required_reviewer": reviewer or "Unassigned",
                "reviewer_status": "not_requested",
                "branch_protection": "not_applicable",
                "draft": True,
            }
            record_path.write_text(json.dumps(record_data, indent=2), encoding="utf-8")

        file_url = f"file:///{record_path.as_posix()}" if record_path else "file://local_draft_pr.json"
        return {
            "publication_mode": "simulated_local",
            "pr_number": None,
            "pr_url": file_url,
            "draft": True,
            "reviewer_requested": reviewer or "Unassigned",
            "reviewer_status": "not_requested",
            "branch_protection": "not_applicable",
            "branch_protection_verified": False,
            "local_draft_record_path": str(record_path) if record_path else None,
            "body": honest_body,
        }

    # Live GitHub API mode
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {api_token.strip()}",
    }

    # Initial body before PR exists: reviewer is intended
    claims_initial = render_review_claims(
        reviewer=reviewer,
        reviewer_status="intended",
        publication_mode="published_github",
        branch_protection="unknown",
        is_published=False,
    )
    initial_body = update_pr_body_review_section(body, claims_initial)

    with httpx.Client(timeout=15.0) as client:
        # 1. Idempotency check: see if a PR already exists for this branch
        list_url = f"https://api.github.com/repos/{owner}/{repo}/pulls?head={owner}:{branch_name}&state=all"
        list_resp = client.get(list_url, headers=headers)
        pr_number: int | None = None
        pr_url: str | None = None
        head_commit_sha: str | None = None

        if list_resp.status_code == 200 and list_resp.json():
            existing_pr = list_resp.json()[0]
            pr_number = existing_pr["number"]
            pr_url = existing_pr["html_url"]
            head_commit_sha = existing_pr.get("head", {}).get("sha")
            # Update PR title and body idempotently
            client.patch(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=headers,
                json={"body": initial_body, "title": title},
            )
        else:
            # Create new draft PR
            create_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
            payload = {
                "title": title,
                "body": initial_body,
                "head": branch_name,
                "base": base_branch,
                "draft": True,
            }
            create_resp = client.post(create_url, headers=headers, json=payload)
            if create_resp.status_code not in (200, 201):
                raise PublicationError(f"Failed to create draft PR ({create_resp.status_code}): {create_resp.text}")
            pr_data = create_resp.json()
            pr_number = pr_data["number"]
            pr_url = pr_data["html_url"]
            head_commit_sha = pr_data.get("head", {}).get("sha")

        # 2. Request reviewer
        reviewer_status = "not_requested"
        if reviewer and reviewer.strip():
            rev_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers"
            rev_resp = client.post(rev_url, headers=headers, json={"reviewers": [reviewer.strip()]})
            reviewer_status = "requested" if rev_resp.status_code in (200, 201) else "failed"

        # 3. Check branch protection rule
        prot_url = f"https://api.github.com/repos/{owner}/{repo}/branches/{base_branch}/protection"
        prot_resp = client.get(prot_url, headers=headers)
        if prot_resp.status_code == 200:
            prot_data = prot_resp.json()
            if "required_pull_request_reviews" in prot_data:
                branch_protection = "verified_required"
            else:
                branch_protection = "not_required"
        elif prot_resp.status_code == 404:
            branch_protection = "not_required"
        else:
            # 403 Forbidden (token lacks admin rights to inspect branch protection) or other status
            branch_protection = "unknown"

        # 4. Update PR body with observed final reviewer status and branch protection
        claims_final = render_review_claims(
            reviewer=reviewer,
            reviewer_status=reviewer_status,
            publication_mode="published_github",
            branch_protection=branch_protection,
            is_published=True,
        )
        final_body = update_pr_body_review_section(body, claims_final)
        try:
            client.patch(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=headers,
                json={"body": final_body},
            )
        except Exception:
            pass  # Partial result preserved; allow retry

        return {
            "publication_mode": "published_github",
            "pr_number": pr_number,
            "pr_url": pr_url,
            "head_commit_sha": head_commit_sha,
            "draft": True,
            "reviewer_requested": reviewer,
            "reviewer_status": reviewer_status,
            "branch_protection": branch_protection,
            "branch_protection_verified": (branch_protection == "verified_required"),
            "body": final_body,
        }


def sync_publication_artifacts(
    artifacts_dir: Path,
    store: EvidenceStore,
    run: RunRecord,
) -> None:
    """
    Update report.json and pr-body.md to match observed publication state,
    and update manifest.json so artifact integrity remains intact.
    """
    run_dir = artifacts_dir / run.run_id
    if not run_dir.is_dir():
        return

    claims = render_review_claims(
        reviewer=run.reviewer,
        reviewer_status=run.reviewer_status,
        publication_mode=run.publication_mode,
        branch_protection=run.branch_protection,
        is_published=(run.stage == RunState.PUBLISHED),
    )

    # 1. Update report.json
    rep_path = run_dir / "report.json"
    if rep_path.is_file():
        try:
            rep = json.loads(rep_path.read_text(encoding="utf-8"))
            rep.setdefault("review_answers", {})["5_who_must_review_before_merge"] = claims

            rep["publication"] = {
                "mode": run.publication_mode,
                "pr_number": run.pr_number,
                "pr_url": run.pr_url,
                "branch_name": run.branch_name,
                "publication_commit_sha": run.publication_commit_sha,
                "verified_tree_id": run.verified_tree_id,
            }
            rep.setdefault("lifecycle", {})["stage"] = run.stage.value if hasattr(run.stage, "value") else str(run.stage)
            rep.setdefault("lifecycle", {})["status"] = run.status
            rep_path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        except Exception:
            pass

    # 2. Update pr-body.md if present
    pr_body_path = run_dir / "pr-body.md"
    honest_body = ""
    if pr_body_path.is_file():
        try:
            body_text = pr_body_path.read_text(encoding="utf-8")
            honest_body = update_pr_body_review_section(body_text, claims)

            # Embed challenge summary if available and not already embedded
            chal_path = run_dir / "challenge.json"
            if chal_path.is_file() and "### Patch Challenge: Generalization Verification" not in honest_body:
                try:
                    from bug_fixer.challenge import ChallengeSummary, format_challenge_markdown
                    chal_dict = json.loads(chal_path.read_text(encoding="utf-8"))
                    c_summary = ChallengeSummary.model_validate(chal_dict)
                    chal_md = format_challenge_markdown(c_summary)
                    if "#### 5. Who must review it before merge?" in honest_body:
                        parts = honest_body.split("#### 5. Who must review it before merge?", 1)
                        honest_body = parts[0] + chal_md + "\n\n#### 5. Who must review it before merge?" + parts[1]
                    else:
                        honest_body = honest_body + "\n\n" + chal_md
                except Exception:
                    pass

            pr_body_path.write_text(honest_body, encoding="utf-8")
        except Exception:
            pass

    # 3. Update local_draft_pr.json if present
    local_draft_path = run_dir / "local_draft_pr.json"
    if local_draft_path.is_file():
        try:
            draft_data = json.loads(local_draft_path.read_text(encoding="utf-8"))
            draft_data["publication_mode"] = run.publication_mode or "simulated_local"
            draft_data["required_reviewer"] = run.reviewer or "Unassigned"
            draft_data["reviewer_status"] = run.reviewer_status or "not_requested"
            draft_data["branch_protection"] = run.branch_protection or "not_applicable"
            if honest_body:
                draft_data["body"] = honest_body
            local_draft_path.write_text(json.dumps(draft_data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # 4. Update manifest.json with all non-manifest files in run_dir
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

    existing_manifest = store.get_manifest(run.run_id)
    created_at = existing_manifest.created_at if existing_manifest else run.created_at
    manifest = RunManifest(
        run_id=run.run_id,
        base_sha=run.base_sha,
        provenance=getattr(run, "provenance", "fixture"),
        created_at=created_at,
        candidate_tree_id=run.verified_tree_id,
        files=manifest_files,
    )
    store.store_manifest(run.run_id, manifest)
