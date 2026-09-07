import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artifact_store import ArtifactStore
from state_store import StateStore


class StateAndArtifactTests(unittest.TestCase):
    def test_transition_is_append_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            first = store.transition("run-1", "INTAKE", {"snapshot": "sha-a"})
            second = store.transition("run-1", "GRILL", {"snapshot": "sha-a"})

            self.assertNotEqual(first["event_id"], second["event_id"])
            self.assertEqual(
                [event["state"] for event in store.events("run-1")],
                ["INTAKE", "GRILL"],
            )

    def test_artifact_hash_and_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            first = store.put("run-1", "spec", b"spec-v1", {"spec_version": "1"})
            second = store.put("run-1", "spec", b"spec-v1", {"spec_version": "1"})

            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(first["artifact_id"], second["artifact_id"])
            self.assertTrue(Path(first["path"]).is_file())

    def test_idempotency_result_cannot_change(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            saved = store.save_idempotency_result("submit:run-1", {"revision": "r1"})

            self.assertEqual(saved, {"revision": "r1"})
            self.assertEqual(
                store.idempotency_result("submit:run-1"),
                {"revision": "r1"},
            )
            with self.assertRaises(ValueError):
                store.save_idempotency_result("submit:run-1", {"revision": "r2"})

    def test_external_intent_is_durable_and_idempotency_key_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            store = StateStore(database)

            first = store.intent(
                "run-1",
                "create-document",
                "doc:requirement-7",
                {"document_key": "requirement-7"},
            )
            duplicate = StateStore(database).intent(
                "run-1",
                "create-document",
                "doc:requirement-7",
                {"document_key": "requirement-7"},
            )

            self.assertEqual(duplicate, first)
            self.assertEqual(
                StateStore(database).pending_intents("run-1"),
                [first],
            )
            with self.assertRaisesRegex(ValueError, "INTENT_CONFLICT"):
                store.intent(
                    "run-1",
                    "create-document",
                    "doc:requirement-7",
                    {"document_key": "different"},
                )

    def test_external_receipt_is_idempotent_and_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            store = StateStore(database)
            intent = store.intent("run-1", "submit-cr", "submit:task-1", {"task": "task-1"})

            first = store.receipt(
                intent["intent_id"],
                {"revision": "refs/changes/7"},
                ["artifact-a"],
            )
            duplicate = StateStore(database).receipt(
                intent["intent_id"],
                {"revision": "refs/changes/7"},
                ["artifact-a"],
            )

            self.assertEqual(duplicate, first)
            self.assertEqual(StateStore(database).pending_intents("run-1"), [])
            with self.assertRaisesRegex(ValueError, "RECEIPT_CONFLICT"):
                store.receipt(
                    intent["intent_id"],
                    {"revision": "refs/changes/8"},
                    ["artifact-a"],
                )

    def test_external_intent_and_verified_result_are_lookupable_by_idempotency_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite")
            intent = store.intent("run-lookup", "remote.write", "lookup-key", {"hash": "h1"})

            self.assertEqual(store.intent_by_idempotency_key("lookup-key"), intent)
            self.assertIsNone(store.result_by_idempotency_key("lookup-key"))

            receipt = store.receipt(
                intent["intent_id"], {"ok": True, "remote_id": "r1"}, ["artifact:a1"]
            )
            result = store.result_by_idempotency_key("lookup-key")

        self.assertEqual(result["intent"], intent)
        self.assertEqual(result["receipt"], receipt)
        self.assertEqual(result["operation"], "remote.write")

    def test_artifact_lookup_verifies_content_and_metadata_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            artifact = store.put("run-1", "spec", b"spec-v1", {"spec_version": "1"})

            loaded = ArtifactStore(Path(directory) / "artifacts").get(artifact["artifact_id"])

            self.assertTrue(loaded["valid"])
            self.assertEqual(loaded["content"], b"spec-v1")
            self.assertEqual(loaded["metadata"], {"spec_version": "1"})

            Path(artifact["path"]).write_bytes(b"tampered")
            corrupted = store.get(artifact["artifact_id"])
            self.assertFalse(corrupted["valid"])
            self.assertEqual(corrupted["reason_code"], "ARTIFACT_CONTENT_HASH_MISMATCH")
            self.assertNotIn("content", corrupted)

    def test_missing_or_corrupt_artifact_metadata_is_not_valid_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            missing = store.get("does-not-exist")
            self.assertEqual(
                missing,
                {
                    "artifact_id": "does-not-exist",
                    "valid": False,
                    "reason_code": "ARTIFACT_NOT_FOUND",
                },
            )

            artifact = store.put("run-1", "plan", b"plan", {"version": 2})
            metadata_path = Path(artifact["path"]).with_name("metadata.json")
            metadata_path.write_text('{"version":3}', encoding="utf-8")

            corrupted = store.get(artifact["artifact_id"])
            self.assertFalse(corrupted["valid"])
            self.assertEqual(corrupted["reason_code"], "ARTIFACT_METADATA_HASH_MISMATCH")
            self.assertNotIn("metadata", corrupted)

    def test_artifact_root_is_absolute_and_survives_a_working_directory_change(self):
        with tempfile.TemporaryDirectory() as directory:
            original_directory = Path.cwd()
            root = Path(directory)
            other = root / "other"
            other.mkdir()
            try:
                os.chdir(root)
                store = ArtifactStore("artifacts")
                artifact = store.put("run-1", "spec", b"spec", {})
                os.chdir(other)

                loaded = store.get(artifact["artifact_id"])
            finally:
                os.chdir(original_directory)

            self.assertTrue(Path(artifact["path"]).is_absolute())
            self.assertTrue(loaded["valid"])
            self.assertEqual(loaded["content"], b"spec")

    def test_artifact_components_cannot_be_absolute_or_traverse_the_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "artifacts"
            outside = base / "outside"
            store = ArtifactStore(root)
            cases = [
                ("../outside", "spec"),
                (str(outside / "absolute-run"), "spec"),
                ("run-1", "../outside"),
                ("run-1", str(outside / "absolute-kind")),
            ]

            for run_id, kind in cases:
                with self.subTest(run_id=run_id, kind=kind):
                    with self.assertRaisesRegex(ValueError, "ARTIFACT_COMPONENT_INVALID"):
                        store.put(run_id, kind, b"blocked", {})

            self.assertFalse(outside.exists())

    def test_artifact_symlink_escape_and_tampered_index_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "artifacts"
            outside = base / "outside"
            outside.mkdir()
            store = ArtifactStore(root)
            (root / "run-link").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "ARTIFACT_PATH_ESCAPE"):
                store.put("run-link", "spec", b"blocked", {})

            artifact = store.put("run-1", "spec", b"trusted", {})
            outside_content = outside / "content.bin"
            outside_content.write_bytes(b"trusted")
            with sqlite3.connect(store.database_path) as connection:
                connection.execute(
                    "UPDATE artifacts SET content_path = ? WHERE artifact_id = ?",
                    (str(outside_content), artifact["artifact_id"]),
                )

            escaped = store.get(artifact["artifact_id"])
            self.assertFalse(escaped["valid"])
            self.assertEqual(escaped["reason_code"], "ARTIFACT_PATH_ESCAPE")
            self.assertNotIn("content", escaped)
            self.assertEqual(list(outside.glob("**/metadata.json")), [])

    def test_reput_of_corrupt_or_conflicting_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            artifact = store.put("run-1", "spec", b"trusted", {"version": 1})
            Path(artifact["path"]).write_bytes(b"corrupt")

            with self.assertRaisesRegex(ValueError, "ARTIFACT_CONFLICT"):
                store.put("run-1", "spec", b"trusted", {"version": 1})

            with sqlite3.connect(store.database_path) as connection:
                connection.execute(
                    "UPDATE artifacts SET metadata_sha256 = ? WHERE artifact_id = ?",
                    ("0" * 64, artifact["artifact_id"]),
                )
            with self.assertRaisesRegex(ValueError, "ARTIFACT_CONFLICT"):
                store.put("run-1", "spec", b"trusted", {"version": 1})

    def test_secret_bearing_keys_are_rejected_at_every_state_boundary_without_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            store = StateStore(database)
            sentinel = "never-persist-this-value"
            intent = store.intent("run-safe", "query", "query:safe", {"query": "safe"})
            attempts = [
                lambda: store.transition("run-1", "INTAKE", {"nested": {"token": sentinel}}),
                lambda: store.save_idempotency_result(
                    "unsafe", {"Authorization": sentinel}
                ),
                lambda: store.intent(
                    "run-1", "submit", "submit:unsafe", {"items": [{"password": sentinel}]}
                ),
                lambda: store.receipt(
                    intent["intent_id"], {"credential": sentinel}, ["artifact-safe"]
                ),
                lambda: store.record_handoff(
                    "run-1", "handoff-unsafe", {"private-key": sentinel}
                ),
            ]

            for attempt in attempts:
                with self.subTest(attempt=attempt):
                    with self.assertRaisesRegex(ValueError, "PERSISTENCE_SECRET_REJECTED") as error:
                        attempt()
                    self.assertNotIn(sentinel, str(error.exception))

            persisted = b"".join(path.read_bytes() for path in Path(directory).glob("state.sqlite*"))
            self.assertFalse(sentinel.encode() in persisted)

    def test_secret_bearing_artifact_metadata_is_never_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = ArtifactStore(root)
            sentinel = "never-write-this-value"

            with self.assertRaisesRegex(ValueError, "PERSISTENCE_SECRET_REJECTED") as error:
                store.put(
                    "run-1",
                    "spec",
                    b"content-safe",
                    {"nested": [{"api key": sentinel}, {"secret": sentinel}]},
                )

            self.assertNotIn(sentinel, str(error.exception))
            persisted = b"".join(path.read_bytes() for path in root.glob("**/*") if path.is_file())
            self.assertFalse(sentinel.encode() in persisted)
            self.assertEqual(list(root.glob("**/metadata.json")), [])

    def test_evidence_references_accept_only_canonical_non_secret_ids_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            store = StateStore(database)
            sentinel = "never-persist-reference-secret"
            invalid_cases = [
                ({"artifact": "artifact-a"}, "EVIDENCE_REF_INVALID"),
                (["https://example.test/evidence"], "EVIDENCE_REF_INVALID"),
                (["artifact-a?signature=value"], "EVIDENCE_REF_INVALID"),
                (["artifact-a#fragment"], "EVIDENCE_REF_INVALID"),
                (["artifact:secret-key"], "PERSISTENCE_SECRET_REJECTED"),
                ([f"artifact-a?token={sentinel}"], "PERSISTENCE_SECRET_REJECTED"),
                (["ku:document-only"], "EVIDENCE_REF_INVALID"),
                (["ipipe:build/stage/credential"], "PERSISTENCE_SECRET_REJECTED"),
            ]

            for index, (references, reason_code) in enumerate(invalid_cases):
                with self.subTest(references=references):
                    intent = store.intent(
                        "run-1",
                        "submit",
                        f"submit:invalid-ref:{index}",
                        {"task": f"task-{index}"},
                    )
                    with self.assertRaisesRegex(ValueError, reason_code) as error:
                        store.receipt(intent["intent_id"], {"status": "ok"}, references)
                    self.assertNotIn(sentinel, str(error.exception))

            intent = store.intent(
                "run-1", "submit", "submit:valid-refs", {"task": "task-valid"}
            )
            saved = store.receipt(
                intent["intent_id"],
                {"status": "ok"},
                [
                    "artifact-a",
                    "artifact:0123456789abcdef",
                    "ku:document-7/version-2",
                    # KU mints document ids that can start with an underscore.
                    "ku:_SpEWIeXSOhcFz/1",
                    "ipipe:build-9/stage-3",
                ],
            )
            persisted = b"".join(path.read_bytes() for path in Path(directory).glob("state.sqlite*"))

            self.assertEqual(
                saved["evidence_refs"],
                [
                    "artifact-a",
                    "artifact:0123456789abcdef",
                    "ku:document-7/version-2",
                    "ku:_SpEWIeXSOhcFz/1",
                    "ipipe:build-9/stage-3",
                ],
            )
            self.assertFalse(sentinel.encode() in persisted)


