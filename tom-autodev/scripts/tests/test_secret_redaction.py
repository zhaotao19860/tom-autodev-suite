"""A-06: credentials never reach observations, receipts, approval material or the DB.

The old rules only scrubbed URLs and >=32-character token shapes, so a short irepo
token, a dotted token, or an error response echoing the request body (which carries
`--header "IREPO-TOKEN:<token>"`) was persisted verbatim. Uses a throwaway config
root; never the user's real credentials.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import persistence_policy
from approval_ledger import ApprovalLedger
from clients.ipipe_client import IpipeHttpTransport, IpipeTransportError
from persistence_policy import ensure_persistable, redact_secrets
from stage_parameters import load_tokens, product_command, redacted
from state_store import StateStore
from test_ipipe_runtime import FakeApi, approved, build, pinned_runtime


SHORT_TOKEN = "k3Y9mQ2a"        # far below the old 32-character heuristic
DOTTED_TOKEN = "irp.v1.Zx81q"   # has dots, which the old shape rule never matched


class SecretRedactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        credentials = self.root / "config" / "credentials"
        credentials.mkdir(parents=True)
        token_file = credentials / "irepo-tokens.yaml"
        token_file.write_text(
            f"baidu/sysip/bgwagent: {DOTTED_TOKEN}\nbaidu/nsiqa/x86bgw: {SHORT_TOKEN}\n",
            encoding="utf-8",
        )
        os.chmod(token_file, 0o600)
        self.addCleanup(persistence_policy._REGISTERED_SECRETS.difference_update,
                        {SHORT_TOKEN, DOTTED_TOKEN})
        self.tokens = load_tokens(self.root / "config")
        self.state = StateStore(self.root / "state.sqlite")
        self.ledger = ApprovalLedger(self.root / "approvals.sqlite")
        self.api = FakeApi()

    def database_text(self):
        return (self.root / "state.sqlite").read_bytes().decode("utf-8", "replace")

    def test_short_and_dotted_header_tokens_are_redacted_by_shape(self):
        persistence_policy._REGISTERED_SECRETS.difference_update({SHORT_TOKEN, DOTTED_TOKEN})
        for token in ("ab", SHORT_TOKEN, DOTTED_TOKEN):
            with self.subTest(token=token):
                text = product_command("https://irepo.example/p.tar.gz", token)
                self.assertNotIn(token, redact_secrets({"value": text})["value"])
                with self.assertRaisesRegex(ValueError, "PERSISTENCE_SECRET_REJECTED"):
                    ensure_persistable({"value": text})

    def test_loaded_token_values_are_redacted_wherever_they_appear(self):
        self.assertEqual(sorted(self.tokens.values()), sorted([DOTTED_TOKEN, SHORT_TOKEN]))
        text = f"downloaded with {DOTTED_TOKEN} and {SHORT_TOKEN}"
        self.assertNotIn(DOTTED_TOKEN, redact_secrets(text))
        self.assertNotIn(SHORT_TOKEN, redact_secrets(text))
        with self.assertRaisesRegex(ValueError, "PERSISTENCE_SECRET_REJECTED"):
            ensure_persistable({"note": text})

    def test_access_token_authorization_and_bearer_values_are_refused(self):
        for text in (
            "https://ku.example/doc?access_token=abc&x=1",
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "header x-ac-Authorization=Bearer-0123456789abcdef",
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "PERSISTENCE_SECRET_REJECTED"):
                    ensure_persistable({"note": text})
                ensure_persistable({"note": redact_secrets(text)})

    def test_placeholders_and_redacted_bindings_stay_byte_identical(self):
        binding = redacted({"get_bgwagent": product_command("https://irepo.example/p", DOTTED_TOKEN)})
        self.assertEqual(redact_secrets(binding), binding)
        ensure_persistable(binding)
        ensure_persistable({"note": "Bearer token and access_token=[REDACTED]"})

    def test_an_error_response_echoing_the_request_body_is_redacted(self):
        command = product_command("https://irepo.example/agent.tar.gz", DOTTED_TOKEN)
        echoed = json.dumps({"code": 500, "msg": "bad params",
                             "request": {"get_bgwagent": command,
                                         "get_bgw_test_case": f'IREPO-TOKEN:{SHORT_TOKEN}'}})

        def sender(method, url, headers, body, timeout, limit):
            return {"status": 500, "body": echoed}

        transport = IpipeHttpTransport(token="Bearer-ipipe-secret-value", sender=sender)
        with self.assertRaises(IpipeTransportError) as caught:
            transport.request("POST", "/stage/execute", body={})
        diagnostic = json.dumps(caught.exception.diagnostic)
        self.assertNotIn(DOTTED_TOKEN, diagnostic)
        self.assertNotIn(SHORT_TOKEN, diagnostic)

        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.api.candidates = [build(status="FAIL", stages=[failed])]
        self.api.builds["build-1"] = build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        runtime = pinned_runtime(self.state, self.ledger, self.api)
        runtime.discover(json.loads(json.dumps(runtime.validated_profile)),
                         __import__("test_ipipe_runtime").REVISIONS)
        runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", runtime.rerun_input_hash("stage-1")["input_hash"])
        # The raw (unsanitized) error, as an adapter bug could hand it over.
        self.api.rerun_result = IpipeTransportError(
            "PIPELINE_TRANSIENT", status=500, transient=True,
            diagnostic={"http_status": 500, "body_truncated": False, "body_excerpt": echoed},
        )
        runtime.rerun("stage-1", approval)

        text = self.database_text()
        self.assertNotIn(DOTTED_TOKEN, text)
        self.assertNotIn(SHORT_TOKEN, text)

    def test_a_receipt_quoting_a_credential_is_stored_redacted(self):
        claim = self.state.claim_intent("run-1", "ipipe.rerun", "k", {"a": 1})
        self.state.receipt(claim["intent"]["intent_id"], {
            "ok": False, "reason_code": "X", "detail": f"wget IREPO-TOKEN:{SHORT_TOKEN}",
        }, [])

        stored = self.state.result_by_idempotency_key("k")["receipt"]["response"]

        self.assertNotIn(SHORT_TOKEN, json.dumps(stored))
        self.assertNotIn(SHORT_TOKEN, self.database_text())

    # --- A-11: irepo header variants --------------------------------------------------

    def test_irepo_header_with_whitespace_or_equals_is_redacted(self):
        for text in (f"IREPO-TOKEN: {DOTTED_TOKEN}x", f"IREPO-TOKEN =  zz{SHORT_TOKEN}",
                     "irepo-token:\tabc123"):
            with self.subTest(text=text):
                persistence_policy._REGISTERED_SECRETS.difference_update({SHORT_TOKEN, DOTTED_TOKEN})
                with self.assertRaisesRegex(ValueError, "PERSISTENCE_SECRET_REJECTED"):
                    ensure_persistable({"value": text})
                cleaned = redact_secrets(text)
                self.assertIn("<IREPO-TOKEN>", cleaned)
                ensure_persistable({"value": cleaned})

    def test_a11_unregistered_one_character_irepo_values_and_placeholders(self):
        persistence_policy._REGISTERED_SECRETS.difference_update({SHORT_TOKEN, DOTTED_TOKEN})
        for text, expected in (
            ("IREPO-TOKEN: x", "IREPO-TOKEN: <IREPO-TOKEN>"),
            ("IREPO-TOKEN=x", "IREPO-TOKEN=<IREPO-TOKEN>"),
            ('--header "IREPO-TOKEN:x"', '--header "IREPO-TOKEN:<IREPO-TOKEN>"'),
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_secrets(text), expected)
                with self.assertRaisesRegex(ValueError, "PERSISTENCE_SECRET_REJECTED"):
                    ensure_persistable({"value": text})
        for placeholder in ("IREPO-TOKEN: <IREPO-TOKEN>", "IREPO-TOKEN=<IREPO-TOKEN>",
                            '--header "IREPO-TOKEN:<IREPO-TOKEN>"'):
            with self.subTest(placeholder=placeholder):
                self.assertEqual(redact_secrets(placeholder), placeholder)
                ensure_persistable({"value": placeholder})

    # --- A-12: drafts quoting a log are redacted before validation ---------------------

    def test_a_fresh_draft_quoting_a_bearer_line_is_redacted(self):
        from worker_driver import _redacted_fresh_draft
        draft = {"log_evidence": ["curl -H 'Authorization: Bearer abcdefgh12345' failed"]}

        fresh = _redacted_fresh_draft({"status": "PENDING", "draft": None}, {"phase": "DIAGNOSE"}, draft)

        self.assertNotIn("abcdefgh12345", str(fresh))
        ensure_persistable(fresh)

    def test_a_recorded_draft_or_a_patch_is_not_rewritten(self):
        from worker_driver import _redacted_fresh_draft
        draft = {"log_evidence": ["Authorization: Bearer abcdefgh12345"]}
        # A draft is already recorded: an approval may be bound to its hash.
        self.assertIs(_redacted_fresh_draft(
            {"status": "FULFILLED", "draft": dict(draft)}, {"phase": "DIAGNOSE"}, draft), draft)
        # IMPLEMENT content is a patch; its bytes are the candidate.
        self.assertIs(_redacted_fresh_draft(None, {"phase": "IMPLEMENT"}, draft), draft)
        with self.assertRaisesRegex(ValueError, "PERSISTENCE_SECRET_REJECTED"):
            ensure_persistable(draft)

    def test_a12_replaying_the_raw_draft_of_a_redacted_record_yields_the_recorded_bytes(self):
        from worker_driver import _redacted_fresh_draft
        raw = {"log_evidence": ["Authorization: Bearer abcdefgh12345"]}
        recorded = redact_secrets(raw)

        replay = _redacted_fresh_draft({"status": "FULFILLED", "draft": recorded}, {"phase": "DIAGNOSE"}, raw)
        other = {"log_evidence": ["Authorization: Bearer zzzzzzzz99999", "new line"]}
        changed = _redacted_fresh_draft({"status": "FULFILLED", "draft": recorded}, {"phase": "DIAGNOSE"}, other)

        self.assertEqual(replay, recorded)
        self.assertIs(changed, other)


if __name__ == "__main__":
    unittest.main()
