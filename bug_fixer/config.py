"""Configuration settings for Bug Fixer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int, low: int, high: int) -> int:
    val_str = os.environ.get(name)
    if val_str is None:
        return default
    try:
        val = int(val_str)
        if low <= val <= high:
            return val
    except ValueError:
        pass
    return default


def _default_data_dir() -> Path:
    env_dir = os.environ.get("BUG_FIXER_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    # Resolve relative to project root (parent of bug_fixer package)
    project_root = Path(__file__).resolve().parent.parent
    return (project_root / ".bug-fixer-data").resolve()


@dataclass(frozen=True)
class Settings:
    model: str = "qwen2.5-coder:7b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    validator_image: str = "bugfixer-validator:py313"
    fallback_validator_image: str = "agentforge-validator:py313"
    data_dir: Path = _default_data_dir()
    max_repair_attempts: int = 2
    validation_timeout_seconds: int = 90
    max_file_bytes: int = 500_000
    runner: str = "docker"  # "docker" or "local"

    @classmethod
    def from_env(cls) -> Settings:
        model = os.environ.get("BUG_FIXER_MODEL", "qwen2.5-coder:7b")
        ollama_url = os.environ.get("BUG_FIXER_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        data_dir = _default_data_dir()
        runner = os.environ.get("BUG_FIXER_RUNNER", "docker").lower()
        if runner not in {"docker", "local"}:
            runner = "docker"

        validator_image = os.environ.get("BUG_FIXER_VALIDATOR_IMAGE", "bugfixer-validator:py313")
        max_attempts = _int_env("BUG_FIXER_MAX_REPAIR_ATTEMPTS", 2, 1, 10)
        timeout = _int_env("BUG_FIXER_VALIDATION_TIMEOUT_SECONDS", 90, 5, 600)
        max_file_bytes = _int_env("BUG_FIXER_MAX_FILE_BYTES", 500_000, 10_000, 10_000_000)

        return cls(
            model=model,
            ollama_base_url=ollama_url,
            validator_image=validator_image,
            data_dir=data_dir,
            max_repair_attempts=max_attempts,
            validation_timeout_seconds=timeout,
            max_file_bytes=max_file_bytes,
            runner=runner,
        )
