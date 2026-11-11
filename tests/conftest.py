"""Pytest configuration and fixtures for Bug Fixer test suite."""

import shutil
import subprocess
from pathlib import Path
import pytest

from bug_fixer.config import Settings
from bug_fixer.db import EvidenceStore
from bug_fixer.workspace import Workspace


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    data_d = tmp_path / "data"
    data_d.mkdir(parents=True, exist_ok=True)
    return Settings(
        model="qwen2.5-coder:7b",
        ollama_base_url="http://127.0.0.1:11434",
        validator_image="bugfixer-validator:py313",
        data_dir=data_d,
        max_repair_attempts=2,
        validation_timeout_seconds=30,
        runner="local",  # default to fast local execution for unit tests; docker tests specify docker
    )


@pytest.fixture
def evidence_store(test_settings: Settings) -> EvidenceStore:
    return EvidenceStore(
        test_settings.data_dir / "bug_fixer.db",
        test_settings.data_dir / "artifacts",
    )


@pytest.fixture
def cross_file_fixture(tmp_path: Path) -> Path:
    """Create a temporary copy of the cross-file caller-callee fixture with Git initialized."""
    source_fixture = Path(__file__).parent.parent / "fixtures" / "cross_file_repo"
    target = tmp_path / "repo"
    shutil.copytree(source_fixture, target)
    subprocess.run(["git", "init", "-q"], cwd=target, check=False)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=target, check=False)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target, check=False)
    subprocess.run(["git", "add", "-A"], cwd=target, check=False)
    subprocess.run(["git", "commit", "-q", "-m", "initial base"], cwd=target, check=False)
    return target
