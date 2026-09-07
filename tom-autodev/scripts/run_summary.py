from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from approval_ledger import ApprovalLedger
from artifact_store import ArtifactStore
from persistence_policy import ensure_persistable
from schema_validator import validate_named_schema
from state_store import StateStore

_SECRET_NAME = r"authorization|api[\s_-]*key|token|credential|password|secret|access[_-]?key"
_QUOTED_SECRET = re.compile(r"(?ix)(?P<prefix>[\"']?(?:" + _SECRET_NAME + r")[\"']?\s*[:=]\s*)(?P<quote>[\"'])(?P<value>(?:\\.|(?!\2).)*)\2")
_BARE_SECRET = re.compile(r"(?ix)(?P<prefix>\b(?:bearer\s+|authorization\s*[:=]\s*(?:bearer\s+)?|api[\s_-]*key\s*[:=]\s*|token\s*[:=]?\s*|credential\s*[:=]\s*|password\s*[:=]\s*|secret\s*[:=]\s*))(?P<value>[^\s,;]+)")
_URL_SECRET = re.compile(r"(?ix)(?P<prefix>(?://[^/\s:@]+:)(?P<value>[^@\s/]+)(?=@)|(?P<query>[?&](?:" + _SECRET_NAME + r")=)(?P<qvalue>[^&#\s]+))")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_FORBIDDEN_TARGET = re.compile(r"(?i)(^|[._/-])(bgw|xflow|business|ipipe|pipeline|profile|projects?)([._/-]|$)")
_FORBIDDEN_COMMAND = re.compile(r"(?i)\b(make|cmake|ninja|bazel|docker|ncs|pytest|ctest|go\s+test|cargo\s+test|npm\s+test|gradle|mvn|release|simulator|regression|integration)\b")
_SUMMARY_EXCLUDED_KINDS = {"run-summary", "optimization-proposal", "optimization-result"}


