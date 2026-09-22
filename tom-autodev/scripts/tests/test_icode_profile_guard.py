"""A-05: profile drift is checked before iCode writes, including cached clients."""
import copy
import hashlib
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import profile_repin
import workflow_spec
from clients.icode_client import IcodeClient
from orchestrator import Orchestrator
import test_icode_runtime as icode_fixture
from test_icode_runtime import approved
from test_ipipe_runtime import PROFILE
from test_orchestrator import _write_profile


class IcodeProfileGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = icode_fixture.IcodeRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        subprocess.run(['git', '-C', str(f.source_repo), 'branch', '-M', 'main'], check=True)
        remote = f.root / 'local-remote.git'
        subprocess.run(['git', 'clone', '--bare', '-q', str(f.source_repo), str(remote)], check=True)
        subprocess.run(['git', '-C', str(f.repo), 'remote', 'add', 'origin', str(remote)], check=True)
        profile = copy.deepcopy(PROFILE)
        profile['project_id'] = 'bgw'
        _write_profile(f.root, profile)
        self.path = f.root / 'config/projects/bgw.yaml'
        self.profile = yaml.safe_load(self.path.read_text())
        self.profile['business_repos'][0].update(path=str(f.source_repo), module='baidu/team/repo')
        self.profile['test_repo']['module'] = 'baidu/team/repo-tests'
        self.path.write_text(yaml.safe_dump(self.profile))
        self.orch = Orchestrator(f.root)
        self.run = 'run-1'
        self.orch.state.transition(self.run, 'INTAKE', {
            'project': 'bgw', 'requirement_id': 'CARD-1', 'profile_path': str(self.path),
            'profile_hash': hashlib.sha256(self.path.read_bytes()).hexdigest(),
            'workflow_spec_hash': workflow_spec.canonical_hash(),
        })
        self.orch.state.transition(self.run, 'SUBMIT', {'previous_state': 'REVIEW'})
        self.change = f.child_change_set()
        self.approval = approved(self.orch.approvals, self.run, 'G7', self.change['input_hash'])
        self.runtime = self.factory()
        self.assertFalse(isinstance(self.runtime, dict), self.runtime)
        f.transport.on_push = self.published

    def factory(self):
        f = self.fixture
        return self.orch.icode_runtime(
            self.run, worktree_bindings=f.runtime().worktree_bindings,
            system_skill_path=f.skill, argv_transport=f.transport,
            binary_candidates=['/fake/icode'], executable_resolver=lambda value: value, owner='dev')

    def published(self):
        self.fixture.transport.changes = [{
            '_number': 42, 'current_revision': self.change['commit_revision'], 'branch': 'main',
            'owner': {'username': 'dev'}, 'subject': 'CARD-1 exact',
            'url': 'https://icode.example/cr/42', 'module': 'baidu/team/repo',
        }]

    def drift(self):
        changed = copy.deepcopy(self.profile)
        changed['environment_profile']['image_digest'] = 'sha256:changed-after-g7'
        self.path.write_text(yaml.safe_dump(changed))

    def snapshot(self):
        with closing(sqlite3.connect(self.orch.state.database_path)) as connection:
            return '\n'.join(connection.iterdump())

    def submit(self):
        return self.runtime.submit(self.change, self.approval)

    def assert_blocked_without_effects(self, invoke, reason='PROFILE_CONFLICT'):
        before, calls = self.snapshot(), list(self.fixture.transport.calls)
        result = invoke()
        self.assertEqual(result.get('reason_code'), reason, result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fixture.transport.calls, calls)

    def test_controller_refuses_drift_before_entering_any_runtime(self):
        self.drift()
        calls = []
        adapter = SimpleNamespace(run_id=self.run, submit=lambda *args: calls.append(args))
        self.assert_blocked_without_effects(lambda: self.orch.submit_to_ipipe(
            self.run, self.change, self.approval, icode_runtime=adapter))
        self.assertEqual(calls, [])

    def test_cached_factory_and_compatibility_runtime_refuse_drift_before_preflight(self):
        self.drift()
        self.assert_blocked_without_effects(self.submit)
        self.assert_blocked_without_effects(
            lambda: IcodeClient(runtime=self.runtime).submit(self.change, self.approval))

    def test_factory_refuses_a_currently_drifted_profile(self):
        self.drift()
        result = self.factory()
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get('reason_code'), 'PROFILE_CONFLICT')

    def test_missing_profile_refuses_cached_runtime(self):
        self.path.unlink()
        self.assert_blocked_without_effects(self.submit, 'PROJECT_NOT_READY')

    def test_cached_policy_mutation_cannot_change_the_pinned_submission_action(self):
        self.runtime.submission_policy = 'one_cr_per_repo'
        self.assert_blocked_without_effects(self.submit)

    def test_factory_cannot_override_pinned_policy_or_profile_identity(self):
        for option, value, reason in (
            ('submission_policy', 'one_cr_per_repo', 'PROFILE_CONFLICT'),
            ('profile_hash', 'a' * 64, 'RUNTIME_OPTION_FORBIDDEN'),
        ):
            with self.subTest(option=option):
                self.assert_blocked_without_effects(
                    lambda: self.orch.icode_runtime(self.run, **{option: value}), reason)

    def test_drift_during_preflight_prevents_fetch_intent_and_push(self):
        original = self.fixture.transport.run
        checkpoint = {}

        def invoke(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[-1] == 'login':
                self.drift()
                checkpoint['state'] = self.snapshot()
            return result

        self.fixture.transport.run = invoke
        result = self.submit()
        self.assertEqual(result.get('reason_code'), 'PROFILE_CONFLICT', result)
        self.assertEqual(self.snapshot(), checkpoint['state'])
        self.assertFalse((self.fixture.source_repo / '.git/FETCH_HEAD').exists())
        self.assertFalse(any('push_cr' in c[0] for c in self.fixture.transport.calls))

    def test_drift_during_cr_query_prevents_push_and_receipt(self):
        original = self.fixture.transport.run
        checkpoint = {}

        def invoke(argv, **kwargs):
            result = original(argv, **kwargs)
            if 'get_repo_reviews' in argv:
                self.drift()
                checkpoint['state'] = self.snapshot()
            return result

        self.fixture.transport.run = invoke
        result = self.submit()
        self.assertEqual(result.get('reason_code'), 'PROFILE_CONFLICT', result)
        self.assertEqual(self.snapshot(), checkpoint['state'])
        self.assertFalse(any('push_cr' in c[0] for c in self.fixture.transport.calls))

    def test_drift_after_push_preserves_unknown_intent_without_claiming_success(self):
        checkpoint = {}

        def pushed():
            self.published()
            self.drift()
            checkpoint['state'] = self.snapshot()

        self.fixture.transport.on_push = pushed
        result = self.submit()
        self.assertEqual(result.get('reason_code'), 'PROFILE_CONFLICT', result)
        self.assertEqual(self.snapshot(), checkpoint['state'])
        self.assertEqual(len(self.orch.state.pending_intents(self.run)), 1)
        self.assertEqual(sum('push_cr' in c[0] for c in self.fixture.transport.calls), 1)

    def test_drift_during_query_of_matching_cr_does_not_write_success_receipt(self):
        original = self.fixture.transport.run
        checkpoint = {}

        def invoke(argv, **kwargs):
            result = original(argv, **kwargs)
            if 'get_repo_reviews' in argv and self.fixture.transport.changes:
                self.drift()
                checkpoint['state'] = self.snapshot()
            return result

        self.fixture.transport.run = invoke
        result = self.submit()
        self.assertEqual(result.get('reason_code'), 'PROFILE_CONFLICT', result)
        self.assertEqual(self.snapshot(), checkpoint['state'])
        self.assertEqual(len(self.orch.state.pending_intents(self.run)), 1)
        self.assertEqual(sum('push_cr' in c[0] for c in self.fixture.transport.calls), 1)

    def test_drift_after_repository_materialization_stops_before_push(self):
        original = self.runtime._materialize_cli_repository
        checkpoint = {}

        def materialize(*args):
            result = original(*args)
            self.drift()
            checkpoint['state'] = self.snapshot()
            return result

        self.runtime._materialize_cli_repository = materialize
        result = self.submit()
        self.assertEqual(result.get('reason_code'), 'PROFILE_CONFLICT', result)
        self.assertEqual(self.snapshot(), checkpoint['state'])
        self.assertFalse(any('push_cr' in c[0] for c in self.fixture.transport.calls))

    def test_unknown_submission_cannot_reconcile_or_push_after_drift(self):
        self.fixture.transport.on_push = None
        self.fixture.transport.push_result = TimeoutError('unknown')
        self.assertEqual(self.submit()['reason_code'], 'SUBMIT_RESULT_UNKNOWN')
        self.published()
        self.drift()
        self.assert_blocked_without_effects(self.submit)

    def test_approved_repin_invalidates_old_client_and_new_factory_uses_new_policy(self):
        previous = self.fixture.root / 'previous.yaml'
        previous.write_bytes(self.path.read_bytes())
        self.profile['submission_policy'] = 'one_cr_per_repo'
        self.path.write_text(yaml.safe_dump(self.profile))
        prepared = profile_repin.plan(self.orch, self.run, previous)
        self.assertTrue(prepared.get('ok'), prepared)
        decision = approved(self.orch.approvals, self.run, profile_repin.ACTION, prepared['input_hash'])
        applied = profile_repin.apply(self.orch, self.run, decision['approval_id'], previous)
        self.assertTrue(applied.get('ok'), applied)
        self.assert_blocked_without_effects(self.submit)
        self.runtime = self.factory()
        self.assertEqual(self.runtime.submission_policy, 'one_cr_per_repo')
        self.assertEqual(self.submit()['reason_code'], 'OK')

    def test_matching_controller_receipt_is_read_only_after_profile_drift(self):
        receipt = {'ok': True, 'reason_code': 'OK', 'state': 'IPIPE'}
        self.orch.state.save_idempotency_result('submit-to-ipipe:run-1:change-1:revisions-1', receipt)
        self.drift()
        before = self.snapshot()
        result = self.orch.submit_to_ipipe(
            self.run, self.change, self.approval, icode_runtime=self.runtime)
        self.assertEqual(result, receipt)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fixture.transport.calls, [])

    def test_unchanged_pinned_runtime_submits_and_records_exact_revision(self):
        result = self.submit()
        self.assertEqual(result['reason_code'], 'OK', result)
        self.assertEqual(result['commit_revision'], self.change['commit_revision'])
        self.assertEqual(sum('push_cr' in c[0] for c in self.fixture.transport.calls), 1)


if __name__ == '__main__':
    unittest.main()
