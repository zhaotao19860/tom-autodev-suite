from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ApprovalChannels:
    def __init__(self, publishers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]):
        self.publishers = publishers

    def publish(self, request: dict[str, Any]) -> dict[str, dict[str, Any]]:
        receipts: dict[str, dict[str, Any]] = {}
        for name, publisher in self.publishers.items():
            try:
                receipts[name] = publisher(request)
            except Exception as error:
                raise RuntimeError(f"APPROVAL_CHANNEL_FAILED:{name}") from error
        return receipts
