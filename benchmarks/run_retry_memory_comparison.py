"""Controlled empirical evaluation: Previous release (no retry memory) vs Current release (with retry memory)."""

import json
import os
import shutil
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bug_fixer.config import Settings
from bug_fixer.state_machine import BugFixerWorkflow
from bug_fixer.verification import verify_run
from bug_fixer.db import EvidenceStore

CASES = [
    {
        "case_id": "case_template_filter",
        "name": "Template Filter (Cross-file Missing Filter Alias)",
        "fixture_dir": "fixtures/eval_retry_memory/template_filter_repo",
        "issue_url": "https://github.com/local/template-engine/issues/1",
        "issue_title": "render fails when uppercase filter is used in template",
        "issue_body": "Calling render with {{ name | uppercase }} raises ValueError: Unknown filter: uppercase",
        "test_nodeid": "tests/test_templates.py::test_render_uppercase",
        "allowed_paths": ["templates/engine.py", "templates/filters.py"],
    },
    {
        "case_id": "case_counter_service",
        "name": "Counter Service (Cross-file Storage Capacity Limit)",
        "fixture_dir": "fixtures/eval_retry_memory/counter_service_repo",
        "issue_url": "https://github.com/local/counter-service/issues/1",
        "issue_title": "increment_counter raises OverflowError on amounts over 50",
        "issue_body": "increment_counter(100) fails with OverflowError because storage MAX_CAPACITY is too low",
        "test_nodeid": "tests/test_counter.py::test_increment_large",
        "allowed_paths": ["service/counter.py", "service/storage.py"],
    },
]


def run_single_case(case_info: dict, config_label: str, target_dir: Path) -> dict:
    is_retry_memory = (config_label == "C")
    root_dir = Path(__file__).resolve().parent.parent
    src_fixture = root_dir / case_info["fixture_dir"]

    # Create fresh isolated copy of fixture
    run_ws = target_dir / f"{case_info['case_id']}_{config_label}"
    if run_ws.exists():
        shutil.rmtree(run_ws)
    shutil.copytree(src_fixture, run_ws)

    runs_data = target_dir / f"runs_data_{case_info['case_id']}_{config_label}"
    runs_data.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings.from_env(), data_dir=runs_data, max_repair_attempts=2)

    # In Config B (no retry memory), mock build_compact_repair_packet to drop earlier_attempts
    if not is_retry_memory:
        from bug_fixer import retrieval
        orig_build = retrieval.build_compact_repair_packet
        def mock_build(issue, rep_node, val, excs, attempt, earlier_attempts=None):
            return orig_build(issue, rep_node, val, excs, attempt, earlier_attempts=None)
        mode = patch("bug_fixer.state_machine.build_compact_repair_packet", side_effect=mock_build)
    else:
        mode = nullcontext()

    start_time = time.monotonic()
    with mode:
        workflow = BugFixerWorkflow(settings)
        run_record = workflow.execute_fix(
            case_info["issue_url"],
            source_repo=run_ws,
            test_nodeid=case_info["test_nodeid"],
            allowed_paths=case_info["allowed_paths"],
            runner_override="docker",
            mock_issue_data={"title": case_info["issue_title"], "body": case_info["issue_body"]},
        )
    elapsed = round(time.monotonic() - start_time, 2)

    # Read artifacts to record: first failure, evidence carried forward, second action
    art_dir = runs_data / "artifacts" / run_record.run_id
    rep_file = art_dir / "report.json"
    rep_data = json.loads(rep_file.read_text(encoding="utf-8")) if rep_file.is_file() else {}

    # Check attempt 1 and 2
    cand1_file = art_dir / "cand_val_attempt_1.json"
    cand1 = json.loads(cand1_file.read_text(encoding="utf-8")) if cand1_file.is_file() else None
    
    cand2_file = art_dir / "cand_val_attempt_2.json"
    cand2 = json.loads(cand2_file.read_text(encoding="utf-8")) if cand2_file.is_file() else None

    # Verify if ready for review
    verified_tree = None
    independently_verified = False
    if run_record.stage.value in ("ready_for_review", "verified"):
        store = EvidenceStore(runs_data / "bug_fixer.db", art_dir.parent)
        try:
            ok, vrep = verify_run(run_record.run_id, store, settings, source_repo=run_ws, runner="docker")
            independently_verified = ok
            verified_tree = vrep.get("verified_tree_id")
        except Exception:
            pass

    return {
        "case_id": case_info["case_id"],
        "name": case_info["name"],
        "config": config_label,
        "retry_memory_enabled": is_retry_memory,
        "run_id": run_record.run_id,
        "status": "verified" if independently_verified else run_record.status,
        "attempts_used": run_record.attempts_used,
        "reached_attempt_2": (run_record.attempts_used >= 2),
        "attempt_1_passed": bool(cand1 and cand1.get("passed")),
        "attempt_1_failure": cand1.get("issues", []) if cand1 else None,
        "attempt_2_passed": bool(cand2 and cand2.get("passed")) if cand2 else None,
        "attempt_2_failure": cand2.get("issues", []) if cand2 else None,
        "verified_tree_id": verified_tree,
        "independently_verified": independently_verified,
        "seconds": elapsed,
    }


def main():
    root = Settings.from_env().data_dir
    out_dir = root / "retry_memory_evaluation" / str(int(time.time()))
    out_dir.mkdir(parents=True, exist_ok=True)
    results_file = out_dir / "results.json"

    results = []
    print("================================================================================")
    print("STARTING CONTROLLED RETRY MEMORY EVALUATION: Config B vs Config C")
    print(f"Target directory: {out_dir}")
    print("================================================================================")

    # Strictly alternate execution order: B then C, then C then B
    pairs = [
        (CASES[0], "B"),
        (CASES[0], "C"),
        (CASES[1], "C"),
        (CASES[1], "B"),
    ]

    for case_info, config in pairs:
        print(f"\n>>> Running {case_info['case_id']} under Config {config} (retry_memory={config == 'C'})...")
        res = run_single_case(case_info, config, out_dir)
        results.append(res)
        results_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"Finished {case_info['case_id']} [Config {config}]: {res['status']} in {res['attempts_used']} attempts ({res['seconds']}s)")

    print("\n================ FINAL EVALUATION RESULTS ================")
    print(results_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
