"""Test reproduction gate: distinguishable outcomes for good base, missing dep, timeout, matching failure."""

import time
from pathlib import Path
import pytest

from bug_fixer.config import Settings
from bug_fixer.docker_runner import validate_workspace
from bug_fixer.models import RunState
from bug_fixer.state_machine import BugFixerWorkflow


def test_matching_failure_unlocks_repairing(cross_file_fixture: Path, test_settings: Settings):
    """A matching failure transitions to REPAIRING (or VERIFIED/REPAIR_FAILED after attempting)."""
    workflow = BugFixerWorkflow(test_settings)
    issue_data = {
        "title": "Greeting returns Goodbye",
        "body": "Running main.py should return Hello, World!",
        "reproducer_test": "tests/test_greeting.py::test_greeting",
    }
    # Mock LLM that doesn't edit to observe reproduction state in DB
    mock_llm = {
        "content": "SUSPECTED_CAUSE: Defect in service\nSUPPORTING_EVIDENCE_IDS: service/formatter.py:3\nNEXT_CHECK_OR_EDIT: check\nEXPECTED_OBSERVABLE_RESULT: pass",
        "tool_calls": [],
    }

    run_rec = workflow.execute_fix(
        "https://github.com/acme/project/issues/42",
        source_repo=cross_file_fixture,
        test_nodeid="tests/test_greeting.py::test_greeting",
        mock_issue_data=issue_data,
        mock_llm_response=mock_llm,
        runner_override="local",
    )

    # Check that defect_reproduced event was recorded
    events = workflow.store.list_events(run_rec.run_id)
    reproduced_evt = next((e for e in events if e.event_type == "defect_reproduced"), None)
    assert reproduced_evt is not None
    assert reproduced_evt.stage == RunState.REPRODUCED.value
    assert reproduced_evt.data["reproducer_nodeid"] == "tests/test_greeting.py::test_greeting"


def test_known_good_base_stops_as_not_reproduced(tmp_path: Path, test_settings: Settings):
    """When the test already passes on base, state transitions to NOT_REPRODUCED and edits are blocked."""
    repo = tmp_path / "good_repo"
    repo.mkdir()
    (repo / "test_ok.py").write_text("def test_always_pass(): assert True\n")

    workflow = BugFixerWorkflow(test_settings)
    issue_data = {
        "title": "Bogus bug report",
        "body": "Nothing is broken",
        "reproducer_test": "test_ok.py::test_always_pass",
    }

    run_rec = workflow.execute_fix(
        "https://github.com/acme/project/issues/100",
        source_repo=repo,
        test_nodeid="test_ok.py::test_always_pass",
        mock_issue_data=issue_data,
        runner_override="local",
    )

    assert run_rec.stage == RunState.NOT_REPRODUCED
    assert run_rec.status == "not_reproduced"
    # Verify no repair events or tool calls were executed
    events = workflow.store.list_events(run_rec.run_id)
    assert not any(e.event_type == "hypothesis_and_edit" for e in events)


def test_missing_dependency_produces_environment_error(tmp_path: Path, test_settings: Settings):
    """Collection failure due to missing dependency produces ENVIRONMENT_ERROR, not defect reproduction."""
    repo = tmp_path / "broken_dep_repo"
    repo.mkdir()
    (repo / "test_broken.py").write_text("import non_existent_package_xyz\ndef test_xyz(): pass\n")

    workflow = BugFixerWorkflow(test_settings)
    issue_data = {
        "title": "Missing dep bug",
        "body": "Fails to import",
        "reproducer_test": "test_broken.py::test_xyz",
    }

    run_rec = workflow.execute_fix(
        "https://github.com/acme/project/issues/101",
        source_repo=repo,
        test_nodeid="test_broken.py::test_xyz",
        mock_issue_data=issue_data,
        runner_override="local",
    )

    assert run_rec.stage == RunState.ENVIRONMENT_ERROR
    assert run_rec.status == "environment_error"
    events = workflow.store.list_events(run_rec.run_id)
    assert not any(e.event_type == "defect_reproduced" for e in events)


def test_timeout_produces_environment_error(tmp_path: Path, test_settings: Settings):
    """Validation timing out produces ENVIRONMENT_ERROR, blocking edits."""
    repo = tmp_path / "hang_repo"
    repo.mkdir()
    (repo / "test_hang.py").write_text("import time\ndef test_hang(): time.sleep(10)\n")

    summary = validate_workspace(repo, timeout_seconds=1, runner="local")
    assert summary.reproducer_outcome == "timeout"
    assert summary.status == "timeout"
    assert not summary.passed
