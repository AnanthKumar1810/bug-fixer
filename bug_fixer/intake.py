"""GitHub issue intake, URL parsing, base commit pinning, and reproducer locking."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
import httpx

from bug_fixer.models import IssueInfo

_ISSUE_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/issues/(\d+)(?:/.*)?$"
)
_PR_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:/.*)?$"
)


class IntakeError(ValueError):
    """Raised when issue URL or checkout intake fails validation."""


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Parse a GitHub issue URL into (owner, repo, issue_number). Reject PR URLs."""
    url = url.strip()
    if _PR_URL_RE.match(url):
        raise IntakeError(f"Rejected pull request URL: {url}. Only GitHub issue URLs are accepted.")
    match = _ISSUE_URL_RE.match(url)
    if not match:
        raise IntakeError(f"Invalid GitHub issue URL format: '{url}'. Expected https://github.com/OWNER/REPO/issues/NUMBER")
    owner, repo, number_str = match.groups()
    return owner, repo, int(number_str)


_HEX_40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def is_valid_commit_sha(sha: str | None) -> bool:
    """Validate that sha is a genuine 40-character hexadecimal commit hash."""
    if not sha or not _HEX_40_RE.match(sha.strip()):
        return False
    sha_lower = sha.strip().lower()
    # Reject all-zeroes placeholder
    if sha_lower == "0" * 40:
        return False
    # Reject trivial repetitive placeholder hashes (e.g., '1111...', 'aaaa...')
    if len(set(sha_lower)) <= 2:
        return False
    return True