class RunSummary:
    def __init__(self, state_store: StateStore, artifact_store: ArtifactStore, approval_ledger: ApprovalLedger, *, knowledge_sync: Any | None = None, control_root: Path | str | None = None, validation_runner: Callable[[list[str], Path], dict[str, Any]] | None = None):
        self.state, self.artifacts, self.approvals = state_store, artifact_store, approval_ledger
        self.knowledge_sync = knowledge_sync
        self.control_root = Path(control_root or Path(__file__).resolve().parents[1]).resolve()
        self.validation_runner = validation_runner or self._run_validation

    def build(self, run_id: str) -> dict[str, Any]:
        events = self.state.events(run_id)
        if not events: return {"ok": False, "reason_code": "RUN_NOT_FOUND", "run_id": run_id}
        approvals, receipts = self.approvals.for_run(run_id), self.state.external_results(run_id)
        artifacts = [item for item in self.artifacts.artifacts_for_run(run_id) if item.get("kind") not in _SUMMARY_EXCLUDED_KINDS]
        pending = self.state.pending_intents(run_id)
        terminal, failures = events[-1]["state"], self._failure_groups(events, receipts, pending)
        timeout = any(row.get("effective_decision") == "TIMEOUT" for row in approvals)
        outcome = "TIMEOUT" if timeout else ("SUCCESS" if terminal == "RELEASE_SUCCESS" else ("FAILED" if failures or terminal in {"STOPPED", "DIAGNOSE"} else "IN_PROGRESS"))
        value = {"run_id": run_id, "schema_version": "1", "terminal_state": terminal, "outcome": outcome,
            "metrics": {"event_count": len(events), "artifact_count": len(artifacts), "valid_artifact_count": sum(bool(x.get("valid")) for x in artifacts), "external_receipt_count": len(receipts), "unreconciled_intent_count": len(pending)},
            "approval_metrics": {"approved": sum(x.get("effective_decision") == "APPROVE" for x in approvals), "rejected": sum(x.get("effective_decision") == "REJECT" for x in approvals), "pending": sum(x.get("effective_decision") is None for x in approvals), "timed_out": sum(x.get("effective_decision") == "TIMEOUT" for x in approvals)},
            "failure_groups": failures, "collaboration_receipt_count": sum(x["intent"]["operation"].startswith(("collaboration.", "infoflow.group.")) for x in receipts), "pipeline_evidence_count": sum(x["intent"]["operation"].startswith("ipipe.") for x in receipts),
            "artifact_integrity_failures": [{"artifact_id": x.get("artifact_id"), "reason_code": x.get("reason_code")} for x in artifacts if not x.get("valid")]}
        value["content_hash"] = _hash(value)
        archived = self.artifacts.put(run_id, "run-summary", _canonical(value).encode(), {"content_hash": value["content_hash"], "schema_version": "1"})
        return {"ok": True, "reason_code": "OK", **value, "artifact_id": archived["artifact_id"]}

    def propose(self, summary: dict[str, Any], allowed_roots: list[Path]) -> dict[str, Any]:
        loaded, error = self._verified_summary(summary)
        if error: return {"ok": False, "reason_code": error}
        if self.knowledge_sync is None: return {"ok": False, "reason_code": "KNOWLEDGE_SYNC_REQUIRED", "run_id": loaded["run_id"]}
        roots = self._allowed_roots(allowed_roots)
        if roots is None: return {"ok": False, "reason_code": "G10_ALLOWED_ROOT_INVALID", "run_id": loaded["run_id"]}
        raw = self._candidate_from_events(loaded["run_id"])
        candidate, error = self._normalize_candidate(raw, roots)
        if error: return {"ok": False, "reason_code": error, "run_id": loaded["run_id"]}
        candidate_hash = _hash(candidate); proposal_id = _proposal_id(loaded["run_id"], candidate_hash)
        proposal = {"schema_version": "1", "proposal_id": proposal_id, "run_id": loaded["run_id"], "summary_hash": loaded["content_hash"], "summary_artifact_id": summary["artifact_id"], "candidate_hash": candidate_hash, "candidate": candidate, "candidate_diff": _candidate_diff(candidate), "allowed_roots": [str(x) for x in roots], "approval_gate": "G10", "expected_benefit": candidate["expected_benefit"], "risk": candidate["risk"], "rollback": candidate["rollback"], "evidence": {"failure_groups": loaded["failure_groups"], "summary_artifact_id": summary["artifact_id"]}}
        proposal["envelope_hash"] = _hash(proposal)
        # The shape check has to happen before the row is written, because the row is
        # immutable and keyed on `envelope_hash`: a malformed proposal saved once is a
        # proposal that can only ever be re-saved identically. `save_optimization_proposal`
        # checks four string fields, which is enough to index it and not enough to know
        # G10 can act on it.
        if validate_named_schema(proposal, "optimization-proposal"):
            return {"ok": False, "reason_code": "G10_PROPOSAL_INVALID", "run_id": loaded["run_id"]}
        try: stored = self.state.save_optimization_proposal(proposal)
        except ValueError: return {"ok": False, "reason_code": "G10_PROPOSAL_INVALID", "run_id": loaded["run_id"]}
        result = self._archive(loaded["run_id"], "G10 Optimization Proposal", proposal)
        if not result.get("ok"):
            failure = {"ok": False, "reason_code": "G10_ARCHIVE_FAILED", "proposal_id": proposal_id, "candidate_hash": candidate_hash}
            self.state.update_optimization_proposal(proposal_id, "ARCHIVE_FAILED", failure)
            return failure
        try: self.state.mark_optimization_archived(proposal_id, result)
        except ValueError: return {"ok": False, "reason_code": "G10_ARCHIVE_STATE_INVALID", "proposal_id": proposal_id}
        return {"ok": True, "reason_code": "OK", **proposal, "knowledge": result}

    def apply(self, proposal_id: str, approval_id: str) -> dict[str, Any]:
        stored = self.state.optimization_proposal(proposal_id)
        proposal, error = self._verified_proposal(stored, proposal_id)
        if error: return {"ok": False, "reason_code": error, "proposal_id": proposal_id}
        if stored["status"] == "ARCHIVING": return {"ok": False, "reason_code": "G10_ARCHIVING", "proposal_id": proposal_id}
        if stored["status"] in {"RESULT_ARCHIVING", "ARCHIVE_PENDING"}:
            return self._recover_result_archive(stored, proposal)
        if stored["status"] in {"RECOVERY_REQUIRED", "ROLLBACK_FAILED"}:
            return self._retry_recovery(stored, proposal)
        if stored["status"] in {"APPLIED", "ROLLED_BACK", "REJECTED", "ARCHIVE_FAILED"}:
            return self._terminal_replay(stored, approval_id)
        if stored["status"] == "APPLYING": return self._recover_or_wait(stored, approval_id)
        if self.knowledge_sync is None: return {"ok": False, "reason_code": "KNOWLEDGE_SYNC_REQUIRED", "proposal_id": proposal_id}
        if not self._receipt_valid(proposal["run_id"], "G10 Optimization Proposal", proposal, stored.get("proposal_receipt") or stored.get("archive_receipt")):
            return {"ok": False, "reason_code": "G10_ARCHIVE_RECEIPT_REQUIRED", "proposal_id": proposal_id}
        approval_error = self._approval_error(self.approvals.get(approval_id), proposal)
        if approval_error:
            if approval_error == "G10_REJECTED":
                rejected = {"ok": False, "reason_code": approval_error, "proposal_id": proposal_id, "approval_id": approval_id}
                if self._archive(proposal["run_id"], "G10 Optimization Result", rejected).get("ok"):
                    self.state.update_optimization_proposal(proposal_id, "REJECTED", rejected, approval_id)
                else:
                    self.state.update_optimization_proposal(proposal_id, "ARCHIVE_FAILED", {"ok": False, "reason_code": "G10_ARCHIVE_FAILED", "proposal_id": proposal_id, "approval_id": approval_id}, approval_id)
                    return {"ok": False, "reason_code": "G10_ARCHIVE_FAILED", "proposal_id": proposal_id}
            return {"ok": False, "reason_code": approval_error, "proposal_id": proposal_id}
        roots = self._allowed_roots([Path(x) for x in proposal["allowed_roots"]])
        candidate, error = self._normalize_candidate(proposal["candidate"], roots or [], expected=True)
        if roots is None or error or _hash(candidate) != proposal["candidate_hash"]: return {"ok": False, "reason_code": error or "G10_CANDIDATE_HASH_MISMATCH", "proposal_id": proposal_id}
        prepared = self._journal(candidate, roots, proposal=proposal, approval_id=approval_id, pin=True)
        journal, error, pins = prepared
        if error: return {"ok": False, "reason_code": error, "proposal_id": proposal_id}
        claim = self.state.claim_optimization_apply(proposal_id, approval_id, journal)
        if claim["status"] == "JOURNAL_INVALID": return {"ok": False, "reason_code": "G10_JOURNAL_INVALID", "proposal_id": proposal_id}
        if claim["status"] != "CLAIMED": return self._recover_or_wait(claim.get("proposal", stored), approval_id)
        token = claim["owner_token"]
        journal = claim["proposal"]["apply_journal"]
        try:
            for item, entry, pin in zip(candidate["target_files"], journal["entries"], pins):
                _write_pinned(pin, entry, item["content"].encode())
            validation = self.validation_runner(candidate["verification_commands"], self.control_root)
            if not isinstance(validation, dict) or not validation.get("ok"): raise _ApplyFailure("G10_VALIDATION_FAILED_ROLLED_BACK")
            result = {"ok": True, "reason_code": "OK", "proposal_id": proposal_id, "candidate_hash": proposal["candidate_hash"], "approval_id": approval_id}
            self.state.begin_optimization_result_archive(proposal_id, token, result, "APPLIED")
            return self._publish_result_archive(proposal, result)
        except Exception as failure:
            rollback_ok = self._rollback_journal(journal)
            if not rollback_ok:
                result = {"ok": False, "reason_code": "G10_ROLLBACK_RECOVERY_REQUIRED", "proposal_id": proposal_id, "approval_id": approval_id}
                self.state.mark_optimization_recovery_required(proposal_id, token, result)
                return result
            reason = failure.reason if isinstance(failure, _ApplyFailure) else "G10_APPLY_FAILED_ROLLED_BACK"
            result = {"ok": False, "reason_code": reason, "proposal_id": proposal_id, "approval_id": approval_id}
            self.state.begin_optimization_result_archive(proposal_id, token, result, "ROLLED_BACK")
            return self._publish_result_archive(proposal, result)
        finally:
            for pin in pins:
                try: os.close(pin["parent_fd"])
                except OSError: pass

    def heartbeat(self, proposal_id: str, owner_token: str) -> dict[str, Any]:
        """Renew a claimed G10 apply lease without exposing the state-store API."""
        try:
            return self.state.heartbeat_optimization_apply(proposal_id, owner_token)
        except ValueError as error:
            return {"ok": False, "reason_code": str(error), "proposal_id": proposal_id}

    def _verified_summary(self, supplied: Any) -> tuple[dict[str, Any], str | None]:
        if not isinstance(supplied, dict) or not isinstance(supplied.get("artifact_id"), str): return {}, "RUN_SUMMARY_ARTIFACT_REQUIRED"
        item = self.artifacts.get(supplied["artifact_id"])
        if not item.get("valid") or item.get("kind") != "run-summary": return {}, "RUN_SUMMARY_ARTIFACT_INVALID"
        try: value = json.loads(item["content"])
        except Exception: return {}, "RUN_SUMMARY_ARTIFACT_INVALID"
        expected = {k: v for k, v in value.items() if k != "content_hash"}
        if value.get("content_hash") != _hash(expected) or supplied.get("run_id") != value.get("run_id") or supplied.get("content_hash") != value.get("content_hash") or item.get("run_id") != value.get("run_id"): return {}, "RUN_SUMMARY_ARTIFACT_INVALID"
        return value, None

    def _verified_proposal(self, stored: Any, proposal_id: str) -> tuple[dict[str, Any], str | None]:
        if not isinstance(stored, dict): return {}, "OPTIMIZATION_PROPOSAL_NOT_FOUND"
        p = stored.get("proposal")
        if not isinstance(p, dict) or p.get("envelope_hash") != stored.get("envelope_hash"): return {}, "G10_PROPOSAL_TAMPERED"
        envelope = {k: v for k, v in p.items() if k != "envelope_hash"}
        if _hash(envelope) != p["envelope_hash"] or p.get("proposal_id") != proposal_id or p.get("candidate_hash") != stored.get("candidate_hash") or p.get("run_id") != stored.get("run_id") or _proposal_id(p["run_id"], p["candidate_hash"]) != proposal_id: return {}, "G10_PROPOSAL_TAMPERED"
        return p, None

    def _terminal_replay(self, stored: dict[str, Any], approval_id: str) -> dict[str, Any]:
        result = stored.get("result") or {"ok": False, "reason_code": "OPTIMIZATION_RESULT_MISSING"}
        if stored.get("status") == "ARCHIVE_FAILED": return result
        proposal, error = self._verified_proposal(stored, stored["proposal_id"])
        approval = self.approvals.get(approval_id)
        reject_replay = stored.get("status") == "REJECTED" and isinstance(approval, dict) and approval.get("run_id") == proposal.get("run_id") and approval.get("action") == "G10" and approval.get("input_hash") == proposal.get("candidate_hash") and approval.get("effective_decision") == "REJECT"
        if error or stored.get("approval_id") != approval_id or (not reject_replay and self._approval_error(approval, proposal)): return {"ok": False, "reason_code": "G10_REPLAY_AUTH_REQUIRED", "proposal_id": stored["proposal_id"]}
        return result

    def _recover_or_wait(self, stored: dict[str, Any], approval_id: str) -> dict[str, Any]:
        if not _lease_expired(stored.get("lease_expires_at")):
            return {"ok": False, "reason_code": "G10_APPLY_IN_PROGRESS", "proposal_id": stored["proposal_id"]}
        if _pid_live(stored.get("owner_pid")): return {"ok": False, "reason_code": "G10_APPLY_IN_PROGRESS", "proposal_id": stored["proposal_id"]}
        proposal, error = self._verified_proposal(stored, stored["proposal_id"])
        journal = stored.get("apply_journal")
        if error or not self._journal_valid(journal, proposal, stored):
            return {"ok": False, "reason_code": "G10_RECOVERY_REQUIRED", "proposal_id": stored["proposal_id"]}
        if not self._rollback_journal(journal):
            result = {"ok": False, "reason_code": "G10_ROLLBACK_RECOVERY_REQUIRED", "proposal_id": stored["proposal_id"], "approval_id": stored.get("approval_id")}
            self.state.mark_optimization_recovery_required(stored["proposal_id"], None, result)
            return result
        result = {"ok": False, "reason_code": "G10_INTERRUPTED_ROLLED_BACK", "proposal_id": stored["proposal_id"], "approval_id": stored.get("approval_id")}
        self.state.mark_optimization_recovery_required(stored["proposal_id"], None, result)
        return self._retry_recovery(self.state.optimization_proposal(stored["proposal_id"]), proposal)

    def _retry_recovery(self, stored: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
        journal = stored.get("apply_journal")
        if not self._journal_valid(journal, proposal, stored) or not self._rollback_journal(journal):
            result = {"ok": False, "reason_code": "G10_ROLLBACK_RECOVERY_REQUIRED", "proposal_id": stored["proposal_id"], "approval_id": stored.get("approval_id")}
            self.state.mark_optimization_recovery_required(stored["proposal_id"], None, result)
            return result
        prior = stored.get("result") or {}
        result = {"ok": False, "reason_code": "G10_ROLLBACK_RECOVERED", "proposal_id": stored["proposal_id"], "approval_id": stored.get("approval_id")}
        if prior.get("reason_code") == "G10_INTERRUPTED_ROLLED_BACK": result["reason_code"] = "G10_INTERRUPTED_ROLLED_BACK"
        self.state.begin_optimization_result_archive(stored["proposal_id"], stored.get("owner_token"), result, "ROLLED_BACK")
        return self._publish_result_archive(proposal, result)

    def _recover_result_archive(self, stored: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
        result = stored.get("result")
        if not isinstance(result, dict):
            return {"ok": False, "reason_code": "G10_RESULT_ARCHIVE_RECOVERY_REQUIRED", "proposal_id": stored["proposal_id"]}
        return self._publish_result_archive(proposal, result)

    def _publish_result_archive(self, proposal: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        receipt = self._archive(proposal["run_id"], "G10 Optimization Result", result)
        if self._receipt_valid(proposal["run_id"], "G10 Optimization Result", result, receipt):
            self.state.complete_optimization_result_archive(proposal["proposal_id"], receipt)
            return result
        self.state.mark_optimization_result_archive_pending(proposal["proposal_id"], result)
        return {**result, "ok": False, "reason_code": "G10_RESULT_ARCHIVE_PENDING"}

    def _failure_groups(self, events: list[dict[str, Any]], receipts: list[dict[str, Any]], pending: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}; evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else payload
            reason = payload.get("reason_code") or evidence.get("reason_code") or evidence.get("classification")
            if isinstance(reason, str) and ("FAIL" in reason or "TIMEOUT" in reason or "ERROR" in reason): self._add_failure(groups, evidence.get("failure_signature") or payload.get("failure_signature"), reason, evidence.get("message") or payload.get("message"))
        for item in receipts:
            response = item["receipt"].get("response")
            if isinstance(response, dict) and response.get("ok") is False: self._add_failure(groups, response.get("failure_signature"), response.get("reason_code"), response.get("message"))
        # A remote step that fails its read-back verification writes no receipt on
        # purpose, so recovery can still query it, and it never reaches a state event
        # either. Counting the open intents is the only way those failures appear here
        # at all: without them the summary hides precisely the failures that stalled
        # the run, and G10 gets evidence that says the run was clean.
        for item in pending or []:
            self._add_failure(groups, f"UNRECONCILED_INTENT:{item['operation']}", "EXTERNAL_INTENT_UNRECONCILED", item["intent_id"])
        return sorted(groups.values(), key=lambda x: x["signature"])

    def _add_failure(self, groups: dict[str, dict[str, Any]], signature: Any, reason: Any, message: Any) -> None:
        reason, signature = _redact(str(reason or "UNKNOWN_FAILURE")), _redact(str(signature or reason or "UNKNOWN_FAILURE")); row = groups.setdefault(signature, {"signature": signature, "reason_code": reason, "count": 0, "examples": []}); row["count"] += 1
        if isinstance(message, str) and len(row["examples"]) < 3: row["examples"].append(_redact(message))

    def _candidate_from_events(self, run_id: str) -> dict[str, Any] | None:
        for event in reversed(self.state.events(run_id)):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}; evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else payload
            value = evidence.get("optimization_candidate")
            if isinstance(value, dict) and value.get("schema_version") == "1": return value
        return None

    def _allowed_roots(self, roots: list[Path]) -> list[Path] | None:
        if not isinstance(roots, list) or not roots: return None
        result=[]
        for item in roots:
            raw = Path(item).expanduser()
            if raw.is_symlink() or _has_symlink_ancestor(raw, self.control_root): return None
            try: resolved=raw.resolve(strict=True); resolved.relative_to(self.control_root)
            except (OSError, ValueError): return None
            if resolved not in result: result.append(resolved)
        return result

    def _normalize_candidate(self, raw: Any, roots: list[Path], expected: bool=False) -> tuple[dict[str, Any], str | None]:
        if not isinstance(raw, dict) or not roots: return {}, "G10_CANDIDATE_INVALID"
        required=("root_cause","expected_benefit","risk","rollback","target_files","verification_commands")
        if any(not isinstance(raw.get(k), str) or not raw[k] for k in required[:4]) or not isinstance(raw.get("target_files"), list) or not raw["target_files"] or not isinstance(raw.get("verification_commands"), list) or not raw["verification_commands"]: return {}, "G10_CANDIDATE_INVALID"
        files=[]
        for entry in raw["target_files"]:
            if not isinstance(entry,dict) or not isinstance(entry.get("path"),str) or not isinstance(entry.get("content"),str) or _has_secret(entry["content"]): return {}, "G10_CANDIDATE_SECRET_FORBIDDEN" if isinstance(entry,dict) else "G10_CANDIDATE_INVALID"
            rawpath=Path(entry["path"]).expanduser()
            if rawpath.is_symlink() or _has_symlink_ancestor(rawpath, self.control_root): return {}, "G10_TARGET_SYMLINK_FORBIDDEN"
            path=rawpath.resolve(strict=False)
            if _FORBIDDEN_TARGET.search(str(path)) or not any(_within(path,x) for x in roots): return {}, "G10_TARGET_FORBIDDEN"
            before=path.read_bytes() if path.exists() else b""; item={"path":str(path),"before_sha256":hashlib.sha256(before).hexdigest(),"content":entry["content"],"content_sha256":hashlib.sha256(entry["content"].encode()).hexdigest()}
            if expected and any(entry.get(k) != v for k,v in item.items()): return {}, "G10_CANDIDATE_HASH_MISMATCH"
            files.append(item)
        commands=raw["verification_commands"]
        if not all(isinstance(x,str) and _safe_validation_command(x) and not _FORBIDDEN_COMMAND.search(x) for x in commands): return {}, "G10_VALIDATION_FORBIDDEN"
        return {"root_cause":_redact(raw["root_cause"]),"expected_benefit":_redact(raw["expected_benefit"]),"risk":_redact(raw["risk"]),"rollback":_redact(raw["rollback"]),"target_files":files,"verification_commands":commands},None

    def _journal(self, candidate:dict[str,Any], roots:list[Path], *, proposal:dict[str,Any]|None=None, approval_id:str|None=None, pin:bool=False) -> Any:
        roots = self._allowed_roots(roots)
        if roots is None:
            return ({}, "G10_ALLOWED_ROOT_INVALID", []) if pin else ({}, "G10_ALLOWED_ROOT_INVALID")
        entries=[]; pins=[]
        try:
            for item in candidate["target_files"]:
                path=Path(item["path"]); opened=_open_pinned_parent(path, roots)
                data, mode, target_identity = _read_pinned_target(opened, item["before_sha256"])
                entry={"path":str(path),"existed":target_identity is not None,"bytes_b64":base64.b64encode(data).decode(),"sha256":hashlib.sha256(data).hexdigest(),"mode":mode,"root_dev":opened["root_dev"],"root_ino":opened["root_ino"],"parent_dev":opened["parent_dev"],"parent_ino":opened["parent_ino"],"target_dev":target_identity[0] if target_identity else None,"target_ino":target_identity[1] if target_identity else None}
                entries.append(entry)
                if pin: pins.append(opened)
                else: os.close(opened["parent_fd"])
        except (OSError,ValueError):
            for opened in pins:
                try: os.close(opened["parent_fd"])
                except OSError: pass
            return ({}, "G10_BASELINE_MISMATCH", []) if pin else ({}, "G10_BASELINE_MISMATCH")
        targets=[{"path":item["path"],"before_sha256":item["before_sha256"],"content_sha256":item["content_sha256"]} for item in candidate["target_files"]]
        journal={"schema_version":"2","proposal_id":proposal.get("proposal_id") if proposal else None,"run_id":proposal.get("run_id") if proposal else None,"candidate_hash":proposal.get("candidate_hash") if proposal else None,"approval_id":approval_id,"allowed_roots":[str(x) for x in roots],"target_identities":targets,"entries":entries}
        return (journal,None,pins) if pin else (journal,None)

    def _revalidate_target(self,item:dict[str,Any],roots:list[Path])->None:
        path=Path(item["path"])
        if path.is_symlink() or _has_symlink_ancestor(path,self.control_root) or not any(_within(path,x) for x in roots): raise ValueError("G10_TARGET_SYMLINK_FORBIDDEN")
        current=path.read_bytes() if path.exists() else b""
        if hashlib.sha256(current).hexdigest()!=item["before_sha256"]: raise ValueError("G10_BASELINE_MISMATCH")

    def _journal_valid(self, journal:Any, proposal:dict[str,Any], stored:dict[str,Any])->bool:
        try:
            if not isinstance(journal, dict) or journal.get("schema_version") != "2": return False
            expected={k:v for k,v in journal.items() if k!="journal_hash"}
            if journal.get("journal_hash") != _hash(expected): return False
            if any(journal.get(k) != proposal.get(k) for k in ("proposal_id", "run_id", "candidate_hash")): return False
            if journal.get("approval_id") != stored.get("approval_id") or journal.get("claim", {}).get("owner_proof") != hashlib.sha256(str(stored.get("owner_token", "")).encode()).hexdigest(): return False
            claim=journal.get("claim")
            if not isinstance(claim,dict) or claim.get("owner_pid") != stored.get("owner_pid") or not isinstance(claim.get("lease_expires_at"), str): return False
            roots=[str(x) for x in self._allowed_roots([Path(x) for x in journal.get("allowed_roots",[])]) or []]
            if roots != journal.get("allowed_roots") or roots != proposal.get("allowed_roots"): return False
            targets=[{"path":item["path"],"before_sha256":item["before_sha256"],"content_sha256":item["content_sha256"]} for item in proposal.get("candidate",{}).get("target_files",[])]
            if journal.get("target_identities") != targets or len(journal.get("entries",[])) != len(targets): return False
            for entry,target in zip(journal["entries"],targets):
                data=base64.b64decode(entry["bytes_b64"], validate=True)
                if entry.get("path") != target["path"] or entry.get("sha256") != hashlib.sha256(data).hexdigest() or entry["sha256"] != target["before_sha256"]: return False
            return True
        except (KeyError, TypeError, ValueError, OSError): return False

    def _rollback_journal(self,journal:dict[str,Any])->bool:
        try:
            roots=[Path(value) for value in journal.get("allowed_roots", [])]
            if not self._allowed_roots(roots): return False
            for item in reversed(journal.get("entries",[])):
                path=Path(item["path"]); data=base64.b64decode(item["bytes_b64"]); 
                if hashlib.sha256(data).hexdigest() != item.get("sha256") or not any(_within(path, root) for root in roots): return False
                opened=_open_pinned_parent(path, roots, expected=item)
                try:
                    if item["existed"]: _restore_pinned(opened,item,data)
                    else: _unlink_pinned(opened,item)
                finally: os.close(opened["parent_fd"])
            return True
        except Exception: return False

    def _approval_error(self,approval:Any,p:dict[str,Any])->str|None:
        if not isinstance(approval,dict) or approval.get("run_id")!=p["run_id"] or approval.get("action")!="G10": return "G10_APPROVAL_REQUIRED"
        if approval.get("input_hash")!=p["candidate_hash"]: return "G10_APPROVAL_HASH_MISMATCH"
        return "G10_REJECTED" if approval.get("effective_decision")=="REJECT" else (None if approval.get("effective_decision")=="APPROVE" else "G10_APPROVAL_REQUIRED")

    def _archive(self,run_id:str,title:str,value:dict[str,Any])->dict[str,Any]:
        if self.knowledge_sync is None:return {"ok":False,"reason_code":"KNOWLEDGE_SYNC_REQUIRED"}
        try:
            artifact=self._archive_artifact(title,value); result=self.knowledge_sync.publish_phase(run_id,artifact)
            return result if self._receipt_valid(run_id,title,value,result) else {"ok":False,"reason_code":"KNOWLEDGE_ARCHIVE_FAILED"}
        except Exception:return {"ok":False,"reason_code":"KNOWLEDGE_ARCHIVE_FAILED"}

    def _archive_artifact(self,title:str,value:dict[str,Any])->dict[str,Any]:
        markdown="# "+title+"\n\n```json\n"+_canonical(_redacted(value))+"\n```\n"
        return {"title":title,"markdown":markdown,"content_hash":hashlib.sha256(markdown.encode()).hexdigest()}

    def _receipt_valid(self,run_id:str,title:str,value:dict[str,Any],receipt:Any)->bool:
        if not isinstance(receipt,dict) or receipt.get("ok") is not True or receipt.get("reason_code") != "OK" or receipt.get("run_id") != run_id: return False
        artifact=self._archive_artifact(title,value)
        if receipt.get("artifact_hash") != artifact["content_hash"]: return False
        fields=("schema_version","child_doc_id","child_url","child_version","index_doc_id","index_version","comment_id")
        if any(not isinstance(receipt.get(field),str) or not receipt[field] for field in fields): return False
        if receipt["schema_version"] != "1" or not receipt["child_url"].startswith("https://ku.baidu-int.com/"): return False
        refs=[f"ku:{receipt['child_doc_id']}/{receipt['child_version']}",f"ku:{receipt['index_doc_id']}/{receipt['index_version']}",f"icafe:{self._requirement_id(run_id)}/{receipt['comment_id']}"]
        return receipt.get("evidence_refs") == refs

    def _requirement_id(self, run_id:str)->str:
        for event in self.state.events(run_id):
            value=(event.get("payload") or {}).get("requirement_id")
            if isinstance(value,str) and value: return value
        return "CARD-1"

    @staticmethod
    def _run_validation(commands:list[str],root:Path)->dict[str,Any]:
        try:
            for command in commands:
                if not _safe_validation_command(command): return {"ok":False,"reason_code":"VALIDATION_FORBIDDEN"}
                if subprocess.run(shlex.split(command),cwd=root,capture_output=True,text=True,timeout=60,check=False).returncode: return {"ok":False,"reason_code":"VALIDATION_FAILED"}
        except Exception:return {"ok":False,"reason_code":"VALIDATION_FAILED"}
        return {"ok":True,"reason_code":"OK"}


def _atomic_write(path:Path,data:bytes,mode:int|None=None,roots:list[Path]|None=None)->None:
    if roots: return _confined_atomic_write(path,data,mode,roots)
    path.parent.mkdir(parents=True,exist_ok=True); old=path.stat().st_mode if path.exists() else mode; fd,name=tempfile.mkstemp(prefix=".g10-",dir=path.parent); temp=Path(name)
    try:
        with os.fdopen(fd,"wb") as h:h.write(data);h.flush();os.fsync(h.fileno())
        if old is not None: os.chmod(temp,old)
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)

def _open_pinned_parent(path:Path, roots:list[Path], expected:dict[str,Any]|None=None)->dict[str,Any]:
    root=next((item for item in roots if _within(path,item)),None)
    if root is None: raise ValueError("G10_TARGET_FORBIDDEN")
    flags=os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_NOFOLLOW",0)
    root_fd=os.open(root,flags)
    parent_fd=root_fd
    try:
        root_stat=os.fstat(root_fd)
        for component in path.relative_to(root).parts[:-1]:
            next_fd=os.open(component,flags,dir_fd=parent_fd)
            if parent_fd != root_fd: os.close(parent_fd)
            parent_fd=next_fd
        parent_stat=os.fstat(parent_fd)
        value={"root":root,"path":path,"parent_fd":parent_fd,"name":path.name,"root_dev":root_stat.st_dev,"root_ino":root_stat.st_ino,"parent_dev":parent_stat.st_dev,"parent_ino":parent_stat.st_ino}
        if expected and any(value[key] != expected.get(key) for key in ("root_dev","root_ino","parent_dev","parent_ino")):
            raise ValueError("G10_PARENT_IDENTITY_CHANGED")
        return value
    except Exception:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)
        raise
    finally:
        if parent_fd != root_fd:
            # The root descriptor no longer participates after traversal.
            try: os.close(root_fd)
            except OSError: pass

