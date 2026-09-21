import json
import os
import tempfile
import unittest
from pathlib import Path

import cli_transport
import orchestrator as orchestrator_module
import stage_parameters

URL = "https://irepo.baidu-int.com/rest/prod/v3/baidu/sysip/bgwagent/nodes/102009429/files"
TOKEN = "82f7b234-8b56-40c5-9011-5492673ed8d8"


class StageParameterTests(unittest.TestCase):
    def test_both_parameters_of_the_bgw_new_case_stage_are_derived(self):
        qa_url = "https://irepo.baidu-int.com/rest/prod/v3/baidu/nsiqa/x86bgw/nodes/102407103/files"
        qa_token = "50c9c669-7fbf-442f-b61a-51d2c36ef172"
        resolved = stage_parameters.resolve(
            ["get_bgw_test_case", "get_bgwagent"],
            change_number="122527554",
            product_urls={
                "baidu/nsiqa/x86bgw": qa_url,
                "baidu/sysip/bgwagent": URL,
            },
            tokens={
                "baidu/nsiqa/x86bgw": qa_token,
                "baidu/sysip/bgwagent": TOKEN,
            },
        )

        self.assertTrue(resolved["ok"])
        self.assertIn(f'--header "IREPO-TOKEN:{qa_token}"', resolved["parameters"]["get_bgw_test_case"])
        self.assertIn(qa_url, resolved["parameters"]["get_bgw_test_case"])
        self.assertIn(f'--header "IREPO-TOKEN:{TOKEN}"', resolved["parameters"]["get_bgwagent"])
        self.assertIn(URL, resolved["parameters"]["get_bgwagent"])

    def test_get_bgw_test_case_does_not_bind_the_business_x86bgw_module(self):
        qa_url = "https://irepo.baidu-int.com/rest/prod/v3/baidu/nsiqa/x86bgw/nodes/102407103/files"
        business_url = "https://irepo.baidu-int.com/rest/prod/v3/baidu/sysip/x86bgw/nodes/102009480/files"
        resolved = stage_parameters.resolve(
            ["get_bgw_test_case"],
            change_number="122527554",
            product_urls={
                "baidu/sysip/x86bgw": business_url,
                "baidu/nsiqa/x86bgw": qa_url,
            },
            tokens={"baidu/nsiqa/x86bgw": "50c9c669-7fbf-442f-b61a-51d2c36ef172"},
        )

        self.assertTrue(resolved["ok"])
        self.assertIn(qa_url, resolved["parameters"]["get_bgw_test_case"])
        self.assertNotIn(business_url, resolved["parameters"]["get_bgw_test_case"])

    def test_a_missing_input_names_the_parameter_instead_of_guessing(self):
        no_cr = stage_parameters.resolve(
            ["test_cr_id"], change_number=None, product_urls={}, tokens={}
        )
        no_token = stage_parameters.resolve(
            ["get_bgwagent"],
            change_number="1",
            product_urls={"baidu/sysip/bgwagent": URL},
            tokens={},
        )
        unknown = stage_parameters.resolve(
            ["some_other_input"], change_number="1", product_urls={}, tokens={}
        )

        for result, detail in (
            (no_cr, "SUBMISSION_RECEIPT_REQUIRED"),
            (no_token, "IREPO_TOKEN_REQUIRED"),
            (unknown, "PARAMETER_NOT_DERIVABLE"),
        ):
            self.assertEqual(result["reason_code"], "STAGE_PARAMETER_UNRESOLVED")
            self.assertEqual(result["detail"], detail)

    def test_evidence_never_carries_the_token(self):
        values = stage_parameters.resolve(
            ["test_cr_id", "get_bgwagent"],
            change_number="122402145",
            product_urls={"baidu/sysip/bgwagent": URL},
            tokens={"baidu/sysip/bgwagent": TOKEN},
        )["parameters"]

        safe = stage_parameters.redacted(values)

        self.assertNotIn(TOKEN, str(safe))
        self.assertIn("<IREPO-TOKEN>", safe["get_bgwagent"])
        self.assertEqual(safe["test_cr_id"], "122402145")

    def test_evidence_redacts_a_non_uuid_token(self):
        # load_tokens accepts any non-empty string, so redaction must not depend on the
        # token being UUID-shaped: a hex blob / JWT / opaque token must still be scrubbed.
        opaque = "gho_ABCdef0123456789ghIJKLmnopQRSTuvwx4242"
        values = stage_parameters.resolve(
            ["get_bgwagent"],
            change_number=None,
            product_urls={"baidu/sysip/bgwagent": URL},
            tokens={"baidu/sysip/bgwagent": opaque},
        )["parameters"]

        safe = stage_parameters.redacted(values)

        self.assertNotIn(opaque, str(safe))
        self.assertIn('--header "IREPO-TOKEN:<IREPO-TOKEN>"', safe["get_bgwagent"])

    def test_a_world_readable_token_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "credentials").mkdir()
            path = root / "credentials" / "irepo-tokens.yaml"
            path.write_text("baidu/sysip/bgwagent: %s\n" % TOKEN, encoding="utf-8")
            os.chmod(path, 0o600)
            tight = stage_parameters.load_tokens(root)
            os.chmod(path, 0o644)

            with self.assertRaises(ValueError) as refused:
                stage_parameters.load_tokens(root)

        self.assertEqual(tight, {"baidu/sysip/bgwagent": TOKEN})
        self.assertEqual(str(refused.exception), "IREPO_TOKEN_FILE_PERMISSIONS")

    def test_no_configured_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(stage_parameters.load_tokens(Path(directory)), {})


