"""Complete paired pilot runner for fresh cases."""
import json
import subprocess
import sys
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bug_fixer.config import Settings
from bug_fixer.docker_runner import validate_workspace
from bug_fixer.state_machine import BugFixerWorkflow
from benchmarks.repository_pilot import git, FRESH_CASES


def main():
    root = Settings.from_env().data_dir
    target_dir = root / "repo-pilot" / "1794406406642797100"
    target_dir.mkdir(parents=True, exist_ok=True)
    runs_data = target_dir / "runs-data"
    settings = replace(Settings.from_env(), data_dir=runs_data, max_repair_attempts=2)

    results_file = target_dir / "results.json"
    rows = []
    if results_file.exists():
        try:
            rows = json.loads(results_file.read_text(encoding="utf-8"))
        except Exception:
            rows = []

    # Map of (case_index, retrieval) that already succeeded
    done_set = {(r["reproducer"], r["retrieval"]) for r in rows if r.get("status") in ("ready_for_review", "verified", "repair_failed")}
    print(f"Loaded {len(rows)} existing rows from {results_file}. Done set: {done_set}")

    for index, (slug, sha, target, node, original, mutant, issue) in enumerate(FRESH_CASES):
        repo = root / "repo-pilot" / slug.split("/")[1]
        if not (repo / ".git").exists():
            subprocess.run(["git", "clone", "https://github.com/" + slug + ".git", str(repo)], check=True)
        source = target_dir / f"{index}-{repo.name}"
        package = target.split("/")[0]
        test_file = node.split("::")[0]

        if not source.exists():
            paths = git(repo, "ls-tree", "-r", "--name-only", sha, package).decode().splitlines()
            for path in [p for p in paths if p.endswith(".py")] + [test_file]:
                dest = source / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(git(repo, "show", f"{sha}:{path}"))

            clean = validate_workspace(source, runner="docker", image=settings.validator_image)
            (target_dir / f"{source.name}-clean.json").write_text(clean.model_dump_json(indent=2))
            if not clean.passed:
                raise RuntimeError(f"Upstream component failed validation: {clean.issues}")

            path = source / target
            code = path.read_text(encoding="utf-8")
            assert code.count(original) == 1, f"Expected 1 occurrence of original in {path}, got {code.count(original)}"
            path.write_text(code.replace(original, mutant), encoding="utf-8")

        for retrieval in ([False, True] if index == 0 else [True, False]):
            if (node, retrieval) in done_set:
                print(f"Skipping already-completed case {node} (retrieval={retrieval})")
                continue

            print(f"\n>>> Running case {index} ({slug}): {node} [retrieval={retrieval}]...")
            mode = nullcontext() if retrieval else patch("bug_fixer.state_machine.collect_evidence_excerpts", return_value=[])
            start = time.monotonic()
            with mode:
                run = BugFixerWorkflow(settings).execute_fix(
                    f"https://github.com/{slug}/issues/1",
                    source_repo=source,
                    test_nodeid=node,
                    allowed_paths=[target],
                    runner_override="docker",
                    mock_issue_data={"title": issue, "body": issue},
                )
            dur = round(time.monotonic() - start, 2)
            row = dict(
                repository=slug,
                upstream=sha,
                reproducer=node,
                retrieval=retrieval,
                run_id=run.run_id,
                status=run.status,
                attempts=run.attempts_used,
                seconds=dur,
            )
            rows.append(row)
            results_file.write_text(json.dumps(rows, indent=2), encoding="utf-8")
            print(f"Done: {row}", flush=True)

    print("\nAll cases complete. Final results:")
    print(results_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