def _read_pinned_target(pin:dict[str,Any], expected_hash:str)->tuple[bytes,int|None,tuple[int,int]|None]:
    try:
        fd=os.open(pin["name"],os.O_RDONLY|getattr(os,"O_NOFOLLOW",0),dir_fd=pin["parent_fd"])
    except FileNotFoundError:
        data=b""; mode=None; identity=None
    else:
        try:
            stat=os.fstat(fd); data=b""
            while True:
                block=os.read(fd, 1024*1024)
                if not block: break
                data += block
            mode=stat.st_mode; identity=(stat.st_dev,stat.st_ino)
        finally: os.close(fd)
    if hashlib.sha256(data).hexdigest()!=expected_hash: raise ValueError("G10_BASELINE_MISMATCH")
    return data,mode,identity

def _write_pinned(pin:dict[str,Any], entry:dict[str,Any], data:bytes)->None:
    stat=os.fstat(pin["parent_fd"])
    if stat.st_dev != entry["parent_dev"] or stat.st_ino != entry["parent_ino"]: raise ValueError("G10_PARENT_IDENTITY_CHANGED")
    # Ensure the path has not been rebound to a different real directory before
    # mutating through the retained descriptor.
    reopened=_open_pinned_parent(pin["path"],[pin["root"]],expected=entry)
    os.close(reopened["parent_fd"])
    _read_pinned_target(pin,entry["sha256"])
    temp=f".g10-{os.getpid()}-{hashlib.sha256(data).hexdigest()[:12]}"
    out=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600,dir_fd=pin["parent_fd"])
    try:
        os.write(out,data); os.fsync(out)
        if entry.get("mode") is not None: os.fchmod(out,entry["mode"])
    finally: os.close(out)
    os.replace(temp,pin["name"],src_dir_fd=pin["parent_fd"],dst_dir_fd=pin["parent_fd"])

