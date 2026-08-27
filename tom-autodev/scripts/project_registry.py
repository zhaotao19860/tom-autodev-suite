from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

import yaml

from schema_validator import validate_schema


DEFAULT_CONFIG_ROOT = Path.home() / ".tom-autodev"
_SECRET_KEY = re.compile(r"(?:api[_-]?key|auth|credential|password|private[_-]?key|secret|token)", re.I)


def project_profiles_dir(config_root: Path | str | None = None) -> Path:
    root = Path(config_root).expanduser() if config_root is not None else DEFAULT_CONFIG_ROOT
    return root / "config" / "projects"


def profile_path(project: str, config_root: Path | str | None = None) -> Path:
    return project_profiles_dir(config_root) / f"{project}.yaml"


def validate_profile(profile: dict[str, Any], *, check_paths: bool = True) -> dict[str, Any]:
    if not isinstance(profile, dict):
        return _not_ready(profile, ["profile_mapping"], [])

    issues = validate_schema(profile)
    missing = [issue.path for issue in issues if issue.kind == "missing"]
    invalid = [issue.path for issue in issues if issue.kind == "invalid"]
    missing.extend(_semantic_missing_paths(profile))
    invalid.extend(_secret_paths(profile))
    invalid.extend(_semantic_invalid_paths(profile))
    if check_paths:
        invalid.extend(_path_invalid_paths(profile))
    missing = _canonical_paths(missing)
    invalid = _canonical_paths(invalid)
    return _not_ready(profile, missing, invalid)


def load_profile(path: Path | str, *, check_paths: bool = True) -> dict[str, Any]:
    profile_file = Path(path).expanduser()
    if not profile_file.is_file():
        return {
            "ready": False,
            "reason_code": "PROJECT_NOT_READY",
            "missing": ["profile_file"],
            "invalid": [],
            "profile_path": str(profile_file),
        }
    try:
        profile = yaml.safe_load(profile_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {
            "ready": False,
            "reason_code": "PROJECT_NOT_READY",
            "missing": ["profile_parse"],
            "invalid": [],
            "profile_path": str(profile_file),
        }
    result = validate_profile(profile, check_paths=check_paths)
    return {**result, "profile_path": str(profile_file)}


def save_profile(
    path: Path,
    profile: dict[str, Any],
    previous_hash: str | None,
    confirmation: bool,
) -> dict[str, Any]:
    profile_path = Path(path).expanduser()
    if not confirmation:
        return {"ready": False, "reason_code": "PROFILE_CONFIRMATION_REQUIRED", "profile_path": str(profile_path)}
    validation = validate_profile(profile)
    if not validation["ready"]:
        return {**validation, "profile_path": str(profile_path)}
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(profile, allow_unicode=False, sort_keys=True).encode("utf-8")
    with _profile_lock(profile_path):
        exists = profile_path.exists()
        if exists:
            current_hash = hashlib.sha256(profile_path.read_bytes()).hexdigest()
            if previous_hash != current_hash:
                return {
                    "ready": False,
                    "reason_code": "PROFILE_CONFLICT",
                    "profile_path": str(profile_path),
                    "content_hash": current_hash,
                }
        elif previous_hash is not None:
            return {"ready": False, "reason_code": "PROFILE_CONFLICT", "profile_path": str(profile_path)}
        _write_atomic(profile_path, content)
    return {
        "ready": True,
        "reason_code": "READY",
        "profile_path": str(profile_path),
        "content_hash": hashlib.sha256(content).hexdigest(),
        "created": not exists,
    }


def _not_ready(profile: Any, missing: list[str], invalid: list[str]) -> dict[str, Any]:
    ready = not missing and not invalid
    return {
        "ready": ready,
        "reason_code": "READY" if ready else "PROJECT_NOT_READY",
        "missing": missing,
        "invalid": invalid,
        "project_id": profile.get("project_id") if isinstance(profile, dict) else None,
        "profile": _redact_secrets(profile),
    }


def _semantic_invalid_paths(profile: dict[str, Any]) -> list[str]:
    return []


def _semantic_missing_paths(profile: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    sources = profile.get("knowledge_sources")
    if isinstance(sources, list):
        ku_sources = [source for source in sources if isinstance(source, dict) and source.get("provider") == "ku"]
        if not ku_sources:
            return ["knowledge_sources.ku"]
        for index, source in enumerate(sources):
            if isinstance(source, dict) and source.get("provider") == "ku":
                for field in ("repo_id", "parent_doc_id"):
                    if not isinstance(source.get(field), str) or not source[field]:
                        missing.append(f"knowledge_sources[{index}].{field}")
    return missing


def _path_invalid_paths(profile: dict[str, Any]) -> list[str]:
    invalid: list[str] = []
    for index, repo in enumerate(profile.get("business_repos", [])):
        if isinstance(repo, dict):
            invalid.extend(_repository_path_errors(repo.get("path"), f"business_repos[{index}].path"))
    test_repo = profile.get("test_repo")
    if isinstance(test_repo, dict):
        invalid.extend(_repository_path_errors(test_repo.get("path"), "test_repo.path"))
        test_top = _repository_top_level(test_repo.get("path"))
        if test_top is not None:
            for repo in profile.get("business_repos", []):
                if isinstance(repo, dict) and _repository_top_level(repo.get("path")) == test_top:
                    invalid.append("test_repo.path")
                    break
    for field in ("language_skill", "project_skill"):
        value = profile.get(field)
        if isinstance(value, str):
            skill_path = Path(value).expanduser()
            if not skill_path.is_dir() or not (skill_path / "SKILL.md").is_file():
                invalid.append(field)
    return invalid


def _repository_path_errors(value: Any, field: str) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    path = Path(value).expanduser()
    if not path.is_dir():
        return [field]
    if _repository_top_level(value) is None:
        return [field]
    return []


def _repository_top_level(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    try:
        inside = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        top_level = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(top_level).resolve() if inside == "true" else None


def _secret_paths(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key in sorted(value):
            child = value[key]
            child_path = key if not path else f"{path}.{key}"
            if _SECRET_KEY.search(key):
                paths.append(child_path)
            else:
                paths.extend(_secret_paths(child, child_path))
        return paths
    if isinstance(value, list):
        paths: list[str] = []
        for index, child in enumerate(value):
            paths.extend(_secret_paths(child, f"{path}[{index}]"))
        return paths
    return []


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(key) else _redact_secrets(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(child) for child in value]
    return value


def _canonical_paths(items: list[str]) -> list[str]:
    return sorted(set(items))


@contextmanager
def _profile_lock(profile_path: Path):
    lock_path = profile_path.with_name(f"{profile_path.name}.lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_atomic(profile_path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=profile_path.parent,
            prefix=f".{profile_path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, profile_path)
        directory_fd = os.open(profile_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
