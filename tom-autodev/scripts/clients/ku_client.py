from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from cli_transport import CliTransport, CliTransportError
from state_store import StateStore


DEFAULT_KU_BINARY = "/Users/tom/.comate/skills/.system/ku-doc-manage/bin/ku"
_COMPONENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_UNKNOWN_REASONS = frozenset({"CLI_TIMEOUT", "CLI_PROCESS_FAILED", "CLI_INVALID_JSON"})


class KuClient:
    def __init__(
        self,
        *,
        transport: Any | None = None,
        state_store: StateStore | None = None,
        run_id: str | None = None,
        repo_id: str,
        username: str | None = None,
        binary: str = DEFAULT_KU_BINARY,
    ):
        self.username = username or os.environ.get("BAIDU_CC_USERNAME") or os.environ.get("COMATE_USERNAME")
        self.transport = transport or CliTransport(
            environment={"BAIDU_CC_USERNAME": self.username or ""}
        )
        self.state = state_store
        self.run_id = run_id
        self.repo_id = repo_id
        self.binary = binary

    @staticmethod
    def marked_markdown(parent_doc_id: str, title: str, markdown: str) -> str:
        return (
            f"{markdown.rstrip()}\n\n"
            f"<!-- tom-autodev-artifact:{_identity(parent_doc_id, title)}:{_content_hash(markdown)} -->"
        )

    @staticmethod
    def index_marker(
        root_doc_id: str,
        title: str,
        doc_id: str,
        content_hash: str,
    ) -> str:
        return f"<!-- tom-autodev-index:{_identity(root_doc_id, title)}:{doc_id}:{content_hash} -->"

    @classmethod
    def index_entry_markdown(cls, root_doc_id: str, entry: dict[str, Any]) -> str:
        marker = cls.index_marker(
            root_doc_id, entry["title"], entry["doc_id"], entry["content_hash"]
        )
        return (
            f"- [{entry['title']}]({entry['url']}) - `{entry['content_hash']}`\n"
            f"{marker}"
        )

    @staticmethod
    def run_root_markdown(parent_doc_id: str, title: str, markdown: str) -> str:
        marker = f"<!-- tom-autodev-run-root:{_identity(parent_doc_id, title)} -->"
        return f"{markdown.rstrip()}\n\n{marker}"

    def ensure_run_root(
        self, parent_doc_id: str, title: str, markdown: str
    ) -> dict[str, Any]:
        persistence = self._persistence()
        if persistence is not None:
            return persistence
        if not all(isinstance(value, str) and value for value in (parent_doc_id, title, markdown)):
            return _failure("INVALID_INPUT")
        initial = self.run_root_markdown(parent_doc_id, title, markdown)
        marker = initial.rsplit("\n", 1)[-1]
        key = f"ku-run-root:{parent_doc_id}:{_identity(parent_doc_id, title)}"
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            response = completed["receipt"]["response"]
            if not response.get("ok"):
                return response
            remote = self._query_document(str(response.get("doc_id", "")))
            if not remote.get("ok") or remote.get("text", "").count(marker) != 1:
                return _failure("KU_RUN_ROOT_VERIFICATION_FAILED")
            publish_key = f"ku-publish:{response['doc_id']}:{_content_hash(initial)}"
            published = self.state.result_by_idempotency_key(publish_key)
            if published is None:
                publish = self._publish(
                    response["doc_id"],
                    _content_hash(initial),
                    verified=remote,
                    expected_text=initial,
                )
                if not publish["ok"]:
                    return publish
                return self._root_result(response, publish)
            if not published["receipt"]["response"].get("ok"):
                return published["receipt"]["response"]
            return self._root_result(response, published["receipt"]["response"])

        intent = self.state.intent_by_idempotency_key(key)
        discovered = self._discover_run_root(parent_doc_id, title, marker)
        if intent is not None:
            if not discovered["ok"]:
                if discovered["reason_code"] == "KU_CHILD_NOT_FOUND":
                    return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
                return discovered
            created = self._persist_create_receipt(intent, discovered, _content_hash(initial))
            publish = self._publish(
                created["doc_id"],
                _content_hash(initial),
                verified=discovered,
                expected_text=initial,
            )
            return publish if not publish["ok"] else self._root_result(created, publish)
        if discovered["ok"]:
            intent = self.state.intent(
                self.run_id,
                "ku.run-root.create.reconcile",
                key,
                self._create_payload(parent_doc_id, title, _content_hash(initial)),
            )
            created = self._persist_create_receipt(intent, discovered, _content_hash(initial))
            publish = self._publish(
                created["doc_id"],
                _content_hash(initial),
                verified=discovered,
                expected_text=initial,
            )
            return publish if not publish["ok"] else self._root_result(created, publish)
        if discovered["reason_code"] != "KU_CHILD_NOT_FOUND":
            return discovered

        intent = self.state.intent(
            self.run_id,
            "ku.run-root.create",
            key,
            self._create_payload(parent_doc_id, title, _content_hash(initial)),
        )
        created_call = self._invoke(
            [
                self.binary,
                "create-doc",
                "--repo-id",
                self.repo_id,
                "--parent-doc-id",
                parent_doc_id,
                "--username",
                self.username or "",
                "--title",
                title,
                "--content",
                initial,
            ]
        )
        if not created_call["ok"]:
            if not _unknown_result(created_call["reason_code"]):
                return self._persist_failure(intent, created_call["reason_code"])
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        info = created_call["payload"].get("result")
        if not self._valid_remote_info(info, expected_title=title):
            return _failure("KU_RUN_ROOT_VERIFICATION_FAILED", intent_id=intent["intent_id"])
        remote = self._query_document(info["docGuid"])
        if (
            not remote.get("ok")
            or remote.get("url") != info["url"]
            or remote.get("text") != initial
        ):
            return _failure("KU_RUN_ROOT_VERIFICATION_FAILED", intent_id=intent["intent_id"])
        created = self._persist_create_receipt(intent, remote, _content_hash(initial))
        publish = self._publish(
            created["doc_id"],
            _content_hash(initial),
            verified=remote,
            expected_text=initial,
        )
        return publish if not publish["ok"] else self._root_result(created, publish)

    def create_artifact(self, parent_doc_id: str, title: str, markdown: str) -> dict[str, Any]:
        persistence = self._persistence()
        if persistence is not None:
            return persistence
        if not all(isinstance(value, str) and value for value in (parent_doc_id, title, markdown)):
            return _failure("INVALID_INPUT")
        content_hash = _content_hash(markdown)
        marked = self.marked_markdown(parent_doc_id, title, markdown)
        create_key = self._create_key(parent_doc_id, title, content_hash)
        completed = self.state.result_by_idempotency_key(create_key)
        if completed is not None:
            response = completed["receipt"]["response"]
            if not response.get("ok"):
                return response
            verified = self._query_document(str(response.get("doc_id", "")))
            if not self._matching_child(verified, marked, response.get("url")):
                return _failure("KU_CREATE_VERIFICATION_FAILED")
            return self._complete_artifact(response, verified, content_hash, marked)

        intent = self.state.intent_by_idempotency_key(create_key)
        if intent is not None:
            reconciled = self._discover_child(parent_doc_id, title, marked)
            if not reconciled["ok"]:
                if reconciled["reason_code"] in {"KU_CHILD_NOT_FOUND", "KU_QUERY_UNKNOWN"}:
                    return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
                return reconciled
            created = self._persist_create_receipt(intent, reconciled, content_hash)
            return self._complete_artifact(created, reconciled, content_hash, marked)

        discovered = self._discover_child(parent_doc_id, title, marked)
        if not discovered["ok"] and discovered["reason_code"] not in {"KU_CHILD_NOT_FOUND"}:
            return discovered
        if discovered["ok"]:
            intent = self.state.intent(
                self.run_id,
                "ku.document.create.reconcile",
                create_key,
                self._create_payload(parent_doc_id, title, content_hash),
            )
            created = self._persist_create_receipt(intent, discovered, content_hash)
            return self._complete_artifact(created, discovered, content_hash, marked)

        intent = self.state.intent(
            self.run_id,
            "ku.document.create",
            create_key,
            self._create_payload(parent_doc_id, title, content_hash),
        )
        created_call = self._invoke(
            [
                self.binary,
                "create-doc",
                "--repo-id",
                self.repo_id,
                "--parent-doc-id",
                parent_doc_id,
                "--username",
                self.username or "",
                "--title",
                title,
                "--content",
                marked,
            ]
        )
        if not created_call["ok"]:
            if not _unknown_result(created_call["reason_code"]):
                return self._persist_failure(intent, created_call["reason_code"])
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        info = created_call["payload"].get("result")
        if not self._valid_remote_info(info, expected_title=title):
            return _failure("KU_CREATE_VERIFICATION_FAILED", intent_id=intent["intent_id"])
        verified = self._query_document(info["docGuid"])
        if not self._matching_child(verified, marked, info["url"]):
            return _failure("KU_CREATE_VERIFICATION_FAILED", intent_id=intent["intent_id"])
        created = self._persist_create_receipt(intent, verified, content_hash)
        return self._complete_artifact(created, verified, content_hash, marked)

    def update_index(self, doc_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        persistence = self._persistence()
        if persistence is not None:
            return persistence
        invalid = self._validate_entry(entry)
        if invalid is not None:
            return invalid
        current = self._query_document(doc_id)
        if not current["ok"]:
            return current
        entry_markdown = self.index_entry_markdown(doc_id, entry)
        marker = self.index_marker(
            doc_id, entry["title"], entry["doc_id"], entry["content_hash"]
        )
        state = _index_entry_state(current["text"], entry_markdown, marker)
        if state == "CONFLICT":
            return _failure("KU_INDEX_CONFLICT")
        edit_key = f"ku-index-edit:{doc_id}:{_content_hash(marker)}"
        completed = self.state.result_by_idempotency_key(edit_key)
        if completed is not None and not completed["receipt"]["response"].get("ok"):
            return completed["receipt"]["response"]
        pending = self.state.intent_by_idempotency_key(edit_key) if completed is None else None

        if state == "EXACT":
            if pending is not None:
                self._persist_edit_receipt(pending, current, entry_markdown)
            elif completed is None:
                reconciled = self.state.intent(
                    self.run_id,
                    "ku.index.edit.reconcile",
                    edit_key,
                    self._edit_payload(doc_id, entry, entry_markdown),
                )
                self._persist_edit_receipt(reconciled, current, entry_markdown)
            return self._publish(
                doc_id,
                current["content_hash"],
                verified=current,
                required_entry=entry_markdown,
            )

        if pending is not None:
            return _failure("QUERY_REQUIRED", intent_id=pending["intent_id"], retry_allowed=False)
        anchor = _last_markdown_block(current["text"])
        if anchor is None:
            return _failure("KU_INDEX_ANCHOR_MISSING")
        intent = self.state.intent(
            self.run_id,
            "ku.index.edit",
            edit_key,
            self._edit_payload(doc_id, entry, entry_markdown),
        )
        operation = {
            "mode": "insert_after",
            "selectionWithEllipsis": anchor,
            "markdown": entry_markdown,
        }
        edited = self._invoke(
            [
                self.binary,
                "edit-content",
                "--doc-id",
                doc_id,
                "--username",
                self.username or "",
                "--editor-mode",
                "mdsl",
                "--operation",
                json.dumps(operation, ensure_ascii=False, separators=(",", ":")),
            ]
        )
        if not edited["ok"]:
            if not _unknown_result(edited["reason_code"]):
                return self._persist_failure(intent, edited["reason_code"])
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        verified = self._query_document(doc_id)
        if not verified["ok"] or _index_entry_state(verified["text"], entry_markdown, marker) != "EXACT":
            return _failure("KU_INDEX_VERIFICATION_FAILED", intent_id=intent["intent_id"])
        self._persist_edit_receipt(intent, verified, entry_markdown)
        return self._publish(
            doc_id,
            verified["content_hash"],
            verified=verified,
            required_entry=entry_markdown,
        )

    def _complete_artifact(
        self,
        created: dict[str, Any],
        verified: dict[str, Any],
        content_hash: str,
        marked: str,
    ) -> dict[str, Any]:
        published = self._publish(
            created["doc_id"],
            content_hash,
            verified=verified,
            expected_text=marked,
        )
        if not published["ok"]:
            return published
        return {
            "ok": True,
            "reason_code": "OK",
            "duplicate": created.get("duplicate", False),
            "doc_id": created["doc_id"],
            "repo_id": self.repo_id,
            "url": created["url"],
            "version": published["version"],
            "content_hash": content_hash,
            "evidence_refs": published["evidence_refs"],
        }

    def _publish(
        self,
        doc_id: str,
        content_hash: str,
        *,
        verified: dict[str, Any] | None = None,
        expected_text: str | None = None,
        required_entry: str | None = None,
    ) -> dict[str, Any]:
        key = f"ku-publish:{doc_id}:{content_hash}"
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            response = completed["receipt"]["response"]
            if not response.get("ok"):
                return response
            if verified is None:
                verified = self._query_document(doc_id)
            if not self._publish_verifies(verified, expected_text, required_entry):
                return _failure("KU_PUBLISH_VERIFICATION_FAILED")
            return response

        pending = self.state.intent_by_idempotency_key(key)
        if pending is not None:
            if verified is None:
                verified = self._query_document(doc_id)
            if self._publish_verifies(verified, expected_text, required_entry):
                return self._persist_publish_receipt(
                    pending, doc_id, content_hash, verified["version"]
                )
            return _failure("QUERY_REQUIRED", intent_id=pending["intent_id"], retry_allowed=False)

        if verified is None:
            verified = self._query_document(doc_id)
        if self._publish_verifies(verified, expected_text, required_entry):
            reconciled = self.state.intent(
                self.run_id,
                "ku.document.publish.reconcile",
                key,
                {"doc_id": doc_id, "content_hash": content_hash},
            )
            return self._persist_publish_receipt(
                reconciled, doc_id, content_hash, verified["version"]
            )
        intent = self.state.intent(
            self.run_id,
            "ku.document.publish",
            key,
            {"doc_id": doc_id, "content_hash": content_hash},
        )
        published = self._invoke(
            [
                self.binary,
                "publish-doc",
                "--doc-id",
                doc_id,
                "--username",
                self.username or "",
            ]
        )
        if not published["ok"]:
            if not _unknown_result(published["reason_code"]):
                return self._persist_failure(intent, published["reason_code"])
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        remote = self._query_document(doc_id)
        if not self._publish_verifies(remote, expected_text, required_entry):
            return _failure("KU_PUBLISH_VERIFICATION_FAILED", intent_id=intent["intent_id"])
        return self._persist_publish_receipt(intent, doc_id, content_hash, remote["version"])

    def _discover_child(self, parent_doc_id: str, title: str, marked: str) -> dict[str, Any]:
        listing = self._query_repo(parent_doc_id)
        if not listing["ok"]:
            return listing
        candidates = [document for document in listing["documents"] if document["title"] == title]
        if not candidates:
            return _failure("KU_CHILD_NOT_FOUND")
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            remote = self._query_document(candidate["doc_id"])
            if not remote["ok"]:
                return remote
            if remote["text"] == marked and remote["url"] == candidate["url"]:
                matches.append(remote)
        if len(candidates) != 1 or len(matches) != 1:
            return _failure("KU_IMMUTABLE_CONFLICT")
        return matches[0]

    def _discover_run_root(
        self, parent_doc_id: str, title: str, marker: str
    ) -> dict[str, Any]:
        listing = self._query_repo(parent_doc_id)
        if not listing["ok"]:
            return listing
        candidates = [document for document in listing["documents"] if document["title"] == title]
        if not candidates:
            return _failure("KU_CHILD_NOT_FOUND")
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            remote = self._query_document(candidate["doc_id"])
            if not remote["ok"]:
                return remote
            if remote["url"] == candidate["url"] and remote["text"].count(marker) == 1:
                matches.append(remote)
        if len(candidates) != 1 or len(matches) != 1:
            return _failure("KU_RUN_ROOT_CONFLICT")
        return matches[0]

    def requirement_acceptance(
        self, parent_doc_id: str, *, title_prefix: str = "\u9700\u6c42\u5206\u6790"
    ) -> dict[str, Any]:
        """Read acceptance criteria out of the newest analysis doc under a KU directory.

        iCafe cards do not always carry an acceptance property; for these projects the
        authoritative acceptance criteria live in the KU requirement-analysis document.
        The caller stays responsible for recording provenance on the snapshot.
        """
        listing = self._query_repo(parent_doc_id)
        if not listing["ok"]:
            return listing
        candidates = [
            document
            for document in listing["documents"]
            if str(document.get("title", "")).startswith(title_prefix)
        ]
        if not candidates:
            return _failure("KU_ACCEPTANCE_DOC_NOT_FOUND")
        # A analysis entry may itself be a directory holding dated revisions, so expand one
        # level and try newest-published first.
        expanded: list[dict[str, Any]] = []
        for document in candidates:
            expanded.append(document)
            children = self._query_repo(document["doc_id"])
            if children["ok"]:
                expanded.extend(children["documents"])
        expanded.sort(key=lambda item: (item.get("publish_time") or "", item["title"]), reverse=True)
        for document in expanded:
            content = self._query_content(document["doc_id"])
            if not content["ok"]:
                continue
            items = _acceptance_items(content["text"])
            if not items:
                continue
            return {
                "ok": True,
                "reason_code": "OK",
                "acceptance": items,
                "source": {
                    "provider": "ku",
                    "repo_id": self.repo_id,
                    "doc_id": document["doc_id"],
                    "doc_title": document["title"],
                    "url": content["url"],
                    "content_hash": content["content_hash"],
                },
            }
        return _failure("KU_ACCEPTANCE_SECTION_NOT_FOUND")

    def _query_repo(self, parent_doc_id: str) -> dict[str, Any]:
        response = self._invoke(
            [
                self.binary,
                "query-repo",
                "--repo-id",
                self.repo_id,
                "--parent-doc-id",
                parent_doc_id,
                "--page-num",
                "1",
                "--page-size",
                "100",
            ]
        )
        if not response["ok"]:
            if _unknown_result(response["reason_code"]):
                return _failure("KU_QUERY_UNKNOWN")
            return response
        result = response["payload"].get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            return _failure("KU_QUERY_VERIFICATION_FAILED")
        documents: list[dict[str, Any]] = []
        for item in data:
            if not self._valid_remote_info(item, name_field="name"):
                return _failure("KU_QUERY_VERIFICATION_FAILED")
            documents.append(
                {
                    "doc_id": item["docGuid"],
                    "title": item["name"],
                    "url": item["url"],
                    "publish_time": item.get("publishTime") or "",
                }
            )
        total = result.get("total", len(data))
        if not isinstance(total, int) or total > len(data):
            return _failure("KU_QUERY_INCOMPLETE")
        return {"ok": True, "reason_code": "OK", "documents": documents}

    def _query_document(self, doc_id: str) -> dict[str, Any]:
        content = self._query_content(doc_id)
        if not content["ok"]:
            return content
        version = self._query_version(doc_id)
        if not version["ok"]:
            return version
        return {**content, "version": version["version"], "init_type": version["init_type"]}

    def _query_content(self, doc_id: str) -> dict[str, Any]:
        response = self._invoke(
            [
                self.binary,
                "query-content",
                "--doc-id",
                doc_id,
                "--protocol",
                "markdown",
                "--show-doc-info",
            ]
        )
        if not response["ok"]:
            return response
        result = response["payload"].get("result")
        # `--show-doc-info` nests the document metadata under `docInfo`; only `docGuid`,
        # `content` and `text` sit at the top level.
        info = result.get("docInfo") if isinstance(result, dict) else None
        identity = info if isinstance(info, dict) else result
        if not self._valid_remote_info(identity, expected_doc_id=doc_id, name_field="name"):
            return _failure("KU_QUERY_VERIFICATION_FAILED")
        text = result.get("text")
        if not isinstance(text, str):
            return _failure("KU_QUERY_VERIFICATION_FAILED")
        return {
            "ok": True,
            "reason_code": "OK",
            "doc_id": doc_id,
            "repo_id": self.repo_id,
            "url": identity["url"],
            "title": identity.get("name"),
            "text": text,
            "content_hash": _content_hash(text),
        }

    def _query_version(self, doc_id: str) -> dict[str, Any]:
        response = self._invoke(
            [
                self.binary,
                "query-version",
                "--doc-id",
                doc_id,
                "--page-num",
                "1",
                "--page-size",
                "1",
            ]
        )
        if not response["ok"]:
            return response
        result = response["payload"].get("result")
        data = result.get("data") if isinstance(result, dict) else None
        latest = data[0] if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
        if latest is None or latest.get("docGuid") != doc_id or latest.get("versionId") is None:
            return _failure("KU_VERSION_VERIFICATION_FAILED")
        version = str(latest["versionId"])
        init_type = latest.get("initType")
        if not re.fullmatch(_COMPONENT, version) or not isinstance(init_type, int) or isinstance(init_type, bool):
            return _failure("KU_VERSION_VERIFICATION_FAILED")
        return {
            "ok": True,
            "reason_code": "OK",
            "doc_id": doc_id,
            "version": version,
            "init_type": init_type,
        }

    def _invoke(self, argv: list[str]) -> dict[str, Any]:
        try:
            payload = self.transport.run(argv, business_field="returnCode")
        except CliTransportError as error:
            return _failure(error.reason_code, diagnostic=error.diagnostic)
        code = payload.get("returnCode")
        if code != 200 or payload.get("success") is False:
            return _failure(_business_reason(code))
        return {"ok": True, "reason_code": "OK", "payload": payload}

    def _matching_child(
        self, remote: dict[str, Any], marked: str, expected_url: Any
    ) -> bool:
        return (
            remote.get("ok") is True
            and remote.get("repo_id") == self.repo_id
            and remote.get("url") == expected_url
            and remote.get("text") == marked
            and remote.get("content_hash") == _content_hash(marked)
        )

    def _publish_verifies(
        self,
        remote: dict[str, Any],
        expected_text: str | None,
        required_entry: str | None,
    ) -> bool:
        if not remote.get("ok") or remote.get("init_type") != 0:
            return False
        if expected_text is not None and remote.get("text") != expected_text:
            return False
        if required_entry is not None and remote.get("text", "").count(required_entry) != 1:
            return False
        return True

    def _valid_remote_info(
        self,
        value: Any,
        *,
        expected_doc_id: str | None = None,
        expected_title: str | None = None,
        name_field: str = "title",
    ) -> bool:
        if not isinstance(value, dict):
            return False
        doc_id = value.get("docGuid")
        title = value.get(name_field)
        return (
            isinstance(doc_id, str)
            and bool(doc_id)
            and (expected_doc_id is None or doc_id == expected_doc_id)
            and value.get("repositoryGuid") == self.repo_id
            and _canonical_ku_url(value.get("url"), self.repo_id, doc_id)
            and (expected_title is None or title == expected_title)
        )

    def _persist_create_receipt(
        self,
        intent: dict[str, Any],
        remote: dict[str, Any],
        content_hash: str,
    ) -> dict[str, Any]:
        response = {
            "ok": True,
            "reason_code": "OK",
            "duplicate": intent["operation"].endswith("reconcile"),
            "doc_id": remote["doc_id"],
            "repo_id": self.repo_id,
            "url": remote["url"],
            "version": remote["version"],
            "content_hash": content_hash,
            "evidence_refs": [f"ku:{remote['doc_id']}/{remote['version']}"],
        }
        self.state.receipt(intent["intent_id"], response, response["evidence_refs"])
        return response

    def _persist_edit_receipt(
        self, intent: dict[str, Any], remote: dict[str, Any], entry_markdown: str
    ) -> dict[str, Any]:
        evidence = [f"ku:{remote['doc_id']}/{remote['version']}"]
        response = {
            "ok": True,
            "reason_code": "OK",
            "doc_id": remote["doc_id"],
            "version": remote["version"],
            "entry_hash": _content_hash(entry_markdown),
            "evidence_refs": evidence,
        }
        self.state.receipt(intent["intent_id"], response, evidence)
        return response

    def _persist_publish_receipt(
        self,
        intent: dict[str, Any],
        doc_id: str,
        content_hash: str,
        version: str,
    ) -> dict[str, Any]:
        evidence = [f"ku:{doc_id}/{version}"]
        response = {
            "ok": True,
            "reason_code": "OK",
            "doc_id": doc_id,
            "version": version,
            "content_hash": content_hash,
            "evidence_refs": evidence,
        }
        self.state.receipt(intent["intent_id"], response, evidence)
        return response

    def _persist_failure(self, intent: dict[str, Any], reason_code: str) -> dict[str, Any]:
        response = _failure(reason_code)
        self.state.receipt(intent["intent_id"], response, [])
        return response

    @staticmethod
    def _root_result(created: dict[str, Any], published: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "reason_code": "OK",
            "doc_id": created["doc_id"],
            "url": created["url"],
            "version": published["version"],
            "evidence_refs": published["evidence_refs"],
        }

    def _create_payload(self, parent_doc_id: str, title: str, content_hash: str) -> dict[str, Any]:
        return {
            "parent_doc_id": parent_doc_id,
            "repo_id": self.repo_id,
            "title_hash": _content_hash(title),
            "content_hash": content_hash,
        }

    @staticmethod
    def _edit_payload(
        doc_id: str, entry: dict[str, Any], entry_markdown: str
    ) -> dict[str, Any]:
        return {
            "doc_id": doc_id,
            "child_doc_id": entry["doc_id"],
            "entry_hash": _content_hash(entry_markdown),
            "content_hash": entry["content_hash"],
        }

    @staticmethod
    def _create_key(parent_doc_id: str, title: str, content_hash: str) -> str:
        return f"ku-create:{parent_doc_id}:{_identity(parent_doc_id, title)}:{content_hash}"

    def _validate_entry(self, entry: Any) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return _failure("INVALID_INPUT")
        for key in ("title", "doc_id", "url", "content_hash"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                return _failure("INVALID_INPUT")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", entry["content_hash"])
            or not _canonical_ku_url(entry["url"], self.repo_id, entry["doc_id"])
        ):
            return _failure("INVALID_INPUT")
        return None

    def _persistence(self) -> dict[str, Any] | None:
        if self.state is None or not isinstance(self.run_id, str) or not self.run_id:
            return _failure("PERSISTENCE_REQUIRED")
        if not self.repo_id or not self.username:
            return _failure("KU_CONFIG_INVALID")
        return None


def _failure(reason_code: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, **details}


def _acceptance_items(text: str) -> list[str]:
    """Split the acceptance section of a requirement-analysis doc into items.

    Matches the deepest heading whose text contains 验收 or "acceptance", then keeps the
    numbered or bulleted entries under it; if there are none, keeps the prose paragraphs.
    """
    match = re.search(r"^(#{1,6})\s*.*(?:\u9a8c\u6536|acceptance).*$", text, re.M | re.I)
    if match is None:
        return []
    body = text[match.end():]
    following = re.search(rf"^#{{1,{len(match.group(1))}}}\s", body, re.M)
    if following is not None:
        body = body[: following.start()]
    items = [
        re.sub(r"\s+", " ", entry).strip()
        for entry in re.split(r"(?m)^\s*(?:\d+[.)\uff09\u3001]|[-*+\u2022])\s+", body)[1:]
    ]
    items = [entry for entry in items if entry]
    if items:
        return items
    return [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in body.split("\n\n")
        if paragraph.strip()
    ]


def _identity(parent_doc_id: str, title: str) -> str:
    return hashlib.sha256(f"{parent_doc_id}\0{title}".encode("utf-8")).hexdigest()[:24]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_ku_url(value: Any, repo_id: str, doc_id: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    path = [component for component in parsed.path.split("/") if component]
    return (
        parsed.scheme == "https"
        and parsed.netloc == "ku.baidu-int.com"
        and not parsed.query
        and not parsed.fragment
        and len(path) >= 5
        and path[-2:] == [repo_id, doc_id]
    )


def _last_markdown_block(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _index_entry_state(text: str, entry_markdown: str, marker: str) -> str:
    marker_count = text.count(marker)
    entry_count = text.count(entry_markdown)
    if marker_count == 0 and entry_count == 0:
        return "ABSENT"
    if marker_count == 1 and entry_count == 1:
        return "EXACT"
    return "CONFLICT"


def _unknown_result(reason_code: str) -> bool:
    return reason_code in _UNKNOWN_REASONS


def _business_reason(code: Any) -> str:
    if code in {401, 40100}:
        return "AUTH_REQUIRED"
    if code in {403, 60414}:
        return "PERMISSION_DENIED"
    if code in {404, 20104}:
        return "OBJECT_NOT_FOUND"
    if code in {400, 601}:
        return "INVALID_INPUT"
    return "KU_BUSINESS_FAILURE"