def _restore_pinned(pin:dict[str,Any], entry:dict[str,Any], data:bytes)->None:
    _write_pinned(pin,{**entry,"sha256":hashlib.sha256(_read_existing_or_empty(pin)).hexdigest()},data)
    restored, _, _ = _read_pinned_target(pin,entry["sha256"])
    if restored != data: raise ValueError("G10_ROLLBACK_VERIFY_FAILED")

def _read_existing_or_empty(pin:dict[str,Any])->bytes:
    try:
        fd=os.open(pin["name"],os.O_RDONLY|getattr(os,"O_NOFOLLOW",0),dir_fd=pin["parent_fd"])
    except FileNotFoundError: return b""
    try:
        value=b""
        while True:
            block=os.read(fd,1024*1024)
            if not block: return value
            value += block
    finally: os.close(fd)

def _unlink_pinned(pin:dict[str,Any], entry:dict[str,Any])->None:
    stat=os.fstat(pin["parent_fd"])
    if stat.st_dev != entry["parent_dev"] or stat.st_ino != entry["parent_ino"]: raise ValueError("G10_PARENT_IDENTITY_CHANGED")
    try: os.unlink(pin["name"],dir_fd=pin["parent_fd"])
    except FileNotFoundError: pass
def _confined_atomic_write(path:Path,data:bytes,mode:int|None,roots:list[Path])->None:
    root=next((item for item in roots if _within(path,item)),None)
    if root is None: raise ValueError("G10_TARGET_FORBIDDEN")
    relative=path.relative_to(root)
    flags=os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_NOFOLLOW",0)
    fd=os.open(root,flags)
    try:
        for component in relative.parts[:-1]:
            next_fd=os.open(component,flags,dir_fd=fd); os.close(fd); fd=next_fd
        name=relative.name; temp=f".g10-{os.getpid()}-{hashlib.sha256(data).hexdigest()[:12]}"
        out=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600,dir_fd=fd)
        try:
            os.write(out,data); os.fsync(out)
            if mode is not None: os.fchmod(out,mode)
        finally: os.close(out)
        os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd)
    finally:
        os.close(fd)