def fetch_github_issue(
    owner: str,
    repo: str,
    number: int,
    *,
    api_token: str | None = None,
    mock_data: dict[str, Any] | None = None,
) -> IssueInfo:
    """Fetch GitHub issue metadata and resolve default branch to exact commit SHA."""
    issue_url = f"https://github.com/{owner}/{repo}/issues/{number}"
    if mock_data is not None:
        return IssueInfo(
            url=issue_url,
            owner=owner,
            repo=repo,
            number=number,
            title=mock_data.get("title", f"Issue #{number}"),
            body=mock_data.get("body", ""),
            default_branch=mock_data.get("default_branch", "main"),
            base_commit=mock_data.get("base_commit", ""),
            reproducer_test=mock_data.get("reproducer_test", ""),
        )

    headers = {"Accept": "application/vnd.github.v3+json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(api_url, headers=headers)
            if resp.status_code == 404:
                raise IntakeError(f"GitHub issue not found (404): {issue_url}")
            if resp.status_code == 403:
                raise IntakeError(f"Access forbidden (403) or rate limit exceeded for: {issue_url}")
            resp.raise_for_status()
            data = resp.json()

            if "pull_request" in data:
                raise IntakeError(f"URL points to a pull request, not an issue: {issue_url}")

            repo_url = f"https://api.github.com/repos/{owner}/{repo}"
            repo_resp = client.get(repo_url, headers=headers)
            default_branch = "main"
            if repo_resp.status_code == 200:
                default_branch = repo_resp.json().get("default_branch", "main")

            # Resolve default branch to exact commit SHA via commits endpoint
            base_sha = ""
            try:
                commit_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{default_branch}"
                commit_resp = client.get(commit_url, headers=headers)
                if commit_resp.status_code == 200:
                    base_sha = commit_resp.json().get("sha", "")
            except Exception:
                pass

            return IssueInfo(
                url=issue_url,
                owner=owner,
                repo=repo,
                number=number,
                title=data.get("title", ""),
                body=data.get("body", "") or "",
                default_branch=default_branch,
                base_commit=base_sha,
            )
    except httpx.RequestError as exc:
        raise IntakeError(f"Network error accessing GitHub API: {exc}")


def pin_and_checkout_base(
    source_path: Path,
    target_workspace: Path,
    base_commit: str | None = None,
) -> tuple[str, Path]:
    """Create a fresh workspace checkout from source and pin the exact base commit SHA."""
    target_workspace.mkdir(parents=True, exist_ok=True)

    # If an explicit base_commit is supplied (even all-zeros for testing rejection), preserve it
    if base_commit:
        if (source_path / ".git").exists():
            try:
                proc = subprocess.run(
                    ["git", "-C", str(source_path), "rev-parse", base_commit],
                    capture_output=True, text=True, check=True
                )
                sha = proc.stdout.strip()
            except subprocess.CalledProcessError:
                sha = base_commit
        else:
            sha = base_commit
    elif (source_path / ".git").exists():
        proc = subprocess.run(
            ["git", "-C", str(source_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False
        )
        if proc.returncode == 0 and proc.stdout.strip():
            sha = proc.stdout.strip()
        else:
            sha = "0" * 40
    else:
        # Fixture directory without git: initialize git and commit to produce a genuine commit SHA
        try:
            subprocess.run(["git", "init", "-q"], cwd=source_path, check=False)
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=source_path, check=False)
            subprocess.run(["git", "config", "core.whitespace", "cr-at-eol"], cwd=source_path, check=False)
            subprocess.run(["git", "config", "user.name", "FixtureAuthor"], cwd=source_path, check=False)
            subprocess.run(["git", "config", "user.email", "fixture@bugfixer.local"], cwd=source_path, check=False)
            subprocess.run(["git", "add", "-A"], cwd=source_path, check=False)
            subprocess.run(["git", "commit", "-q", "-m", "fixture base"], cwd=source_path, check=False)
            proc = subprocess.run(
                ["git", "-C", str(source_path), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=False
            )
            sha = proc.stdout.strip() if proc.returncode == 0 else "0" * 40
        except Exception:
            sha = "0" * 40

    EXCLUDED_COPY = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", ".bug-fixer-data"}
    for item in source_path.iterdir():
        if item.name in EXCLUDED_COPY:
            continue
        dest = target_workspace / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    subprocess.run(["git", "init", "-q"], cwd=target_workspace, check=False)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=target_workspace, check=False)
    subprocess.run(["git", "config", "core.whitespace", "cr-at-eol"], cwd=target_workspace, check=False)
    subprocess.run(["git", "config", "user.name", "BugFixerBot"], cwd=target_workspace, check=False)
    subprocess.run(["git", "config", "user.email", "bugfixer@local"], cwd=target_workspace, check=False)
    subprocess.run(["git", "add", "-A"], cwd=target_workspace, check=False)
    subprocess.run(["git", "commit", "-q", "-m", f"base: {sha}"], cwd=target_workspace, check=False)

    return sha, target_workspace


def compute_test_hash(workspace_root: Path, test_path_or_nodeid: str) -> str:
    """Compute sha256 hash of the reproducer test file."""
    file_rel = test_path_or_nodeid.split("::")[0].replace("\\", "/")
    test_file = workspace_root / file_rel
    if test_file.is_file():
        # Normalize CRLF for stable cross-platform hash
        content = test_file.read_bytes().replace(b"\r\n", b"\n")
        return hashlib.sha256(content).hexdigest()
    return hashlib.sha256(test_path_or_nodeid.encode("utf-8")).hexdigest()


def associate_live_issue(
    run_rec: Any,
    live_issue_url: str,
    store: Any,
    *,
    api_token: str | None = None,
) -> Any:
    """
    Associate a verified live GitHub issue with a run originally conducted under local rehearsal.
    Preserves audit history by recording an 'issue_associated' audit event.
    Rejects mismatched repositories, pull request URLs, or 404/invalid issues.
    """
    owner, repo, number = parse_issue_url(live_issue_url)
    if owner.lower() != run_rec.repo_owner.lower() or repo.lower() != run_rec.repo_name.lower():
        raise IntakeError(
            f"Repository mismatch: run is for '{run_rec.repo_owner}/{run_rec.repo_name}', but issue is for '{owner}/{repo}'"
        )
    live_info = fetch_github_issue(owner, repo, number, api_token=api_token)

    # Record association audit event before updating
    store.record_event(
        run_rec.run_id,
        run_rec.stage.value if hasattr(run_rec.stage, "value") else str(run_rec.stage),
        "issue_associated",
        data={
            "previous_provenance": run_rec.provenance,
            "previous_issue_url": run_rec.issue_url,
            "previous_issue_number": run_rec.issue_number,
            "previous_issue_title": getattr(run_rec, "issue_title", None),
            "live_issue_url": live_info.url,
            "live_issue_number": live_info.number,
            "live_issue_title": live_info.title,
        },
    )

    run_rec.issue_url = live_info.url
    run_rec.issue_number = live_info.number
    run_rec.issue_title = live_info.title
    run_rec.provenance = "live_github"
    store.save_run(run_rec)
    return run_rec

