"""Paired component-level mutation pilot. Run from the bug-fixer root."""
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

CASES = [
    ("mahmoud/boltons", "d31d821d56c8f7756af9c30a864077f2e5855321", "boltons/mathutils.py",
     "tests/test_mathutils.py::test_clamp_examples", "return min(max(x, lower), upper)",
     "return max(min(x, lower), upper)", "Clamping a value outside the bounds gives the wrong result."),
    ("more-itertools/more-itertools", "08dca284ba5446a026afdc34067b658f9da989eb",
     "more_itertools/recipes.py", "tests/test_recipes.py::TakeTests::test_simple_take",
     "return list(islice(iterable, n))", "return list(islice(iterable, n + 1))",
     "Taking five elements returns an extra item."),
]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


FRESH_CASES = [
    (CASES[0][0], CASES[0][1], CASES[0][2], "tests/test_mathutils.py::test_ceil_basic",
     "return options[i]", "return options[i - 1]", "Ceiling selects the previous permitted value."),
    (CASES[1][0], CASES[1][1], CASES[1][2], "tests/test_recipes.py::NthTests::test_basic",
     "return next(islice(iterable, n, None), default)", "return next(islice(iterable, n + 1, None), default)",
     "Selecting the nth item skips one too many elements."),
    (CASES[1][0], CASES[1][1], CASES[1][2], "tests/test_recipes.py::TailTests::test_iterator_greater",
     "return iter(deque(iterable, maxlen=n))", "return iter(deque(iterable, maxlen=n + 1))",
     "The tail of an iterator includes an extra item."),
]


def main():
    root = Settings.from_env().data_dir
    out = root / "repo-pilot" / str(time.time_ns())
    out.mkdir(parents=True)
    settings = replace(Settings.from_env(), data_dir=out / "runs-data", max_repair_attempts=2)
    rows = []
    cases = FRESH_CASES if sys.argv[1:] == ["--fresh"] else CASES
    for index, (slug, sha, target, node, original, mutant, issue) in enumerate(cases):
        repo = root / "repo-pilot" / slug.split("/")[1]
        if not (repo / ".git").exists():
            subprocess.run(["git", "clone", "https://github.com/" + slug + ".git", str(repo)], check=True)
        source = out / f"{index}-{repo.name}"
        package = target.split("/")[0]
        test_file = node.split("::")[0]
        paths = git(repo, "ls-tree", "-r", "--name-only", sha, package).decode().splitlines()
        for path in [p for p in paths if p.endswith(".py")] + [test_file]:
            dest = source / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(git(repo, "show", f"{sha}:{path}"))
        clean = validate_workspace(source, runner="docker", image=settings.validator_image)
        (source.parent / f"{source.name}-clean.json").write_text(clean.model_dump_json(indent=2))
        if not clean.passed:
            raise RuntimeError(f"Upstream component failed validation: {clean.issues}")
        path = source / target
        code = path.read_text(encoding="utf-8")
        assert code.count(original) == 1
        path.write_text(code.replace(original, mutant), encoding="utf-8")
        for retrieval in ([False, True] if index == 0 else [True, False]):
            mode = nullcontext() if retrieval else patch("bug_fixer.state_machine.collect_evidence_excerpts", return_value=[])
            start = time.monotonic()
            with mode:
                run = BugFixerWorkflow(settings).execute_fix(
                    f"https://github.com/{slug}/issues/1", source_repo=source,
                    test_nodeid=node, allowed_paths=[target], runner_override="docker",
                    mock_issue_data={"title": issue, "body": issue})
            row = dict(repository=slug, upstream=sha, reproducer=node, retrieval=retrieval,
                       run_id=run.run_id, status=run.status, attempts=run.attempts_used,
                       seconds=round(time.monotonic()-start, 2))
            rows.append(row)
            (out / "results.json").write_text(json.dumps(rows, indent=2))
            print(row, flush=True)
    print(out / "results.json")


if __name__ == "__main__":
    main()
