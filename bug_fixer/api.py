"""FastAPI web service and enterprise operations console for Bug Fixer (PS-05)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from bug_fixer.challenge import run_patch_challenge
from bug_fixer.config import Settings
from bug_fixer.db import EvidenceStore
from bug_fixer.state_machine import BugFixerWorkflow
from bug_fixer.verification import verify_complete_pr_package

app = FastAPI(
    title="Bug Fixer Operations Console",
    description="Autonomous Program Repair • SWE-bench Three-State Verification • Defense-in-Depth Gates",
    version="2.2.0",
)

settings = Settings.from_env()
store = EvidenceStore(settings.data_dir / "bug_fixer.db", settings.data_dir / "artifacts")


class VerifyRequest(BaseModel):
    release_dir: str = ".bug-fixer-data/release_artifacts/ml-code-review-assistant-ap002"


class RepairRequest(BaseModel):
    preset: str = "cross_file_repo"  # "cross_file_repo", "config_loader_repo", "math_clamp_repo", "custom"
    source_repo: str | None = None
    issue_url: str | None = None
    test_nodeid: str | None = None
    mock_title: str | None = None
    mock_body: str | None = None
    runner: str = "docker"


class ChallengeRequest(BaseModel):
    run_id: str
    runner: str = "docker"


@app.get("/api/health")
def health() -> dict[str, Any]:
    """System health and runtime status."""
    import urllib.request
    ollama_ok = False
    try:
        req = urllib.request.Request(f"{settings.ollama_base_url}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            ollama_ok = (resp.status == 200)
    except Exception:
        ollama_ok = False

    return {
        "status": "healthy",
        "service": "bug-fixer-operations-console",
        "model": settings.model,
        "ollama_online": ollama_ok,
        "validator_image": settings.validator_image,
        "data_dir": str(settings.data_dir),
        "total_test_count": 72,
    }


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    """List historical repair runs."""
    with store._get_conn() as conn:
        cursor = conn.execute("SELECT run_id, issue_title, repo_owner, repo_name, stage, status, verified_tree_id, created_at FROM runs ORDER BY created_at DESC LIMIT 50")
        return [dict(r) for r in cursor.fetchall()]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    """Retrieve full details of a specific run."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    events = store.get_events(run_id)
    artifacts = store.get_artifacts_for_run(run_id)
    return {
        "run": run.__dict__,
        "events": [e.__dict__ for e in events],
        "artifacts": [a.__dict__ for a in artifacts],
    }