def _confined_unlink(path:Path,roots:list[Path])->None:
    root=next((item for item in roots if _within(path,item)),None)
    if root is None: raise ValueError("G10_TARGET_FORBIDDEN")
    rel=path.relative_to(root); flags=os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_NOFOLLOW",0); fd=os.open(root,flags)
    try:
        for component in rel.parts[:-1]:
            next_fd=os.open(component,flags,dir_fd=fd);os.close(fd);fd=next_fd
        try: os.unlink(rel.name,dir_fd=fd)
        except FileNotFoundError: pass
    finally: os.close(fd)
def _within(path:Path,root:Path)->bool:
    try:path.relative_to(root);return True
    except ValueError:return False
def _has_symlink_ancestor(path:Path,root:Path)->bool:
    current = path
    while True:
        if current.exists() and current.is_symlink(): return True
        try:
            if current.resolve(strict=False) == root.resolve(strict=True): return False
        except OSError: return True
        if current.parent == current: return True
        current = current.parent
def _candidate_diff(c:dict[str,Any])->str:return "\n".join(f"--- {x['path']}\n+++ {x['path']}\n@@ G10 @@\n-<sha256={x['before_sha256']}>\n+{x['content']}" for x in c["target_files"])
def _canonical(v:Any)->str:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def _hash(v:Any)->str:return hashlib.sha256(_canonical(v).encode()).hexdigest()
def _proposal_id(run_id:str,candidate_hash:str)->str:return hashlib.sha256(f"{run_id}:{candidate_hash}".encode()).hexdigest()
def _redact(v:str)->str:
    try:
        structured=json.loads(v)
    except (TypeError, ValueError, json.JSONDecodeError):
        structured=None
    if structured is not None:
        return json.dumps(_redact_structured(structured), ensure_ascii=False, separators=(",", ":"))
    return _redact_text(v)