class OpenTestRepoCrTests(unittest.TestCase):
    """The CR a manual stage should pull is the one still open, not the receipt's.

    Folding T3's own CR into the requirement's existing CR leaves the newest receipt
    pointing at an abandoned change, so the open-CR list has to win.
    """

    PROFILE = {"test_repo": {"module": "baidu/sysip/bgw_auto", "path": "/tmp"}}

    def _with_reviews(self, changes, returncode=0):
        payload = json.dumps({"data": {"changes": changes}})
        calls = []

        class FakeTransport:
            def run(self, command, cwd=None, timeout=None):
                calls.append(command)
                return {"returncode": returncode, "stdout": payload, "stderr": ""}

        original = cli_transport.ProcessTransport
        cli_transport.ProcessTransport = FakeTransport
        try:
            found = orchestrator_module._open_test_repo_cr(self.PROFILE, "run-1")
        finally:
            cli_transport.ProcessTransport = original
        return found, calls

    def test_a_single_open_cr_supersedes_the_receipt(self):
        found, calls = self._with_reviews([{"_number": 122402145}])

        self.assertEqual(found, "122402145")
        self.assertIn("get_repo_reviews", calls[0])
        self.assertIn("baidu/sysip/bgw_auto", calls[0])
        self.assertIn("NEW", calls[0])

    def test_an_ambiguous_repository_leaves_the_receipt_alone(self):
        several, _ = self._with_reviews([{"_number": 1}, {"_number": 2}])
        none_open, _ = self._with_reviews([])
        unusable, _ = self._with_reviews([{"_number": 1}], returncode=1)

        self.assertIsNone(several)
        self.assertIsNone(none_open)
        self.assertIsNone(unusable)

    def test_a_project_without_a_test_repository_is_not_queried(self):
        self.assertIsNone(orchestrator_module._open_test_repo_cr({}, "run-1"))


class RevisionSetTests(unittest.TestCase):
    """What the pipeline is building: the run's CRs at their current patchset.

    A submission receipt records the revision that was pushed then. A repair or a fold
    adds a patchset without writing a new receipt, so a build of the newer patchset was
    unownable and every stage operation refused it.
    """

    PROFILE = {
        "business_repos": [{"module": "baidu/sysip/x86bgw", "path": "/repo/a", "branch": "cdn-url"}],
        "test_repo": {"module": "baidu/nsiqa/x86bgw", "path": "/repo/t", "branch": "pipline_case"},
    }

    def _orchestrator(self, *submissions):
        class Artifacts:
            def artifacts_for_run(self, _run_id):
                return [
                    {"kind": "submission", "content": json.dumps(item).encode("utf-8")}
                    for item in submissions
                ]

        state = type("LegacyState", (), {"events": lambda self, run_id: []})()
        return type("Fake", (), {"artifacts": Artifacts(), "state": state})()

    def _with_reviews(self, reviews):
        original = orchestrator_module._open_repo_reviews
        orchestrator_module._open_repo_reviews = lambda module, path: reviews.get(str(module))
        try:
            return orchestrator_module._ipipe_revision_set(
                self._orchestrator(*self.submissions), "run-1", self.PROFILE
            )
        finally:
            orchestrator_module._open_repo_reviews = original

    def setUp(self):
        self.submissions = [{
            "module": "baidu/sysip/x86bgw",
            "change_number": "122396573",
            "revision_set": {
                "business": {"module": "baidu/sysip/x86bgw", "branch": "cdn-url", "revision": "old-business"},
                "test": {"module": "baidu/nsiqa/x86bgw", "branch": "pipline_case", "revision": "old-test"},
            },
        }]

    def test_a_newer_patchset_replaces_the_receipt_revision_and_is_reported(self):
        derived = self._with_reviews({
            "baidu/sysip/x86bgw": [{"_number": 122396573, "current_revision": "new-business"}],
            "baidu/nsiqa/x86bgw": [{"_number": 122402145, "current_revision": "new-test"}],
        })

        revisions = {
            item["module"]: item["revision"]
            for item in derived["revisions"]["repositories"]
        }
        self.assertTrue(derived["ok"])
        self.assertEqual(revisions["baidu/sysip/x86bgw"], "new-business")
        # The test repository's open CR supersedes the receipt's folded-away number.
        self.assertEqual(revisions["baidu/nsiqa/x86bgw"], "new-test")
        self.assertEqual(
            derived["drift"]["baidu/sysip/x86bgw"],
            {"submitted": "old-business", "current": "new-business"},
        )

    def test_an_unanswerable_repository_keeps_the_receipt_revision(self):
        derived = self._with_reviews({})

        revisions = {
            item["module"]: item["revision"]
            for item in derived["revisions"]["repositories"]
        }
        self.assertEqual(revisions["baidu/sysip/x86bgw"], "old-business")
        self.assertEqual(revisions["baidu/nsiqa/x86bgw"], "old-test")
        self.assertEqual(derived["drift"], {})

    def test_a_module_with_no_revision_at_all_is_named_not_guessed(self):
        self.submissions = [{
            "module": "baidu/sysip/x86bgw", "change_number": "122396573",
            "revision_set": {
                "business": {"module": "baidu/sysip/x86bgw", "branch": "cdn-url", "revision": "only-business"},
            },
        }]

        derived = self._with_reviews({})

        self.assertFalse(derived["ok"])
        self.assertEqual(derived["reason_code"], "REVISION_UNRESOLVED")
        self.assertEqual(derived["module"], "baidu/nsiqa/x86bgw")

    def test_the_revision_set_id_follows_the_revisions(self):
        first = self._with_reviews({})
        moved = self._with_reviews({
            "baidu/sysip/x86bgw": [{"_number": 122396573, "current_revision": "new-business"}],
        })

        self.assertNotEqual(
            first["revisions"]["revision_set_id"], moved["revisions"]["revision_set_id"]
        )


if __name__ == "__main__":
    unittest.main()
