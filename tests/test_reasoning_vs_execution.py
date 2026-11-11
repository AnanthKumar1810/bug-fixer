"""Test separation of pure reasoning from tool execution."""

from pathlib import Path
import pytest

from bug_fixer.config import Settings
from bug_fixer.llm_adapter import call_qwen_repair
from bug_fixer.models import RunState
from bug_fixer.state_machine import BugFixerWorkflow
from bug_fixer.workspace import Workspace


def test_pure_reasoning_without_tool_call_produces_no_change(tmp_path: Path):
    """When the model outputs prose claiming a fix without calling a tool, workspace is unchanged."""
    file_p = tmp_path / "calc.py"
    file_p.write_text("def add(a, b): return a - b\n")
    ws = Workspace(tmp_path)

    prose_response = {
        "content": (
            "SUSPECTED_CAUSE: The minus operator is used instead of plus.\n"
            "SUPPORTING_EVIDENCE_IDS: calc.py:1\n"
            "NEXT_CHECK_OR_EDIT: I have fixed it by changing - to + in calc.py.\n"
            "EXPECTED_OBSERVABLE_RESULT: add(1, 2) == 3\n"
        ),
        "tool_calls": [],
    }

    diag, raw, executed = call_qwen_repair("fix this", ws, mock_response=prose_response)
    assert diag.suspected_cause
    assert len(executed) == 1
    assert executed[0]["status"] == "no_change"
    assert ws.changed_files == []
    assert file_p.read_text() == "def add(a, b): return a - b\n"


def test_real_edit_with_failing_test_records_contradicted_hypothesis(cross_file_fixture: Path, test_settings: Settings):
    """A real edit that fails records the failure in event history and proceeds to next attempt."""
    workflow = BugFixerWorkflow(test_settings)
    # Model makes wrong edit on attempt 1, then correct edit on attempt 2
    mock_bad_then_good = {
        "content": (
            "SUSPECTED_CAUSE: Wrong prefix\n"
            "SUPPORTING_EVIDENCE_IDS: service/formatter.py:3\n"
            "NEXT_CHECK_OR_EDIT: replace_text\n"
            "EXPECTED_OBSERVABLE_RESULT: pass"
        ),
        "tool_calls": [
            {
                "name": "replace_text",
                "arguments": {
                    "relative_path": "service/formatter.py",
                    "old": "Goodbye",
                    "new": "Hey",  # Still wrong! Should be 'Hello'
                },
            }
        ],
    }

    run_rec = workflow.execute_fix(
        "https://github.com/acme/project/issues/12",
        source_repo=cross_file_fixture,
        test_nodeid="tests/test_greeting.py::test_greeting",
        mock_issue_data={"title": "Greeting wrong", "body": "fix prefix"},
        mock_llm_response=mock_bad_then_good,
        runner_override="local",
    )

    # Both attempts used the bad edit -> ended in repair_failed
    assert run_rec.stage == RunState.REPAIR_FAILED
    assert run_rec.attempts_used == 2

    # Check that failed attempts were recorded
    events = workflow.store.list_events(run_rec.run_id)
    failed_evts = [e for e in events if e.event_type == "repair_attempt_failed"]
    assert len(failed_evts) >= 1
    assert "failing_assertions" in failed_evts[0].data
