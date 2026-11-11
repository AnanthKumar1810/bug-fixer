"""Test persistence, SQLite checkpointing, and safe resumption."""

from pathlib import Path
import pytest

from bug_fixer.config import Settings
from bug_fixer.models import RunState
from bug_fixer.state_machine import BugFixerWorkflow, StateMachineError


def test_restart_recovers_base_sha_and_failing_test(cross_file_fixture: Path, test_settings: Settings):
    """Resume recovers exact base SHA, failing test nodeid, and previous event records."""
    workflow = BugFixerWorkflow(test_settings)
    issue_data = {
        "title": "Greeting wrong",
        "body": "Fix greeting",
        "reproducer_test": "tests/test_greeting.py::test_greeting",
    }
    # Mock LLM that fails on attempt 1
    mock_bad = {
        "content": "SUSPECTED_CAUSE: Typo\nSUPPORTING_EVIDENCE_IDS: service/formatter.py:3\nNEXT_CHECK_OR_EDIT: edit\nEXPECTED_OBSERVABLE_RESULT: pass",
        "tool_calls": [
            {
                "name": "replace_text",
                "arguments": {
                    "relative_path": "service/formatter.py",
                    "old": "Goodbye",
                    "new": "Hi",  # Still wrong
                },
            }
        ],
    }

    # Run attempt 1 with retry_budget=2
    run_1 = workflow.execute_fix(
        "https://github.com/acme/project/issues/77",
        source_repo=cross_file_fixture,
        test_nodeid="tests/test_greeting.py::test_greeting",
        mock_issue_data=issue_data,
        mock_llm_response=mock_bad,
        runner_override="local",
    )
    original_run_id = run_1.run_id
    original_base_sha = run_1.base_sha

    # Verify run record in DB
    stored_run = workflow.store.get_run(original_run_id)
    assert stored_run is not None
    assert stored_run.base_sha == original_base_sha
    assert stored_run.reproducer_nodeid == "tests/test_greeting.py::test_greeting"

    # Now create a fresh workflow instance (simulating restart)
    new_workflow = BugFixerWorkflow(test_settings)
    events = new_workflow.store.list_events(original_run_id)
    assert len(events) > 0

    event_types = [e.event_type for e in events]
    assert "defect_reproduced" in event_types


def test_resume_preserves_retry_budget_and_does_not_reset(cross_file_fixture: Path, test_settings: Settings):
    """Resuming must NOT reset attempts_used to 0."""
    workflow = BugFixerWorkflow(test_settings)
    issue_data = {
        "title": "Greeting issue",
        "body": "Need Hello",
        "reproducer_test": "tests/test_greeting.py::test_greeting",
    }
    # Mock LLM that fixes it
    mock_good = {
        "content": "SUSPECTED_CAUSE: Wrong prefix\nSUPPORTING_EVIDENCE_IDS: service/formatter.py:3\nNEXT_CHECK_OR_EDIT: edit\nEXPECTED_OBSERVABLE_RESULT: pass",
        "tool_calls": [
            {
                "name": "replace_text",
                "arguments": {
                    "relative_path": "service/formatter.py",
                    "old": "Goodbye",
                    "new": "Hello",
                },
            }
        ],
    }

    run = workflow.execute_fix(
        "https://github.com/acme/project/issues/88",
        source_repo=cross_file_fixture,
        test_nodeid="tests/test_greeting.py::test_greeting",
        mock_issue_data=issue_data,
        mock_llm_response=mock_good,
        runner_override="local",
    )

    # Completed run was verified
    assert run.stage in (RunState.VERIFIED, RunState.READY_FOR_REVIEW)
    assert run.attempts_used == 1

    # Resume on verified run returns same status without re-running
    new_workflow = BugFixerWorkflow(test_settings)
    resumed = new_workflow.resume_run(run.run_id, source_repo=cross_file_fixture)
    assert resumed.attempts_used == 1
    assert resumed.stage in (RunState.VERIFIED, RunState.READY_FOR_REVIEW)


def test_resume_detects_workspace_tampering(cross_file_fixture: Path, test_settings: Settings):
    """If workspace files are tampered with externally, resume raises mismatch error."""
    workflow = BugFixerWorkflow(test_settings)
    mock_llm = {
        "content": "SUSPECTED_CAUSE: none\nSUPPORTING_EVIDENCE_IDS: a\nNEXT_CHECK_OR_EDIT: b\nEXPECTED_OBSERVABLE_RESULT: c",
        "tool_calls": [],
    }

    run = workflow.execute_fix(
        "https://github.com/acme/project/issues/99",
        source_repo=cross_file_fixture,
        test_nodeid="tests/test_greeting.py::test_greeting",
        mock_issue_data={"title": "test", "body": "test"},
        mock_llm_response=mock_llm,
        runner_override="local",
    )

    # Tamper with workspace
    ws_file = test_settings.data_dir / "runs" / run.run_id / "workspace" / "main.py"
    ws_file.write_text("# tampered comment\n")

    new_workflow = BugFixerWorkflow(test_settings)
    with pytest.raises(StateMachineError, match="Workspace mismatch"):
        new_workflow.resume_run(run.run_id, source_repo=cross_file_fixture)
