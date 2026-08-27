from __future__ import annotations

from pathlib import Path
from typing import Any


class IcodeClient:
    def __init__(
        self,
        submitter: Any | None = None,
        *,
        runtime: Any | None = None,
    ):
        if submitter is not None or runtime is None:
            raise ValueError("ICODE_RUNTIME_REQUIRED")
        self.runtime = runtime

    def preflight(self, repo_path: Path) -> dict[str, Any]:
        if self.runtime is None:
            raise RuntimeError("ICODE_RUNTIME_REQUIRED")
        return self.runtime.preflight(repo_path)

    def submit(
        self,
        revision_set: dict[str, Any],
        approval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if approval is None:
            raise ValueError("APPROVAL_REQUIRED:G7")
        return self.runtime.submit(revision_set, approval)