def _redact_structured(value:Any)->Any:
    if isinstance(value,dict):
        return {key:("[REDACTED_SECRET]" if isinstance(key,str) and re.search(_SECRET_NAME,key,re.I) else _redact_structured(child)) for key,child in value.items()}
    if isinstance(value,list): return [_redact_structured(item) for item in value]
    return _redact_text(value) if isinstance(value,str) else value

def redact_structured(value:Any)->Any:
    """Canonical control-plane redaction for structured external diagnostics."""
    return _redact_structured(value)

def _redact_text(v:str)->str:
    def quoted(match:re.Match[str])->str: return match.group("prefix") + match.group("quote") + "[REDACTED_SECRET]" + match.group("quote")
    def bare(match:re.Match[str])->str: return match.group("prefix") + "[REDACTED_SECRET]"
    def url(match:re.Match[str])->str:
        if match.group("query"): return match.group("query") + "[REDACTED_SECRET]"
        return match.group("prefix").split(":",1)[0] + "://[REDACTED_SECRET]"
    value=_QUOTED_SECRET.sub(quoted,v)
    value=_URL_SECRET.sub(url,value)
    value=_BARE_SECRET.sub(bare,value)
    return _PHONE.sub("[REDACTED_PHONE]",_EMAIL.sub("[REDACTED_EMAIL]",value))
