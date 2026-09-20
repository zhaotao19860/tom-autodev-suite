import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collaboration import CollaborationSession
from state_store import StateStore


class FakeGroupClient:
    def __init__(self, *, create_result=None, send_result=None, create_error=None):
        self.create_result = create_result or {"group_id": "123", "bot_id": "bot-7"}
        self.send_result = send_result or {"message_id": "message-1"}
        self.create_error = create_error
        self.create_calls = []
        self.send_calls = []
        self.reconcile_calls = []

    def create_or_reuse(self, request):
        self.create_calls.append(request)
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def reconcile_group(self, request):
        self.reconcile_calls.append(request)
        return None

    def send_markdown(self, group_id, content, at_users, idempotency_key):
        self.send_calls.append((group_id, content, at_users, idempotency_key))
        return self.send_result

    def reconcile_message(self, group_id, idempotency_key):
        return None


def members(*, include_card_owner=False, invalid_email=None):
    profile = {
        "development": ["dev@example.test"],
        "test": ["test@example.test"],
        "project": ["owner@example.test"],
        "icafe_responsible": {"development": ["cafe-dev@example.test"]},
        "allow_card_owners": include_card_owner,
        "card_owners": ["card-owner@example.test"],
    }
    if invalid_email is not None:
        profile["test"] = [invalid_email]
    return profile


def g0_binding(session, profile):
    resolved = session.resolve_members(profile)
    return {
        "action": "G0",
        "effective_decision": "APPROVE",
        "group_name": "BGW-19-Login-flow",
        "owner": "owner@example.test",
        "member_snapshot": resolved["member_snapshot"],
    }


