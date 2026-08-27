from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ReviewProvider:
    def __init__(self, reviewer: Callable[[dict[str, Any]], dict[str, Any]]):
        self.reviewer = reviewer

    def review(self, change_set: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.reviewer(change_set)
        except TimeoutError:
            return {
                "verdict": "INCOMPLETE",
                "reason_code": "REVIEW_PROVIDER_TIMEOUT",
                "standards": None,
                "spec": None,
            }
        if not result:
            return {
                "verdict": "INCOMPLETE",
                "reason_code": "REVIEW_PROVIDER_EMPTY",
                "standards": None,
                "spec": None,
            }
        return result
