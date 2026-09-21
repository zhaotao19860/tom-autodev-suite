from __future__ import annotations

from execution_guard import guard_execution

import hashlib
import json
from typing import Any

from clients.icafe_client import CafeClient
from clients.ku_client import KuClient
from persistence_policy import validate_evidence_refs
from phase_document import canonical_appendix
from state_store import StateStore


_PROJECT_KU_TARGETS = {
    "bgw": ("sX0BTOBWJX", "I15ClP2KW4ZGAK"),
    "xflow": ("sX0BTOBWJX", "meQ-Acjg0K09Xr"),
}


def project_ku_target(profile: Any) -> tuple[str, str] | None:
    """Where a run publishes its collaboration documents.

    The repository is fixed per project. The directory follows the requirement:
    when the profile names a primary KU knowledge source in that repository, the
    run root is created beside the requirement documents it reads, which is where
    the requirement owner expects to find it. Projects without such a source fall
    back to the shared project directory.
    """
    project_id = profile.get("project_id") if isinstance(profile, dict) else profile
    default = _PROJECT_KU_TARGETS.get(str(project_id).lower())
    if default is None or not isinstance(profile, dict):
        return default
    repo_id = default[0]
    sources = [
        source
        for source in profile.get("knowledge_sources") or []
        if isinstance(source, dict)
        and source.get("provider") == "ku"
        and source.get("repo_id") == repo_id
        and isinstance(source.get("parent_doc_id"), str)
        and source["parent_doc_id"]
    ]
    if not sources:
        return default
    primary = min(sources, key=lambda source: (source.get("priority", 0), source["parent_doc_id"]))
    return repo_id, primary["parent_doc_id"]


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
        expected = project_ku_target(profile)
        if expected is None:
            raise ValueError("PROJECT_KU_TARGET_INVALID")
        repo_id, parent_doc_id = expected
        repo_paths = [
            repo["path"]
            for repo in (profile.get("business_repos") or [])
            if isinstance(repo, dict) and repo.get("path")
        ] if isinstance(profile, dict) else []
        root_title = f"{card_id}-{run_id[:12]}-研发测试协作"
        return cls(
            state_store=state_store,
            ku_client=KuClient(
                transport=ku_transport,
                state_store=state_store,
                run_id=run_id,
                repo_id=repo_id,
                username=username,
                repo_paths=repo_paths,
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

    @guard_execution
    def publish_phase(self, run_id: str, artifact: dict[str, Any], scope: str = "both") -> dict[str, Any]:
        # scope (slimming Stage B): "both" = KU doc + iCafe comment; "ku_only" = KU doc, no
        # comment; "icafe_only" = milestone comment, no KU doc; "skip" = neither. The phase
        # artifact is stored by the caller regardless; this only governs the human-facing
        # KU/iCafe writes.
        if scope == "skip":
            return {
                "ok": True, "reason_code": "OK", "schema_version": "1", "run_id": self.run_id,
                "artifact_hash": artifact.get("content_hash"), "child_doc_id": None,
                "child_url": None, "child_version": None, "index_doc_id": None,
                "index_version": None, "comment_id": None, "evidence_refs": [], "scope": scope,
            }
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
            self.state.live_idempotency_key(f"knowledge-sync:{operation_hash}"),
            {
                "parent_doc_id": root_doc_id,
                "card_id": self.card_id,
                "title_hash": hashlib.sha256(title.encode()).hexdigest(),
                "content_hash": content_hash,
            },
        )

        do_ku = scope in ("both", "ku_only")
        do_icafe = scope in ("both", "icafe_only")
        child: dict[str, Any] | None = None
        index: dict[str, Any] | None = None
        comment: dict[str, Any] | None = None
        if do_ku:
            child = self.ku.create_artifact(root_doc_id, title, markdown)
            # The document is verified against what was written to it; the index entry
            # carries the artifact hash, which is what the control plane records.
            child_error = self._verify_child(
                child, hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            )
            if child_error is not None:
                return self._settled(intent, child_error)
            entry = {
                "title": title,
                "doc_id": child["doc_id"],
                "url": child["url"],
                "content_hash": content_hash,
            }
            index = self.ku.update_index(root_doc_id, entry)
            index_error = self._verify_index(index, root_doc_id)
            if index_error is not None:
                return index_error

        if do_icafe:
            body = (
                f"Published phase artifact: {title}\nKnowledge: {child['url']}\n"
                f"Content hash: {content_hash}"
            ) if child is not None else (
                # icafe_only milestone: no child doc, so point at the run's KU root.
                f"Phase milestone: {title}\nRun knowledge doc: {root_doc_id}\n"
                f"Content hash: {content_hash}"
            )
            comment = self.cafe.comment(
                self.card_id, body, f"knowledge:{bound_run_id}:{operation_hash}",
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
            source["evidence_refs"][0]
            for source in (child, index, comment) if source is not None
        ]
        try:
            evidence_refs = validate_evidence_refs(evidence_refs)
        except ValueError:
            return _failure("KNOWLEDGE_EVIDENCE_INVALID")
        knowledge_fields = {
            "artifact_hash": content_hash,
            "child_doc_id": child["doc_id"] if child else None,
            "child_url": child["url"] if child else None,
            "child_version": child["version"] if child else None,
            "index_doc_id": index["doc_id"] if index else None,
            "index_version": index["version"] if index else None,
            "comment_id": str(comment["comment_id"]) if comment else None,
        }
        response = {
            "ok": True, "reason_code": "OK", "schema_version": "1", "run_id": bound_run_id,
            "scope": scope, **knowledge_fields, "evidence_refs": evidence_refs,
        }
        self.state.receipt(
            intent["intent_id"],
            {"ok": True, "schema_version": "1", **knowledge_fields},
            evidence_refs,
        )
        self.state.save_idempotency_result(result_key, response)
        return response

    # Refusals from the create step that mean no document was written. Anything else —
    # a verification mismatch, an unknown result — may have left a document behind and
    # must stay open for recovery to query.
    _NOTHING_WRITTEN = frozenset({"KU_IMMUTABLE_CONFLICT", "INVALID_INPUT"})

    def _settled(self, intent: dict[str, Any], failure: dict[str, Any]) -> dict[str, Any]:
        """Close this operation's intent when the create step provably wrote nothing.

        A re-cut phase reuses its predecessor's title, which KU refuses because a phase
        document is immutable. Nothing is created in that case, so leaving the intent
        open would park the whole run in RECOVERY_REQUIRED over an operation that did
        not happen. A pending lower intent still keeps this one open: the sub-operation
        may yet reconcile, and a receipt is write-once, so a failure recorded over it
        would make the eventual success unrecordable.
        """
        if _reason(failure, "") not in self._NOTHING_WRITTEN:
            return failure
        if self._pending_lower_intent(intent["run_id"], intent["intent_id"]) is not None:
            return failure
        self.state.receipt(
            intent["intent_id"],
            {"ok": False, "reason_code": _reason(failure, "KNOWLEDGE_PUBLISH_INCOMPLETE")},
            [],
        )
        return failure

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
        markdown = _run_root_markdown(self.run_root_title, run_id, self.card_id)
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
        # A phase document is read by people, so `markdown` may be a rendered view.
        # What is hashed stays the canonical JSON, and the rendered document has to
        # carry it verbatim, otherwise the published page and the artifact could drift.
        # A change set's patches are payload sized, so the appendix carries them elided
        # behind their own sha256 — `canonical_appendix` is a pure function of the
        # canonical bytes, so requiring exactly its output keeps the same anti-drift
        # guarantee without putting a 484 KB diff on the page.
        canonical = artifact.get("canonical")
        if canonical is not None:
            if not isinstance(canonical, str) or not canonical:
                return _failure("INVALID_INPUT")
            if artifact["content_hash"] != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
                return _failure("ARTIFACT_HASH_MISMATCH")
            if artifact["markdown"].count(canonical_appendix(canonical)) != 1:
                return _failure("ARTIFACT_HASH_MISMATCH")
            return None
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


def _run_root_markdown(title: str, run_id: str, card_id: str) -> str:
    """The run root is a directory people read, so it says what it is.

    Phase entries are appended after the last block, so the section note below is
    the anchor the first entry lands after.
    """
    return (
        f"# {title}\n\n"
        f"本文档由 tom-autodev 自动维护，收录 iCafe 卡片 {card_id} 在本次运行中产出的阶段文档。\n\n"
        f"* iCafe 卡片：{card_id}\n"
        f"* 运行编号：{run_id}\n\n"
        "## 阶段产物\n\n"
        "每个阶段完成后在下面追加一条。条目后面的 `tom-autodev-index` 注释与文末的 "
        "`tom-autodev-run-root` 注释是校验标记，工具靠它们核对文档身份与产物哈希，请不要手工修改。"
    )


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