def _redacted(v:Any)->Any:return _redact(v) if isinstance(v,str) else ([_redacted(x) for x in v] if isinstance(v,list) else ({k:_redacted(x) for k,x in v.items()} if isinstance(v,dict) else v))
def _has_secret(v:str)->bool:
    try:
        structured=json.loads(v)
    except (TypeError, ValueError, json.JSONDecodeError):
        structured=None
    if structured is not None and _structured_has_secret(structured): return True
    return bool(_QUOTED_SECRET.search(v) or _BARE_SECRET.search(v) or _URL_SECRET.search(v))

def _structured_has_secret(value:Any)->bool:
    if isinstance(value,dict): return any((isinstance(key,str) and re.search(_SECRET_NAME,key,re.I)) or _structured_has_secret(child) for key,child in value.items())
    if isinstance(value,list): return any(_structured_has_secret(item) for item in value)
    return isinstance(value,str) and bool(_URL_SECRET.search(value))
def _safe_validation_command(command:str)->bool:
    try:parts=shlex.split(command)
    except ValueError:return False
    if parts[:3]!=["python3","-m","unittest"] or any(any(x in p for x in ";|&`$\\") for p in parts):return False
    args=parts[3:]
    if not args:return True
    if args[:2]==["discover","-s"] and len(args)==3:return args[2] in {"scripts/tests","scripts.tests"}
    return all(re.fullmatch(r"scripts\.tests(?:\.[A-Za-z_][A-Za-z0-9_]*)+",x) for x in args)
def _pid_live(pid: Any) -> bool:
    """Return False only for a PID that the operating system proves is absent."""
    if not isinstance(pid, int) or pid <= 0: return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True

def _lease_expired(value: Any) -> bool:
    try:
        expiry = datetime.fromisoformat(value)
        if expiry.tzinfo is None:
            return False
        return expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False

class _ApplyFailure(Exception):
    def __init__(self, reason: str): self.reason = reason