class CollaborationSessionTests(unittest.TestCase):
    def test_create_resolves_fixed_profile_icafe_and_explicitly_allowed_card_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGroupClient()
            session = CollaborationSession(StateStore(Path(directory) / "state.sqlite"), client)
            profile = members(include_card_owner=True)
            profile["g0_approval"] = g0_binding(session, profile)

            result = session.create("run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, profile)

        self.assertEqual(result["group_id"], "123")
        self.assertEqual(
            result["member_snapshot"],
            [
                "cafe-dev@example.test",
                "card-owner@example.test",
                "dev@example.test",
                "owner@example.test",
                "test@example.test",
            ],
        )
        self.assertEqual(client.create_calls[0]["friendlyLevel"], 3)
        self.assertEqual(client.create_calls[0]["group_name"], "BGW-19-Login-flow")

    def test_create_requires_full_email_before_an_external_call(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGroupClient()
            session = CollaborationSession(StateStore(Path(directory) / "state.sqlite"), client)
            profile = members(invalid_email="tester")
            profile["g0_approval"] = {"action": "G0", "effective_decision": "APPROVE"}

            result = session.create("run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, profile)

        self.assertEqual(result["reason_code"], "MEMBER_CONFIRMATION_REQUIRED")
        self.assertEqual(result["unresolved"], ["tester"])
        self.assertEqual(client.create_calls, [])

    def test_create_requires_a_g0_binding_for_the_canonical_group_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGroupClient()
            session = CollaborationSession(StateStore(Path(directory) / "state.sqlite"), client)

            result = session.create(
                "run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, members()
            )

        self.assertEqual(result["reason_code"], "G0_BINDING_REQUIRED")
        self.assertEqual(client.create_calls, [])

    def test_create_reuses_the_durable_group_receipt_without_a_second_create(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGroupClient()
            session = CollaborationSession(StateStore(Path(directory) / "state.sqlite"), client)
            profile = members()
            profile["g0_approval"] = g0_binding(session, profile)

            first = session.create("run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, profile)
            repeated = session.create("run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, profile)

        self.assertEqual(repeated, first)
        self.assertEqual(len(client.create_calls), 1)

    def test_unknown_group_create_result_is_reconciled_without_replaying_the_write(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGroupClient(create_error=TimeoutError("unknown"))
            session = CollaborationSession(StateStore(Path(directory) / "state.sqlite"), client)
            profile = members()
            profile["g0_approval"] = g0_binding(session, profile)

            first = session.create("run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, profile)
            repeated = session.create("run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, profile)

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(repeated["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(len(client.create_calls), 1)
        self.assertGreaterEqual(len(client.reconcile_calls), 1)

    def test_route_failure_targets_the_required_roles_and_replays_duplicate_messages(self):
        routes = {
            "environment": ["test@example.test"],
            "code": ["cafe-dev@example.test", "dev@example.test"],
            "mixed": ["cafe-dev@example.test", "dev@example.test", "test@example.test"],
            "auth": ["owner@example.test"],
        }
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGroupClient()
            session = CollaborationSession(StateStore(Path(directory) / "state.sqlite"), client)
            profile = members()
            profile["g0_approval"] = g0_binding(session, profile)
            session.create("run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, profile)

            for category, expected in routes.items():
                with self.subTest(category=category):
                    receipt = session.route_failure(
                        {
                            "run_id": "run-1",
                            "category": category,
                            "summary": "x" * 900,
                            "next_action": "inspect",
                            "evidence": {"icafe": "BGW-19", "pipeline": "pipe-4", "stage": "unit"},
                        }
                    )
                    self.assertEqual(receipt["at_users"], expected)
                    self.assertLessEqual(len(receipt["content"]), 1200)
                    self.assertTrue(all(f"@{email}" in receipt["content"] for email in expected))

            duplicate = session.route_failure(
                {
                    "run_id": "run-1",
                    "category": "code",
                    "summary": "same failure",
                    "next_action": "inspect",
                    "evidence": {"icafe": "BGW-19"},
                }
            )
            duplicate_again = session.route_failure(
                {
                    "run_id": "run-1",
                    "category": "code",
                    "summary": "same failure",
                    "next_action": "inspect",
                    "evidence": {"icafe": "BGW-19"},
                }
            )

        self.assertEqual(duplicate_again, duplicate)
        self.assertEqual(len(client.send_calls), 5)

    def test_changed_message_content_under_one_idempotency_key_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGroupClient()
            session = CollaborationSession(StateStore(Path(directory) / "state.sqlite"), client)
            profile = members()
            profile["g0_approval"] = g0_binding(session, profile)
            session.create("run-1", "bgw", {"id": "BGW-19", "title": "Login flow"}, profile)

            first = session.send_message(
                "run-1", "@dev@example.test first", ["dev@example.test"], "same-key"
            )
            conflict = session.send_message(
                "run-1", "@dev@example.test changed", ["dev@example.test"], "same-key"
            )

        self.assertEqual(first["message_id"], "message-1")
        self.assertEqual(conflict["reason_code"], "MESSAGE_CONFLICT")
        self.assertEqual(len(client.send_calls), 1)


class IntakeInputHashTests(unittest.TestCase):
    """MEDIUM-001: the G0 input hash binds the change class and workflow_spec version."""

    @staticmethod
    def _v2_payload(**overrides):
        payload = {
            "requirement_id": "BGW-1",
            "project": "bgw",
            "profile_hash": "p",
            "requirement_snapshot": {"content_hash": "c"},
            "collaboration_binding": {"run_id": "run-1"},
            "change_class": "standard",
            "workflow_spec_hash": "spec-hash-1",
            "intake_hash_version": "v2",
        }
        payload.update(overrides)
        return payload

    def test_change_class_is_part_of_the_v2_hash(self):
        from collaboration import intake_input_hash

        base = intake_input_hash(self._v2_payload())
        other = intake_input_hash(self._v2_payload(change_class="full"))
        self.assertNotEqual(base, other)

    def test_workflow_spec_hash_is_part_of_the_v2_hash(self):
        from collaboration import intake_input_hash

        base = intake_input_hash(self._v2_payload())
        edited = intake_input_hash(self._v2_payload(workflow_spec_hash="spec-hash-2"))
        self.assertNotEqual(base, edited)

    def test_legacy_payload_ignores_change_class_in_the_hash(self):
        # A legacy (v1) INTAKE payload carries no intake_hash_version, so its hash is over
        # the original five keys — adding/altering change_class does not change it, which is
        # what keeps already-approved legacy runs valid.
        from collaboration import intake_input_hash

        legacy = {
            "requirement_id": "BGW-1", "project": "bgw", "profile_hash": "p",
            "requirement_snapshot": {"content_hash": "c"},
            "collaboration_binding": {"run_id": "run-1"},
        }
        base = intake_input_hash(legacy)
        self.assertEqual(intake_input_hash({**legacy, "change_class": "full"}), base)


if __name__ == "__main__":
    unittest.main()
