"""Regression tests for issue provenance: local rehearsal vs verified live GitHub issue."""

import json
import pytest
from pathlib import Path
import tempfile
from unittest.mock import patch, MagicMock

from bug_fixer.models import RunRecord, RunState, ValidationSummary, Diagnosis, PromptExcerpt
from bug_fixer.db import EvidenceStore
from bug_fixer.reporting import generate_pr_body_markdown, generate_report_dict
from bug_fixer.publication import create_draft_pr
from bug_fixer.intake import IntakeError, associate_live_issue


def _make_dummy_run(provenance: str = "fixture", issue_number: int = 3, issue_title: str = "AP002 incorrectly flags immutable empty-tuple defaults") -> RunRecord:
    return RunRecord(
        run_id="run_testprov123",
        issue_url=f"https://github.com/AnanthKumar1810/ml-code-review-assistant/issues/{issue_number}",
        issue_number=issue_number,
        issue_title=issue_title,
        repo_owner="AnanthKumar1810",
        repo_name="ml-code-review-assistant",
        base_sha="53b302526d6235c30929eca54243eadc569cf73c",
        stage=RunState.READY_FOR_REVIEW,
        status="ready_for_review",
        reproducer_nodeid="tests/test_project4.py::test_ap002_immutable_tuple_default",
        reproducer_hash="2568417d7c59441581de63a42636493e551720369e664cce271fbed08c886ebc",
        provenance=provenance,
        created_at="2026-11-11T20:00:00Z",
        updated_at="2026-11-11T20:00:00Z",
    )


def test_local_rehearsal_pr_title_and_body_does_not_claim_fake_github_issue():
    """A locally supplied issue rehearsal must NOT generate a PR title or body claiming a verified GitHub issue #."""
    run = _make_dummy_run(provenance="fixture", issue_number=3, issue_title="AP002 incorrectly flags immutable empty-tuple defaults")
    base_val = ValidationSummary(passed=False, reproducer_outcome="failed", failing_assertions=["assert False"])
    cand_val = ValidationSummary(passed=True, reproducer_outcome="passed")
    diagnosis = Diagnosis(suspected_cause="Tuple default flagged", next_check_or_edit="Fix regex", expected_observable_result="Pass")

    # Generate PR body
    body = generate_pr_body_markdown(
        run=run,
        base_val=base_val,
        cand_val=cand_val,
        prompt_excerpts=[],
        patch_text="diff --git a/src/patterns.py...",
        changed_files=["src/patterns.py"],
        diagnosis=diagnosis,
        reviewer=None,
    )

    # The body must explicitly indicate local rehearsal and NOT claim 'for issue #3' as a live GitHub issue
    assert "for issue #3" not in body, "PR body must not claim 'for issue #3' when issue provenance is local/fixture"
    assert "Local" in body or "local" in body, "PR body must indicate local rehearsal provenance"

    # Also verify structured report dict
    rep_dict = generate_report_dict(
        run=run,
        base_val=base_val,
        cand_val=cand_val,
        prompt_excerpts=[],
        patch_text="diff --git a/src/patterns.py...",
        changed_files=["src/patterns.py"],
        diagnosis=diagnosis,
        tool_calls=[],
        reviewer=None,
        eligible_for_publication=True,
        reasons_ineligible=[],
    )
    assert rep_dict["issue"]["verified_on_github"] is False, "Local rehearsal issue must not be marked verified_on_github"
    assert rep_dict["issue"]["number"] is None, "Issue number must be None in report dict for local rehearsal"


def test_live_github_issue_retains_real_number_and_title():
    """A live GitHub issue verified via API must retain its real number, title, and verified provenance."""
    run = _make_dummy_run(provenance="live_github", issue_number=42, issue_title="AP002 false positive on tuple")
    base_val = ValidationSummary(passed=False, reproducer_outcome="failed", failing_assertions=["assert False"])
    cand_val = ValidationSummary(passed=True, reproducer_outcome="passed")
    diagnosis = Diagnosis(suspected_cause="Defect", next_check_or_edit="Fix", expected_observable_result="Pass")

    body = generate_pr_body_markdown(
        run=run,
        base_val=base_val,
        cand_val=cand_val,
        prompt_excerpts=[],
        patch_text="diff --git a/src/patterns.py...",
        changed_files=["src/patterns.py"],
        diagnosis=diagnosis,
        reviewer=None,
    )

    assert "for issue #42" in body
    assert "https://github.com/AnanthKumar1810/ml-code-review-assistant/issues/42" in body

    rep_dict = generate_report_dict(
        run=run,
        base_val=base_val,
        cand_val=cand_val,
        prompt_excerpts=[],
        patch_text="diff --git a/src/patterns.py...",
        changed_files=["src/patterns.py"],
        diagnosis=diagnosis,
        tool_calls=[],
        reviewer=None,
        eligible_for_publication=True,
        reasons_ineligible=[],
    )
    assert rep_dict["issue"]["verified_on_github"] is True
    assert rep_dict["issue"]["number"] == 42


def test_associate_live_issue_rejects_mismatched_repository():
    """Associating an issue from a different repository must be rejected with IntakeError."""
    import gc
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        art_dir = Path(tmp_dir) / "artifacts"
        store = EvidenceStore(db_path, art_dir)

        run = _make_dummy_run(provenance="fixture")
        store.save_run(run)

        # Attempt to associate issue from psf/requests into ml-code-review-assistant run
        with pytest.raises(IntakeError) as exc_info:
            associate_live_issue(
                run_rec=run,
                live_issue_url="https://github.com/psf/requests/issues/100",
                store=store,
            )
        assert "Repository mismatch" in str(exc_info.value)
        # Provenance must remain untouched
        persisted = store.get_run(run.run_id)
        assert persisted.provenance == "fixture"
        del store, persisted
        gc.collect()


def test_associate_live_issue_tracks_audit_event_and_updates_provenance():
    """Associating a verified live issue records an audit event and updates provenance without silent relabeling."""
    import gc
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        art_dir = Path(tmp_dir) / "artifacts"
        store = EvidenceStore(db_path, art_dir)

        run = _make_dummy_run(provenance="fixture", issue_number=3)
        store.save_run(run)

        # Mock GitHub API response for real issue #77
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "html_url": "https://github.com/AnanthKumar1810/ml-code-review-assistant/issues/77",
            "number": 77,
            "title": "AP002 incorrectly flags immutable empty-tuple defaults",
            "body": "Detailed reproduction...",
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            updated_run = associate_live_issue(
                run_rec=run,
                live_issue_url="https://github.com/AnanthKumar1810/ml-code-review-assistant/issues/77",
                store=store,
            )

        assert updated_run.provenance == "live_github"
        assert updated_run.origin_provenance == "fixture"
        assert updated_run.issue_number == 77
        assert updated_run.issue_title == "AP002 incorrectly flags immutable empty-tuple defaults"

        # Verify DB persistence retains origin_provenance
        reloaded = store.get_run(run.run_id)
        assert reloaded.provenance == "live_github"
        assert reloaded.origin_provenance == "fixture"

        # Verify audit event in DB
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT event_type, data_json FROM events WHERE run_id = ? AND event_type = 'issue_associated'", (run.run_id,)).fetchone()
        assert row is not None, "issue_associated audit event must be recorded"
        data = json.loads(row["data_json"])
        assert data["previous_provenance"] == "fixture"
        assert data["live_issue_number"] == 77
        conn.close()
        del store
        gc.collect()