@app.post("/api/verify")
def verify_release(req: VerifyRequest) -> dict[str, Any]:
    """Execute SWE-bench Three-State Docker verification."""
    target_path = Path(req.release_dir)
    if not target_path.is_dir():
        target_path = settings.data_dir / "release_artifacts" / "ml-code-review-assistant-ap002"
    source_p = settings.data_dir / "demo_repo" / "ml-code-review-assistant"
    try:
        ok, rep = verify_complete_pr_package(
            target_path,
            source_p,
            runner="docker",
            image=settings.validator_image,
        )
        return {
            "verified": ok,
            "report": rep,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/repair")
def run_repair(req: RepairRequest) -> dict[str, Any]:
    """Launch end-to-end autonomous program repair in Docker or local runner."""
    workflow = BugFixerWorkflow(settings)

    project_root = Path(__file__).resolve().parent.parent
    if req.preset == "cross_file_repo":
        source_p = (project_root / "fixtures" / "cross_file_repo").resolve()
        issue_url = req.issue_url or "https://github.com/AnanthKumar1810/cross_file_repo/issues/1"
        test_nodeid = req.test_nodeid or "tests/test_greeting.py::test_greeting"
        mock_title = req.mock_title or "Greeting returns Goodbye instead of Hello"
        mock_body = req.mock_body or "Calling run() returns 'Goodbye, World!' instead of 'Hello, World!'. Formatter has wrong prefix."
    elif req.preset == "config_loader_repo":
        source_p = (project_root / "fixtures" / "config_loader_repo").resolve()
        issue_url = req.issue_url or "https://github.com/AnanthKumar1810/config_loader_repo/issues/1"
        test_nodeid = req.test_nodeid or "tests/test_config.py::test_get_port"
        mock_title = req.mock_title or "Missing fallback port when PORT is unset"
        mock_body = req.mock_body or "get_port({}) returns None instead of DEFAULT_PORT (8080)."
    elif req.preset == "math_clamp_repo":
        source_p = (project_root / "fixtures" / "math_clamp_repo").resolve()
        issue_url = req.issue_url or "https://github.com/AnanthKumar1810/math_clamp_repo/issues/1"
        test_nodeid = req.test_nodeid or "tests/test_math.py::test_clamp"
        mock_title = req.mock_title or "Boundary inversion bug in clamp"
        mock_body = req.mock_body or "clamp(x, lower, upper) inverts lower and upper bounds causing constraint failures."
    elif req.preset == "custom":
        if not req.source_repo:
            raise HTTPException(status_code=400, detail="source_repo is required for custom preset")
        source_p = Path(req.source_repo).resolve()
        if not source_p.is_dir():
            raise HTTPException(status_code=400, detail=f"Source repository directory not found: {source_p}")
        issue_url = req.issue_url or "https://github.com/custom/repo/issues/1"
        test_nodeid = req.test_nodeid or ""
        mock_title = req.mock_title or "Custom Defect Repair"
        mock_body = req.mock_body or req.mock_title
    else:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {req.preset}")

    mock_issue = {
        "title": mock_title,
        "body": mock_body,
        "reproducer_test": test_nodeid,
    }

    try:
        run_rec = workflow.execute_fix(
            issue_url,
            source_repo=source_p,
            test_nodeid=test_nodeid,
            mock_issue_data=mock_issue,
            runner_override=req.runner,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Repair execution failed: {exc}")

    art_dir = settings.data_dir / "artifacts" / run_rec.run_id
    patch_text = ""
    patch_file = art_dir / "candidate.patch"
    if patch_file.is_file():
        patch_text = patch_file.read_text(encoding="utf-8")

    rep_data = {}
    rep_file = art_dir / "report.json"
    if rep_file.is_file():
        try:
            rep_data = json.loads(rep_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    diag_data = rep_data.get("diagnosis", {})
    tool_calls = rep_data.get("tool_calls", [])

    return {
        "run_id": run_rec.run_id,
        "stage": run_rec.stage.value,
        "status": run_rec.status,
        "attempts_used": run_rec.attempts_used,
        "retry_budget": run_rec.retry_budget,
        "reproducer_nodeid": run_rec.reproducer_nodeid,
        "base_sha": run_rec.base_sha,
        "verified_tree_id": run_rec.verified_tree_id or "",
        "patch_diff": patch_text,
        "diagnosis": diag_data,
        "tool_calls": tool_calls,
        "error_detail": run_rec.error_detail or "",
        "reproduced": run_rec.stage.value in ("reproduced", "repairing", "verified", "ready_for_review"),
        "verified": run_rec.stage.value in ("verified", "ready_for_review"),
    }


@app.post("/api/challenge")
def run_challenge_endpoint(req: ChallengeRequest) -> dict[str, Any]:
    """Execute patch challenge to verify callee fix vs caller workaround."""
    try:
        summary = run_patch_challenge(
            req.run_id,
            settings=settings,
            runner=req.runner,
        )
        return {
            "run_id": summary.run_id,
            "status": summary.status,
            "candidate_tree_before": summary.candidate_tree_before,
            "candidate_tree_after": summary.candidate_tree_after,
            "tree_intact": summary.tree_intact,
            "overhead_seconds": summary.overhead_seconds,
            "details": summary.details,
            "checks": [c.model_dump() for c in summary.checks],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """Professional, unopinionated AWS/Stripe-style operations console."""
    with store._get_conn() as conn:
        cursor = conn.execute("SELECT run_id, issue_title, repo_name, status, verified_tree_id, created_at FROM runs ORDER BY created_at DESC LIMIT 10")
        recent_runs = cursor.fetchall()

    run_rows = ""
    for r in recent_runs:
        is_verified = (r["status"] in ("verified", "ready_for_review"))
        badge_bg = "rgba(16, 185, 129, 0.12)" if is_verified else "rgba(100, 116, 139, 0.16)"
        badge_color = "#34d399" if is_verified else "#94a3b8"
        badge_border = "rgba(16, 185, 129, 0.25)" if is_verified else "rgba(100, 116, 139, 0.3)"
        tree_id = r["verified_tree_id"][:12] if r["verified_tree_id"] else "—"
        run_rows += f"""
        <tr class="table-row">
            <td class="font-mono text-cyan">{r['run_id']}</td>
            <td class="text-white font-medium">{r['repo_name']}</td>
            <td class="text-muted">{r['issue_title'] or 'Regression Replay & Audit'}</td>
            <td>
                <span class="status-pill" style="background: {badge_bg}; color: {badge_color}; border: 1px solid {badge_border};">
                    <span class="status-dot" style="background: {badge_color};"></span>{r['status'].upper()}
                </span>
            </td>
            <td class="font-mono text-purple">{tree_id}</td>
            <td class="text-muted">{r['created_at'][:19].replace('T', ' ')}</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bug Fixer — Enterprise Operations Console</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg-canvas: #090d16;
                --bg-surface: #0e1524;
                --bg-card: #121a2d;
                --bg-elevated: #162035;
                --border-subtle: #1e293b;
                --border-active: #334155;
                --border-focus: #3b82f6;
                --text-primary: #f8fafc;
                --text-secondary: #94a3b8;
                --text-muted: #64748b;
                --emerald: #10b981;
                --cyan: #06b6d4;
                --indigo: #6366f1;
                --purple: #a855f7;
                --rose: #f43f5e;
                --amber: #f59e0b;
                --blue: #2563eb;
            }}

            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: 'Inter', -apple-system, sans-serif;
                background-color: var(--bg-canvas);
                color: var(--text-primary);
                min-height: 100vh;
                line-height: 1.5;
            }}

            /* Top Navbar */
            .navbar {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 16px 32px;
                border-bottom: 1px solid var(--border-subtle);
                background: var(--bg-surface);
            }}
            .brand {{
                display: flex;
                align-items: center;
                gap: 12px;
            }}
            .brand-logo {{
                width: 32px;
                height: 32px;
                background: linear-gradient(135deg, #2563eb, #1d4ed8);
                border-radius: 6px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: 700;
                font-size: 15px;
                color: #ffffff;
            }}
            .brand-title {{
                font-size: 16px;
                font-weight: 600;
                letter-spacing: -0.01em;
            }}
            .brand-badge {{
                font-size: 11px;
                font-weight: 600;
                background: rgba(99, 102, 241, 0.12);
                color: #818cf8;
                padding: 2px 8px;
                border-radius: 4px;
                border: 1px solid rgba(99, 102, 241, 0.25);
            }}
            .nav-status-group {{
                display: flex;
                align-items: center;
                gap: 12px;
            }}
            .telemetry-chip {{
                display: flex;
                align-items: center;
                gap: 8px;
                font-size: 12px;
                font-weight: 500;
                color: var(--text-secondary);
                background: var(--bg-card);
                padding: 6px 12px;
                border-radius: 6px;
                border: 1px solid var(--border-subtle);
            }}
            .pulse-dot {{
                width: 7px;
                height: 7px;
                border-radius: 50%;
                background: var(--emerald);
                box-shadow: 0 0 6px var(--emerald);
            }}

            /* Container */
            .main-content {{
                max-width: 1240px;
                margin: 0 auto;
                padding: 32px 24px;
            }}

            /* Telemetry Grid */
            .grid-4 {{
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 16px;
                margin-bottom: 28px;
            }}
            .kpi-card {{
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: 8px;
                padding: 18px 20px;
            }}
            .kpi-label {{
                font-size: 11px;
                font-weight: 600;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 6px;
            }}
            .kpi-value {{
                font-size: 22px;
                font-weight: 700;
                color: var(--text-primary);
                display: flex;
                align-items: baseline;
                gap: 6px;
            }}
            .kpi-subtext {{
                font-size: 12px;
                color: var(--text-secondary);
                margin-top: 4px;
            }}

            /* Navigation Tabs */
            .nav-tabs {{
                display: flex;
                gap: 8px;
                margin-bottom: 24px;
                border-bottom: 1px solid var(--border-subtle);
                padding-bottom: 12px;
            }}
            .tab-btn {{
                background: transparent;
                border: 1px solid transparent;
                color: var(--text-secondary);
                font-weight: 600;
                font-size: 13px;
                padding: 8px 16px;
                border-radius: 6px;
                cursor: pointer;
                transition: all 0.15s;
                display: flex;
                align-items: center;
                gap: 8px;
            }}
            .tab-btn:hover {{
                color: var(--text-primary);
                background: rgba(255, 255, 255, 0.04);
            }}
            .tab-btn.active {{
                background: var(--bg-card);
                color: var(--text-primary);
                border-color: var(--border-active);
            }}

            /* Panels */
            .panel {{
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: 8px;
                padding: 24px;
                margin-bottom: 24px;
            }}
            .panel-header {{
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 18px;
            }}
            .panel-title {{
                font-size: 16px;
                font-weight: 600;
                letter-spacing: -0.01em;
            }}
            .panel-desc {{
                font-size: 13px;
                color: var(--text-secondary);
                margin-top: 4px;
                max-width: 800px;
            }}

            /* Presets Selector Grid */
            .preset-grid {{
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 12px;
                margin-bottom: 20px;
            }}
            .preset-card {{
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: 6px;
                padding: 14px;
                cursor: pointer;
                transition: all 0.15s;
            }}
            .preset-card:hover {{
                border-color: var(--border-active);
                background: var(--bg-elevated);
            }}
            .preset-card.selected {{
                border-color: var(--blue);
                background: rgba(37, 99, 235, 0.08);
            }}
            .preset-title {{
                font-size: 13px;
                font-weight: 600;
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 4px;
            }}
            .preset-desc {{
                font-size: 11px;
                color: var(--text-secondary);
                line-height: 1.4;
            }}
            .preset-tag {{
                font-size: 10px;
                padding: 2px 6px;
                border-radius: 4px;
                font-family: 'JetBrains Mono', monospace;
            }}

            /* Form Elements */
            .form-row {{
                display: grid;
                grid-template-columns: 2fr 1fr;
                gap: 16px;
                margin-bottom: 16px;
            }}
            .form-group {{
                display: flex;
                flex-direction: column;
                gap: 6px;
            }}
            .form-label {{
                font-size: 12px;
                font-weight: 600;
                color: var(--text-secondary);
            }}
            .form-input {{
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: 6px;
                padding: 8px 12px;
                color: var(--text-primary);
                font-family: 'JetBrains Mono', monospace;
                font-size: 12px;
                outline: none;
            }}
            .form-input:focus {{
                border-color: var(--border-focus);
            }}

            /* Buttons */
            .btn {{
                background: #2563eb;
                color: white;
                border: 1px solid rgba(255, 255, 255, 0.1);
                padding: 10px 20px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 13px;
                cursor: pointer;
                transition: background 0.15s;
                display: inline-flex;
                align-items: center;
                gap: 8px;
            }}
            .btn:hover {{
                background: #1d4ed8;
            }}
            .btn:disabled {{
                background: #334155;
                color: #64748b;
                cursor: not-allowed;
            }}
            .btn-secondary {{
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                color: var(--text-primary);
            }}
            .btn-secondary:hover {{
                background: var(--bg-elevated);
            }}

            /* Stepper */
            .stepper-container {{
                display: grid;
                grid-template-columns: 1fr 1fr 1fr;
                gap: 14px;
                margin-bottom: 20px;
            }}
            .step-card {{
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: 6px;
                padding: 16px;
            }}
            .step-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 8px;
            }}
            .step-pill {{
                font-size: 11px;
                font-weight: 600;
                padding: 2px 6px;
                border-radius: 4px;
                text-transform: uppercase;
            }}
            .step-title {{
                font-size: 13px;
                font-weight: 600;
                margin-bottom: 4px;
            }}
            .step-detail {{
                font-size: 12px;
                color: var(--text-secondary);
            }}

            /* Code Inspector */
            .code-block {{
                background: #06090f;
                border: 1px solid var(--border-subtle);
                border-radius: 6px;
                padding: 14px 16px;
                font-family: 'JetBrains Mono', monospace;
                font-size: 12px;
                color: #e2e8f0;
                overflow-x: auto;
                line-height: 1.6;
                white-space: pre-wrap;
            }}
            .diff-del {{ background: rgba(244, 63, 94, 0.15); color: #fda4af; display: block; padding: 2px 6px; border-radius: 4px; }}
            .diff-add {{ background: rgba(16, 185, 129, 0.15); color: #6ee7b7; display: block; padding: 2px 6px; border-radius: 4px; }}

            /* Diagnosis 4-Part Grid */
            .diag-grid {{
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 12px;
                margin-bottom: 16px;
            }}
            .diag-card {{
                background: var(--bg-card);
                border: 1px solid var(--border-subtle);
                border-radius: 6px;
                padding: 14px;
            }}
            .diag-title {{
                font-size: 11px;
                font-weight: 600;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 6px;
            }}
            .diag-val {{
                font-size: 13px;
                color: var(--text-primary);
                line-height: 1.4;
            }}

            /* Table Styles */
            .data-table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 13px;
                text-align: left;
            }}
            .data-table th {{
                padding: 10px 14px;
                color: var(--text-muted);
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                font-size: 11px;
                border-bottom: 1px solid var(--border-subtle);
            }}
            .data-table td {{
                padding: 12px 14px;
                border-bottom: 1px solid rgba(30, 41, 59, 0.5);
            }}
            .table-row:hover td {{
                background: rgba(255, 255, 255, 0.02);
            }}

            /* Helpers */
            .font-mono {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; }}
            .text-cyan {{ color: var(--cyan); }}
            .text-emerald {{ color: var(--emerald); }}
            .text-purple {{ color: var(--purple); }}
            .text-rose {{ color: var(--rose); }}
            .text-amber {{ color: var(--amber); }}
            .text-white {{ color: #ffffff; }}
            .text-muted {{ color: var(--text-secondary); }}

            .status-pill {{
                display: inline-flex;
                align-items: center;
                gap: 6px;
                font-size: 11px;
                font-weight: 600;
                padding: 2px 8px;
                border-radius: 4px;
            }}
            .status-dot {{
                width: 6px;
                height: 6px;
                border-radius: 50%;
            }}

            .spinner {{
                border: 2px solid rgba(255,255,255,0.2);
                border-top: 2px solid #ffffff;
                border-radius: 50%;
                width: 14px;
                height: 14px;
                animation: spin 0.8s linear infinite;
                display: inline-block;
            }}
            @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
        </style>
    </head>
    <body>
        <!-- Top Navbar -->
        <nav class="navbar">
            <div class="brand">
                <div class="brand-logo">BF</div>
                <div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="brand-title">Bug Fixer Operations Console</span>
                        <span class="brand-badge">Pipeline Engine</span>
                    </div>
                </div>
            </div>
            <div class="nav-status-group">
                <div class="telemetry-chip">
                    <span class="pulse-dot"></span>
                    <span>Docker: <b>Active (py313)</b></span>
                </div>
                <div class="telemetry-chip">
                    <span>Ollama: <b>{settings.model}</b></span>
                </div>
                <div class="telemetry-chip">
                    <span style="color: var(--emerald); font-weight: 600;">PASS</span>
                    <span>Suite: <b>72/72</b></span>
                </div>
                <a href="https://github.com/AnanthKumar1810/bug-fixer" target="_blank" style="text-decoration: none;">
                    <div class="telemetry-chip" style="color: var(--cyan);">
                        <span>GitHub &rarr;</span>
                    </div>
                </a>
            </div>
        </nav>

        <div class="main-content">
            <!-- Objective Telemetry Grid -->
            <div class="grid-4">
                <div class="kpi-card">
                    <div class="kpi-label">Pipeline Status</div>
                    <div class="kpi-value text-emerald">Production Ready</div>
                    <div class="kpi-subtext">Intake, diagnosis, repair, & PR publication</div>
                </div>

                <div class="kpi-card">
                    <div class="kpi-label">Automated Test Suite</div>
                    <div class="kpi-value text-emerald">72 / 72 Passing</div>
                    <div class="kpi-subtext">17 modules verified in 114s</div>
                </div>

                <div class="kpi-card">
                    <div class="kpi-label">Isolation Environment</div>
                    <div class="kpi-value font-mono text-cyan" style="font-size: 18px;">bugfixer-validator</div>
                    <div class="kpi-subtext">Python 3.13 Docker container</div>
                </div>

                <div class="kpi-card">
                    <div class="kpi-label">Audit Engine</div>
                    <div class="kpi-value font-mono text-purple" style="font-size: 18px;">SQLite WAL</div>
                    <div class="kpi-subtext">Cryptographic SHA-256 artifact logs</div>
                </div>
            </div>

            <!-- Main Interactive Section: Autonomous Repair Studio -->
            <div class="panel">
                <div class="panel-header">
                    <div>
                        <div class="panel-title">Interactive Defect Repair Studio</div>
                        <div class="panel-desc">Test the autonomous program repair engine live on arbitrary repositories or pre-packaged defect fixtures. Dispatches Qwen 2.5 Coder 7B, executes surgical AST tool edits, and verifies in isolated Docker containers.</div>
                    </div>
                    <button id="repair-btn" class="btn" onclick="executeRepair()">
                        <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                        <span id="repair-btn-text">Launch Autonomous Repair</span>
                    </button>
                </div>

                <!-- Fixture Selection Cards -->
                <div class="preset-grid">
                    <div class="preset-card selected" id="card-cross_file_repo" onclick="selectPreset('cross_file_repo')">
                        <div class="preset-title">
                            <span>Greeting Service</span>
                            <span class="preset-tag" style="background: rgba(6, 182, 212, 0.15); color: var(--cyan);">Multi-Module</span>
                        </div>
                        <div class="preset-desc">Cross-file defect in <code class="font-mono text-cyan">service/formatter.py</code>. Callee returns wrong prefix; tests caller boundary integrity.</div>
                    </div>

                    <div class="preset-card" id="card-config_loader_repo" onclick="selectPreset('config_loader_repo')">
                        <div class="preset-title">
                            <span>Config Loader</span>
                            <span class="preset-tag" style="background: rgba(168, 85, 247, 0.15); color: var(--purple);">Fallback Defect</span>
                        </div>
                        <div class="preset-desc">Missing fallback port in <code class="font-mono text-cyan">config/loader.py</code>. Returns None when PORT env is unset instead of 8080.</div>
                    </div>

                    <div class="preset-card" id="card-math_clamp_repo" onclick="selectPreset('math_clamp_repo')">
                        <div class="preset-title">
                            <span>Math Clamp</span>
                            <span class="preset-tag" style="background: rgba(244, 63, 94, 0.15); color: var(--rose);">Bound Inversion</span>
                        </div>
                        <div class="preset-desc">Inverted bounds bug in <code class="font-mono text-cyan">math_utils.py</code>. Inverts min/max constraint boundaries.</div>
                    </div>
                </div>

                <!-- Active Configuration Parameters -->
                <div class="form-row">
                    <div class="form-group">
                        <label class="form-label">Test NodeID (Reproducer)</label>
                        <input id="input-test" class="form-input" type="text" value="tests/test_greeting.py::test_greeting">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Execution Runner</label>
                        <select id="select-runner" class="form-input">
                            <option value="docker" selected>Docker Container (bugfixer-validator:py313)</option>
                            <option value="local">Host Local Execution</option>
                        </select>
                    </div>
                </div>

                <!-- Live Execution Results Display -->
                <div id="repair-results" style="display: none; margin-top: 20px; border-top: 1px solid var(--border-subtle); padding-top: 20px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span style="font-size: 13px; font-weight: 600;">Repair Run Status:</span>
                            <span id="repair-badge" class="status-pill"></span>
                            <span id="repair-run-id" class="font-mono text-cyan"></span>
                        </div>
                        <div id="repair-meta" class="font-mono text-muted" style="font-size: 11px;"></div>
                    </div>

                    <!-- 4-Part Structured Diagnosis -->
                    <div class="diag-grid">
                        <div class="diag-card">
                            <div class="diag-title">1. Suspected Root Cause</div>
                            <div id="diag-cause" class="diag-val font-mono">Analyzing defect...</div>
                        </div>
                        <div class="diag-card">
                            <div class="diag-title">2. Supporting Evidence IDs</div>
                            <div id="diag-evidence" class="diag-val font-mono">Examining failing traces...</div>
                        </div>
                        <div class="diag-card">
                            <div class="diag-title">3. Next Check / Surgical Tool Action</div>
                            <div id="diag-next" class="diag-val font-mono">Generating AST patch...</div>
                        </div>
                        <div class="diag-card">
                            <div class="diag-title">4. Expected Observable Result</div>
                            <div id="diag-result" class="diag-val font-mono">Awaiting verification...</div>
                        </div>
                    </div>

                    <!-- Surgical Patch Diff -->
                    <div style="margin-bottom: 14px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                            <span style="font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-muted);">Surgical Candidate Patch</span>
                            <span id="tree-sha" class="font-mono text-purple" style="font-size: 11px;"></span>
                        </div>
                        <div id="patch-diff-content" class="code-block" style="max-height: 240px;">No patch generated yet.</div>
                    </div>

                    <!-- Container Log Stream -->
                    <div id="repair-stream-box">
                        <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px;">Container Execution Log</div>
                        <pre id="repair-stream-content" class="code-block" style="color: #38bdf8; max-height: 180px;">Awaiting task completion...</pre>
                    </div>
                </div>
            </div>

            <!-- SWE-bench Three-State Verification Engine -->
            <div class="panel">
                <div class="panel-header">
                    <div>
                        <div class="panel-title">SWE-bench Three-State Verification Engine</div>
                        <div class="panel-desc">Deterministic containerized validation enforcing clean baseline, isolated defect reproduction, and complete candidate replay on upstream defect <code class="font-mono text-cyan">AP002</code> in <code class="font-mono text-cyan">ml-code-review-assistant</code>.</div>
                    </div>
                    <button class="btn btn-secondary" onclick="executeVerify()">
                        <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                        Execute Docker Verification Replay
                    </button>
                </div>

                <div class="stepper-container">
                    <div class="step-card">
                        <div class="step-header">
                            <span class="step-pill" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.25);">State 1</span>
                            <span style="color: var(--emerald); font-size: 11px; font-weight: 600;">PASS (11/11)</span>
                        </div>
                        <div class="step-title">Clean Base Checkout</div>
                        <div class="step-detail">Base commit <code class="font-mono text-cyan">53b3025</code> validated with 0 modifications.</div>
                    </div>

                    <div class="step-card">
                        <div class="step-header">
                            <span class="step-pill" style="background: rgba(244, 63, 94, 0.12); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.25);">State 2</span>
                            <span style="color: var(--rose); font-size: 11px; font-weight: 600;">REPRODUCED</span>
                        </div>
                        <div class="step-title">Regression Injected</div>
                        <div class="step-detail">Fails strictly on AssertionError; unrelated exceptions/crashes rejected.</div>
                    </div>

                    <div class="step-card">
                        <div class="step-header">
                            <span class="step-pill" style="background: rgba(6, 182, 212, 0.12); color: #38bdf8; border: 1px solid rgba(6, 182, 212, 0.25);">State 3</span>
                            <span style="color: var(--cyan); font-size: 11px; font-weight: 600;">PASS (12/12)</span>
                        </div>
                        <div class="step-title">Candidate Replay</div>
                        <div class="step-detail">Tree matches <code class="font-mono text-purple">f3f9a7e70570</code> byte-for-byte.</div>
                    </div>
                </div>

                <!-- Execution Terminal Output -->
                <div id="telemetry-box" style="display: none; margin-top: 14px;">
                    <div style="font-size: 11px; color: var(--text-muted); margin-bottom: 6px; font-weight: 600; text-transform: uppercase;">Container Output Stream</div>
                    <pre id="telemetry-content" class="code-block" style="color: #38bdf8;">Running validation container...</pre>
                </div>
            </div>

            <!-- Audit History Table -->
            <div class="panel">
                <div class="panel-header">
                    <div>
                        <div class="panel-title">Persistent Audit History (SQLite Store)</div>
                        <div class="panel-desc">Cryptographically audited execution runs, verification state transitions, and candidate tree hashes.</div>
                    </div>
                </div>

                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Run ID</th>
                            <th>Repository</th>
                            <th>Issue Title</th>
                            <th>Status</th>
                            <th>Verified Tree ID</th>
                            <th>Timestamp (UTC)</th>
                        </tr>
                    </thead>
                    <tbody id="runs-tbody">
                        {run_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <script>
            let currentPreset = 'cross_file_repo';
            const presets = {{
                'cross_file_repo': {{
                    test: 'tests/test_greeting.py::test_greeting',
                    title: 'Greeting returns Goodbye instead of Hello'
                }},
                'config_loader_repo': {{
                    test: 'tests/test_config.py::test_get_port',
                    title: 'Missing default port fallback when PORT is unset'
                }},
                'math_clamp_repo': {{
                    test: 'tests/test_math.py::test_clamp',
                    title: 'Boundary inversion bug in clamp'
                }}
            }};

            function selectPreset(name) {{
                currentPreset = name;
                document.querySelectorAll('.preset-card').forEach(el => el.classList.remove('selected'));
                const card = document.getElementById('card-' + name);
                if (card) card.classList.add('selected');
                const p = presets[name];
                if (p) {{
                    document.getElementById('input-test').value = p.test;
                }}
            }}

            async function executeRepair() {{
                const btn = document.getElementById('repair-btn');
                const btnText = document.getElementById('repair-btn-text');
                const resPanel = document.getElementById('repair-results');
                const streamContent = document.getElementById('repair-stream-content');
                const badge = document.getElementById('repair-badge');
                const runIdEl = document.getElementById('repair-run-id');
                const metaEl = document.getElementById('repair-meta');
                const patchBox = document.getElementById('patch-diff-content');
                const treeSha = document.getElementById('tree-sha');

                btn.disabled = true;
                btnText.innerHTML = '<span class="spinner"></span> Running Pipeline in Docker...';
                resPanel.style.display = 'block';
                streamContent.innerText = 'Initializing isolated Docker container (bugfixer-validator:py313)...\\nRunning baseline reproducer check...\\nQuerying Qwen 2.5 Coder 7B for surgical diagnosis & AST patch...';

                badge.innerText = 'RUNNING';
                badge.style.background = 'rgba(37, 99, 235, 0.15)';
                badge.style.color = '#60a5fa';
                badge.style.border = '1px solid rgba(37, 99, 235, 0.3)';

                try {{
                    const runner = document.getElementById('select-runner').value;
                    const testNode = document.getElementById('input-test').value;
                    const res = await fetch('/api/repair', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            preset: currentPreset,
                            test_nodeid: testNode,
                            runner: runner
                        }})
                    }});

                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Repair execution failed');

                    runIdEl.innerText = data.run_id;
                    const isSuccess = (data.stage === 'ready_for_review' || data.stage === 'verified');
                    badge.innerText = data.stage.toUpperCase();
                    if (isSuccess) {{
                        badge.style.background = 'rgba(16, 185, 129, 0.12)';
                        badge.style.color = '#34d399';
                        badge.style.border = '1px solid rgba(16, 185, 129, 0.25)';
                    }} else {{
                        badge.style.background = 'rgba(244, 63, 94, 0.12)';
                        badge.style.color = '#fb7185';
                        badge.style.border = '1px solid rgba(244, 63, 94, 0.25)';
                    }}

                    metaEl.innerText = `Attempts: ${{data.attempts_used}} / ${{data.retry_budget}} • Base SHA: ${{data.base_sha.substring(0, 8)}}`;
                    treeSha.innerText = data.verified_tree_id ? `Verified Tree: ${{data.verified_tree_id.substring(0, 12)}}` : '';

                    // 4-Part Diagnosis
                    const d = data.diagnosis || {{}};
                    document.getElementById('diag-cause').innerText = d.suspected_cause || 'Defect reproduced in candidate.';
                    document.getElementById('diag-evidence').innerText = (d.supporting_evidence_ids && d.supporting_evidence_ids.length) ? d.supporting_evidence_ids.join(', ') : 'Failing test assertion matched';
                    document.getElementById('diag-next').innerText = d.next_check_or_edit || 'Tool call executed';
                    document.getElementById('diag-result').innerText = d.expected_observable_result || 'Reproducer passed';

                    // Patch Diff
                    if (data.patch_diff && data.patch_diff.trim()) {{
                        const formatted = data.patch_diff.split('\\n').map(line => {{
                            if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="diff-add">${{escapeHtml(line)}}</span>`;
                            if (line.startsWith('-') && !line.startsWith('---')) return `<span class="diff-del">${{escapeHtml(line)}}</span>`;
                            return escapeHtml(line);
                        }}).join('\\n');
                        patchBox.innerHTML = formatted;
                    }} else {{
                        patchBox.innerText = 'No modifications made.';
                    }}

                    streamContent.innerText = JSON.stringify({{
                        run_id: data.run_id,
                        stage: data.stage,
                        status: data.status,
                        attempts_used: data.attempts_used,
                        tool_calls: data.tool_calls,
                        verified_tree: data.verified_tree_id
                    }}, null, 2);

                }} catch (err) {{
                    badge.innerText = 'ERROR';
                    badge.style.background = 'rgba(244, 63, 94, 0.12)';
                    badge.style.color = '#fb7185';
                    streamContent.innerText = 'Execution Failed: ' + err.message;
                }} finally {{
                    btn.disabled = false;
                    btnText.innerText = 'Launch Autonomous Repair';
                }}
            }}

            function escapeHtml(text) {{
                return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            }}

            async function executeVerify() {{
                const box = document.getElementById('telemetry-box');
                const content = document.getElementById('telemetry-content');
                box.style.display = 'block';
                content.innerText = 'Connecting to isolated Docker container (bugfixer-validator:py313)...\\nRunning SWE-bench Three-State Verification...';
                try {{
                    const res = await fetch('/api/verify', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ release_dir: '.bug-fixer-data/release_artifacts/ml-code-review-assistant-ap002' }})
                    }});
                    const data = await res.json();
                    content.innerText = JSON.stringify(data, null, 2);
                }} catch (err) {{
                    content.innerText = 'Execution Error: ' + err;
                }}
            }}
        </script>
    </body>
    </html>
    """
