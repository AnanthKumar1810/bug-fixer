"""End-to-end tests exercising full CLI and Docker validation."""

import json
from pathlib import Path
import pytest

from bug_fixer.config import Settings
from bug_fixer.models import RunState
from bug_fixer.state_machine import BugFixerWorkflow


def test_e2e_controlled_workflow_with_docker_validation(cross_file_fixture: Path, test_settings: Settings):
    """End-to-end workflow executing in real Docker validator."""
    # Ensure settings use docker runner
    settings = Settings(
        model=test_settings.model,
        ollama_base_url=test_settings.ollama_base_url,
        validator_image="bugfixer-validator:py313",
        data_dir=test_settings.data_dir,
        max_repair_attempts=2,
        validation_timeout_seconds=60,
        runner="docker",  # Real Docker validator!
    )
    workflow = BugFixerWorkflow(settings)

    issue_data = {
        "title": "Greeting prefix incorrect",
        "body": "When running main.py, greeting says Goodbye instead of Hello",
        "reproducer_test": "tests/test_greeting.py::test_greeting",
    }

    # Controllable mock tool call that accurately fixes the defect in callee
    mock_repair = {
        "content": (
            "SUSPECTED_CAUSE: Defect in service/formatter.py where Goodbye is returned instead of Hello.\n"
            "SUPPORTING_EVIDENCE_IDS: service/formatter.py:3\n"
            "NEXT_CHECK_OR_EDIT: replace_text in service/formatter.py\n"
            "EXPECTED_OBSERVABLE_RESULT: tests/test_greeting.py passes with Hello prefix\n"
        ),
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
        "https://github.com/acme/project/issues/1",
        source_repo=cross_file_fixture,
        test_nodeid="tests/test_greeting.py::test_greeting",
        mock_issue_data=issue_data,
        mock_llm_response=mock_repair,
        runner_override="docker",
    )

    # 1. Check final stage
    assert run.stage == RunState.READY_FOR_REVIEW
    assert run.status == "ready_for_review"

    # 2. Check artifacts exist
    art_dir = settings.data_dir / "artifacts" / run.run_id
    assert (art_dir / "report.json").is_file()
    assert (art_dir / "pr-body.md").is_file()
    assert (art_dir / "candidate.patch").is_file()

    # 3. Check report answers the 5 audit questions
    rep_dict = json.loads((art_dir / "report.json").read_text(encoding="utf-8"))
    answers = rep_dict["review_answers"]
    assert "1_what_failed_on_original_commit" in answers
    assert "2_what_evidence_did_qwen_receive" in answers
    assert "3_what_did_it_actually_edit" in answers
    assert "4_which_tests_passed_on_exact_patch" in answers
    assert "5_who_must_review_before_merge" in answers

    # Verify that candidate reproducer passed
    assert answers["4_which_tests_passed_on_exact_patch"]["candidate_reproducer_passed"] is True
    assert answers["4_which_tests_passed_on_exact_patch"]["candidate_suite_passed"] is True