class GenericPutSchemaTests(unittest.TestCase):
    """Content checks on the plain `put` path, which had none.

    `put_envelope` derives a schema from the phase, so phase artifacts were always
    checked. Everything else -- the submit descriptor iCode is asked to take, the run
    summary G10 reasons from -- was archived, hashed, indexed, and read back as
    evidence with nobody having looked at its shape.
    """

    descriptor = {
        "run_id": "run-1", "change_set_id": "change-1", "revision_set_id": "RS-1",
        "repo_path": "/tmp/work/repo", "module": "baidu/team/repo", "target_branch": "main",
        "commit_revision": "rev-1", "card_id": "BGW-1", "owner": "dev",
        "revision_set": {
            "business": {"module": "baidu/team/repo", "revision": "rev-1", "branch": "main"},
            "test": {"module": "baidu/team/repo-tests", "revision": "test-rev", "branch": "main"},
        },
    }

    @staticmethod
    def _canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    def test_a_whole_submit_descriptor_is_archived_and_a_partial_one_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = ArtifactStore(root)

            archived = store.put("run-1", "change-set", self._canonical(self.descriptor), {"verdict": "PASS"})
            self.assertTrue(store.get(archived["artifact_id"])["valid"])

            # Dropping `revision_set` used to be found by iCode rejecting the
            # submission, one approved gate and one push attempt later.
            partial = {key: value for key, value in self.descriptor.items() if key != "revision_set"}
            cases = {
                "missing field": self._canonical(partial),
                "unknown field": self._canonical({**self.descriptor, "merged": True}),
                "wrong nesting": self._canonical({**self.descriptor, "revision_set": {"business": {}, "test": {}}}),
                "not json": b"<html>gateway timeout</html>",
            }
            for name, content in cases.items():
                with self.subTest(case=name):
                    with self.assertRaisesRegex(ValueError, "SCHEMA_INVALID"):
                        store.put("run-1", "change-set", content, {"verdict": "PASS"})

            # Rejected before any write, so nothing can cite a rejected artifact: no
            # file on disk beyond the one good archive, and no index row.
            with sqlite3.connect(store.database_path) as connection:
                rows = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            self.assertEqual(rows, 1)
            self.assertEqual(
                sorted(path.name for path in (root / "run-1" / "change-set").iterdir()),
                [archived["artifact_id"]],
            )

    def test_a_kind_with_no_schema_still_takes_opaque_bytes(self):
        """`submission` and `ai-review` wrap a remote response whose shape is iCode's.

        Pinning it here would reject real evidence for being unfamiliar, so the
        exemption is deliberate and listed. An unrecognised kind is archived too --
        this is a check on known kinds, not an allowlist.
        """
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")

            for kind, content in (("submission", b'{"cr":1}'), ("ai-review", b"{}"), ("spec", b"not json")):
                with self.subTest(kind=kind):
                    artifact = store.put("run-1", kind, content, {})
                    self.assertEqual(store.get(artifact["artifact_id"])["content"], content)


if __name__ == "__main__":
    unittest.main()
