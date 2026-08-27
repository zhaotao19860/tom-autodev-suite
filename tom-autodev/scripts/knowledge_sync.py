from __future__ import annotations

import hashlib
import json
from typing import Any

from clients.icafe_client import CafeClient
from clients.ku_client import KuClient
from persistence_policy import validate_evidence_refs
from state_store import StateStore


_PROJECT_KU_TARGETS = {
    "bgw": ("sX0BTOBWJX", "I15ClP2KW4ZGAK"),
    "xflow": ("sX0BTOBWJX", "meQ-Acjg0K09Xr"),
}


class KnowledgeSync:
    def __init__(
        self,
        *,
        state_store: StateStore,
        ku_client: Any,
        cafe_client: Any,
        parent_doc_id: str | None,
        card_id: str,
        project_parent_doc_id: str | None = None,
        run_root_title: str | None = None,
        run_id: str,
    ):
        self.state = state_store
        self.ku = ku_client
        self.cafe = cafe_client
        self.parent_doc_id = parent_doc_id
        self.card_id = card_id
        self.project_parent_doc_id = project_parent_doc_id
        self.run_root_title = run_root_title
        self.run_id = run_id

    @classmethod
    def from_profile(
        cls,
        profile: dict[str, Any],
        *,
        run_id: str,
        card_id: str,
        state_store: StateStore,
        ku_transport: Any | None = None,
        cafe_transport: Any | None = None,
        username: str | None = None,
        cafe_preflight: bool = True,
    ) -> "KnowledgeSync":
        project_id = profile.get("project_id") if isinstance(profile, dict) else None
        expected = _PROJECT_KU_TARGETS.get(str(project_id).lower())
        sources = profile.get("knowledge_sources") if isinstance(profile, dict) else None
        ku_sources = [
            source
            for source in sources or []
            if isinstance(source, dict) and source.get("provider") == "ku"
        ] if isinstance(sources, list) else []
        if expected is None or len(ku_sources) != 1:
            raise ValueError("PROJECT_KU_TARGET_INVALID")
        source = ku_sources[0]
        if (source.get("repo_id"), source.get("parent_doc_id")) != expected:
            raise ValueError("PROJECT_KU_TARGET_INVALID")
        repo_id, parent_doc_id = expected
        root_title = f"{card_id}-{run_id[:12]}-研发测试协作"
        return cls(
            state_store=state_store,
            ku_client=KuClient(
                transport=ku_transport,
                state_store=state_store,
                run_id=run_id,
                repo_id=repo_id,
                username=username,
            ),
            cafe_client=CafeClient(
                transport=cafe_transport,
                state_store=state_store,
                run_id=run_id,
                preflight=cafe_preflight,
            ),
            parent_doc_id=None,
            project_parent_doc_id=parent_doc_id,
            run_root_title=root_title,
            card_id=card_id,
            run_id=run_id,
        )

    def publish_phase(self, run_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        if run_id != self.run_id:
            return _failure("RUN_ID_MISMATCH")
        bound_run_id = self.run_id
        invalid = self._validate_artifact(bound_run_id, artifact)
        if invalid is not None:
            return invalid
        root = self._resolve_root(bound_run_id)
        if not root["ok"]:
            return root
        root_doc_id = root["doc_id"]
        title = artifact["title"]
        markdown = artifact["markdown"]
        content_hash = artifact["content_hash"]
        operation_hash = _canonical_hash(
            {
                "run_id": bound_run_id,
                "parent_doc_id": root_doc_id,
                "card_id": self.card_id,
                "title_hash": hashlib.sha256(title.encode()).hexdigest(),
                "content_hash": content_hash,
            }
        )
        result_key = f"knowledge-sync-result:{operation_hash}"
        existing = self.state.idempotency_result(result_key)
        if existing is not None:
            return existing
        intent = self.state.intent(
            bound_run_id,
            "knowledge.publish-phase",
            f"knowledge-sync:{operation_hash}",
            {
                "parent_doc_id": root_doc_id,
                "card_id": self.card_id,
                "title_hash": hashlib.sha256(title.encode()).hexdigest(),
                "content_hash": content_hash,
            },
        )

        child = self.ku.create_artifact(root_doc_id, title, markdown)
        child_error = self._verify_child(child, content_hash)
        if child_error is not None:
            return child_error
        entry = {
            "title": title,
            "doc_id": child["doc_id"],
            "url": child["url"],
            "content_hash": child["content_hash"],
        }
        index = self.ku.update_index(root_doc_id, entry)
        index_error = self._verify_index(index, root_doc_id)
        if index_error is not None:
            return index_error

        comment = self.cafe.comment(
            self.card_id,
            (
                f"Published phase artifact: {title}\n"
                f"Knowledge: {child['url']}\n"
                f"Content hash: {content_hash}"
            ),
            f"knowledge:{bound_run_id}:{operation_hash}",
        )
        comment_error = self._verify_comment(comment)
        if comment_error is not None:
            return comment_error
        lower_pending = self._pending_lower_intent(bound_run_id, intent["intent_id"])
        if lower_pending is not None:
            return {
                **_failure("QUERY_REQUIRED"),
                "intent_id": lower_pending["intent_id"],
                "retry_allowed": False,
            }

        evidence_refs = [
            child["evidence_refs"][0],
            index["evidence_refs"][0],
            comment["evidence_refs"][0],
        ]
        try:
            evidence_refs = validate_evidence_refs(evidence_refs)
        except ValueError:
            return _failure("KNOWLEDGE_EVIDENCE_INVALID")
        response = {
            "ok": True,
            "reason_code": "OK",
            "schema_version": "1",
            "run_id": bound_run_id,
            "artifact_hash": content_hash,
            "child_doc_id": child["doc_id"],
            "child_url": child["url"],
            "child_version": child["version"],
            "index_doc_id": index["doc_id"],
            "index_version": index["version"],
            "comment_id": str(comment["comment_id"]),
            "evidence_refs": evidence_refs,
        }
        self.state.receipt(
            intent["intent_id"],
            {
                "ok": True,
                "schema_version": "1",
                "artifact_hash": content_hash,
                "child_doc_id": child["doc_id"],
                "child_url": child["url"],
                "child_version": child["version"],
                "index_doc_id": index["doc_id"],
                "index_version": index["version"],
                "comment_id": str(comment["comment_id"]),
            },
            evidence_refs,
        )
        self.state.save_idempotency_result(result_key, response)
        return response

    def _pending_lower_intent(
        self, run_id: str, phase_intent_id: str
    ) -> dict[str, Any] | None:
        return next(
            (
                pending
                for pending in self.state.pending_intents(run_id)
                if pending["intent_id"] != phase_intent_id
            ),
            None,
        )

    def _resolve_root(self, run_id: str) -> dict[str, Any]:
        if isinstance(self.parent_doc_id, str) and self.parent_doc_id:
            return {"ok": True, "reason_code": "OK", "doc_id": self.parent_doc_id}
        if (
            not isinstance(self.project_parent_doc_id, str)
            or not self.project_parent_doc_id
            or not isinstance(self.run_root_title, str)
            or not self.run_root_title
        ):
            return _failure("KNOWLEDGE_TARGET_INVALID")
        markdown = (
            f"# {self.run_root_title}\n\n"
            f"Run: {run_id}\n\n"
            f"Card: {self.card_id}"
        )
        root = self.ku.ensure_run_root(
            self.project_parent_doc_id, self.run_root_title, markdown
        )
        if not isinstance(root, dict) or not root.get("ok") or not root.get("doc_id"):
            return _failure(_reason(root, "KU_RUN_ROOT_VERIFICATION_FAILED"))
        try:
            validate_evidence_refs(root.get("evidence_refs"))
        except ValueError:
            return _failure("KNOWLEDGE_EVIDENCE_INVALID")
        return root

    def _validate_artifact(
        self, run_id: Any, artifact: Any
    ) -> dict[str, Any] | None:
        if not isinstance(run_id, str) or not run_id or not isinstance(artifact, dict):
            return _failure("INVALID_INPUT")
        for key in ("title", "markdown", "content_hash"):
            if not isinstance(artifact.get(key), str) or not artifact[key]:
                return _failure("INVALID_INPUT")
        if not self.card_id or (
            not self.parent_doc_id
            and (not self.project_parent_doc_id or not self.run_root_title)
        ):
            return _failure("KNOWLEDGE_TARGET_INVALID")
        actual = hashlib.sha256(artifact["markdown"].encode("utf-8")).hexdigest()
        if artifact["content_hash"] != actual:
            return _failure("ARTIFACT_HASH_MISMATCH")
        return None

    @staticmethod
    def _verify_child(result: Any, content_hash: str) -> dict[str, Any] | None:
        if not isinstance(result, dict) or not result.get("ok"):
            return _failure(_reason(result, "KU_CREATE_VERIFICATION_FAILED"))
        required = ("doc_id", "url", "version", "content_hash", "evidence_refs")
        if any(not result.get(key) for key in required) or result.get("content_hash") != content_hash:
            return _failure("KU_CREATE_VERIFICATION_FAILED")
        expected = f"ku:{result['doc_id']}/{result['version']}"
        if not _exact_evidence(result["evidence_refs"], expected):
            return _failure("KNOWLEDGE_EVIDENCE_INVALID")
        return None

    def _verify_index(
        self, result: Any, root_doc_id: str
    ) -> dict[str, Any] | None:
        if not isinstance(result, dict) or not result.get("ok"):
            return _failure(_reason(result, "KU_INDEX_VERIFICATION_FAILED"))
        if result.get("doc_id") != root_doc_id or not result.get("version"):
            return _failure("KU_INDEX_VERIFICATION_FAILED")
        expected = f"ku:{root_doc_id}/{result['version']}"
        if not _exact_evidence(result.get("evidence_refs"), expected):
            return _failure("KNOWLEDGE_EVIDENCE_INVALID")
        return None

    def _verify_comment(self, result: Any) -> dict[str, Any] | None:
        if not isinstance(result, dict) or not result.get("ok"):
            return _failure(_reason(result, "ICAFE_COMMENT_VERIFICATION_FAILED"))
        if result.get("comment_id") is None:
            return _failure("ICAFE_COMMENT_VERIFICATION_FAILED")
        expected = f"icafe:{self.card_id}/{result['comment_id']}"
        if not _exact_evidence(result.get("evidence_refs"), expected):
            return _failure("KNOWLEDGE_EVIDENCE_INVALID")
        return None


def _failure(reason_code: str) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, "phase_complete": False}


def _reason(result: Any, fallback: str) -> str:
    if isinstance(result, dict) and isinstance(result.get("reason_code"), str):
        return result["reason_code"]
    return fallback


def _exact_evidence(value: Any, expected: str) -> bool:
    try:
        references = validate_evidence_refs(value)
    except ValueError:
        return False
    return references == [expected]


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
