"""Cross-layer regressions for immutable pipeline and release input identity."""
import copy
import hashlib
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import Orchestrator, _recorded_submissions, _ipipe_revision_set
from test_task7_controller_boundary import profile_fixture, FakeIcodeRuntime, KnowledgeFake
from test_ipipe_runtime import FakeApi, approved
from clients.ipipe_runtime import IpipeRuntime
from datetime import datetime, timedelta, timezone
import worker_driver


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class PipelinePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = profile_fixture()
        self.profile['business_repos'] = [
            {'path': '/repo/'+m, 'module': m, 'branch': 'main', 'lock': m} for m in ['A', 'B']]
        self.profile['test_repo']['module'] = 'tests'
        for repository in self.profile['business_repos'] + [self.profile['test_repo']]:
            repository['path'] = str(self.root / repository['module'])
            subprocess.run(['git', 'init', '-q', repository['path']], check=True)
        for tag in ('t1', 't2'):
            repo = self.profile['test_repo']['path']
            subprocess.run(['git', '-C', repo, '-c', 'user.email=fixture@example.test',
                            '-c', 'user.name=Fixture', 'commit', '-q', '--allow-empty', '-m', tag], check=True)
            subprocess.run(['git', '-C', repo, 'tag', tag], check=True)
        for field in ('language_skill', 'project_skill'):
            skill = self.root / field
            skill.mkdir()
            (skill / 'SKILL.md').write_text('fixture')
            self.profile[field] = str(skill)
        self.profile['pipeline_profile']['pipelines'] = [
            {'module': m, 'pipeline_id': 'pipe-'+m, 'stage_classes': ['unit'],
             'required_for_release': True, 'depends_on': []} for m in ['A', 'B']]
        self.path = self.root / 'config/projects/bgw.yaml'
        self.path.parent.mkdir(parents=True)
        self._write_profile()
        self.orch = Orchestrator(self.root)
        self.run = 'run-plan'
        self.orch.state.transition(self.run, 'INTAKE', {
            'requirement_id': 'BGW-1', 'project': 'bgw', 'profile_path': str(self.path),
            'profile_hash': hashlib.sha256(self.path.read_bytes()).hexdigest()})
        self.knowledge = KnowledgeFake()
        self.protocol = self.orch.phase_protocol(self.knowledge)
        self.api = FakeApi()

    def _write_profile(self):
        self.path.write_text(json.dumps(self.profile))

    def _descriptor(self, module, revision, change_id, tests='t1', task=None, tests_only=False):
        value = {'run_id': self.run, 'change_set_id': change_id,
                 'revision_set_id': digest([module, revision, tests]),
                 'repo_path': str(self.root/module), 'module': module, 'target_branch': 'main',
                 'commit_revision': revision, 'card_id': 'BGW-1', 'owner': 'dev',
                 'revision_set': {'business': {'module': module, 'branch': 'main', 'revision': revision},
                                  'test': {'module': 'tests', 'branch': 'main', 'revision': tests}}}
        if tests_only:
            value.update(module='tests', commit_revision=tests, repo_path=str(self.root/'tests'))
        value['input_hash'] = digest(value)
        self.orch.artifacts.put(self.run, 'change-set', json.dumps({k: v for k, v in value.items() if k != 'input_hash'}).encode(), {
            'task_id': task or 'task-'+module, 'verdict': 'PASS'})
        return value

    def _submit(self, descriptor, expect_ok=True):
        self.orch.state.transition(self.run, 'SUBMIT', {'previous_state': 'REVIEW'})
        approval = approved(self.orch.approvals, 'G7', descriptor['input_hash'], self.run)
        with patch('orchestrator.load_profile', return_value={'ready': True, 'profile': self.profile}):
            result = self.orch.submit_to_ipipe(self.run, descriptor, approval,
                icode_runtime=FakeIcodeRuntime(self.orch.state, self.run))
        if expect_ok:
            self.assertTrue(result['ok'], result)
        return result

    def _start_plan(self):
        a = self._descriptor('A', 'a1', 'A-1')
        b = self._descriptor('B', 'b1', 'B-1')
        self._submit(a)
        self._submit(b)
        return self.orch.state.events(self.run)[-1]['payload']

    def _runtime(self):
        return IpipeRuntime(self.orch.state, self.orch.approvals, self.run, self.api,
                            validated_profile=self.profile,
                            max_polls=1, sleeper=lambda _: None,
                            profile_hash=hashlib.sha256(self.path.read_bytes()).hexdigest())

    def _build(self, module, build_id, status='SUCCESS', expect_ok=True):
        from pipeline_plan import frozen_plan
        plan = frozen_plan(self.orch.state.events(self.run))
        target = plan['modules'][module]
        self.api.pipeline = {'id': target['pipeline_id'], 'module': module}
        platform_status = 'FAILED' if status == 'FAILURE' else status
        self.api.builds[build_id] = {
            'id': build_id, 'pipelineConfId': target['pipeline_id'], 'module': module,
            'revision': target['expected_revisions'][module],
            'revisions': {r['module']: r['revision'] for r in plan['revision_set']['repositories']},
            'params': {}, 'status': platform_status,
            'stageBuilds': [{'id': build_id+'-stage', 'class': 'unit', 'status': platform_status}],
        }
        self.api.candidates = [self.api.builds[build_id]]
        runtime = self._runtime()
        result = runtime.discover(self.profile, plan['revision_set'], module)
        self.assertTrue(result['ok'], result)
        monitored = runtime.monitor(build_id, (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat())
        self.assertEqual(monitored['status'], status, monitored)
        content = worker_driver._build_ipipe_evidence(target, monitored)
        result = self.protocol.ingest_ipipe_evidence(self.run, content)
        if expect_ok:
            self.assertIn(result['reason_code'], ['OK', 'PIPELINE_EVIDENCE_INCOMPLETE'], result)
        return result

    def _publish(self, build_id):
        durable = self.orch.state.idempotency_result(f'ipipe.build-binding:{self.run}:{build_id}')
        binding = durable['binding']
        self.api.releases.append({
            'id': 'release-'+build_id, 'module': binding['module'], 'branch': binding['target_branch'],
            'pipelineBuildId': build_id, 'revisions': binding['revision_map'],
            'releaseRule': binding['release_rule'], 'status': 'SUCCESS'})

    def _release(self):
        action = self.protocol.next(self.run)
        self.assertTrue(action['ok'], action)
        self.assertEqual(action['state'], 'RELEASE')
        approved(self.orch.approvals, 'G9', action['input_hash'], self.run)
        return worker_driver._execute_release(self.orch, self.run, action, self.api, self.knowledge)

    def test_current_submission_uses_reviewed_revision_not_change_id_sort(self):
        self._start_plan()
        latest = self._descriptor('A', 'a2', 'A-2')
        self._submit(latest)
        payload = self.orch.state.events(self.run)[-1]['payload']
        self.assertEqual(payload['source_revisions']['business'], 'a2')
        a = [s for s in _recorded_submissions(self.orch, self.run)
             if s['controller_binding']['module'] == 'A']
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]['revision_set_id'], latest['revision_set_id'])

    def test_same_change_id_new_revision_must_not_reuse_old_submission(self):
        self._start_plan()
        changed = self._descriptor('A', 'a2', 'A-1')
        from orchestrator import _outstanding_submissions
        self.assertIn('task-A', _outstanding_submissions(self.orch, self.run))
        self._submit(changed)
        self.assertEqual(self.orch.state.events(self.run)[-1]['payload']['source_revisions']['business'], 'a2')

    def test_plan_is_frozen_and_does_not_follow_live_cr_patchsets(self):
        payload = self._start_plan()
        self.assertIn('pipeline_plan', payload)
        with patch('orchestrator._current_patchset', side_effect=AssertionError('no live selection')):
            revisions = _ipipe_revision_set(self.orch, self.run, self.profile)
        self.assertTrue(revisions['ok'], revisions)
        self.assertEqual(revisions['revisions'], payload['pipeline_plan']['revision_set'])

    def test_explicit_independence_and_default_dependency_closure(self):
        from pipeline_plan import create_plan
        payload = self._start_plan()
        plan = payload['pipeline_plan']
        self.assertEqual(plan['modules']['A']['expected_revisions'], {'A': 'a1', 'tests': 't1'})
        conservative = copy.deepcopy(self.profile)
        for entry in conservative['pipeline_profile']['pipelines']:
            entry.pop('depends_on')
        full = create_plan(self.orch, self.run, conservative)
        self.assertEqual(full['modules']['A']['expected_revisions'], {'A': 'a1', 'B': 'b1', 'tests': 't1'})

    def test_shared_tests_are_current_for_every_module(self):
        self._start_plan()
        self._submit(self._descriptor('B', 'b2', 'B-2', tests='t2'))
        plan = self.orch.state.events(self.run)[-1]['payload']['pipeline_plan']
        self.assertEqual(plan['modules']['A']['source_revisions'], {'business': 'a1', 'tests': 't2'})

    def test_repaired_a_cannot_release_using_old_a_success_and_new_b(self):
        self._start_plan()
        self._build('A', 'a-old')
        self._publish('a-old')
        self._build('B', 'b-fail', 'FAILURE')
        self.assertEqual(self.orch.state.events(self.run)[-1]['state'], 'DIAGNOSE')
        self._submit(self._descriptor('A', 'a2', 'A-2'))
        self.assertEqual(self.protocol.ipipe_outstanding_modules(self.run), ['A', 'B'])
        self._build('B', 'b-new')
        self._publish('b-new')
        self.assertEqual(self.orch.state.events(self.run)[-1]['state'], 'IPIPE')
        self.assertEqual(self.protocol.ipipe_outstanding_modules(self.run), ['A'])
        self._build('A', 'a-new')
        result = self._release()
        self.assertEqual(result.get('parked'), 'RELEASE_WAITING', result)
        self.assertEqual(result['module'], 'A')
        self._publish('a-new')
        # Restart uses the same plan and durable bindings, without current CR reads.
        self.orch = Orchestrator(self.root)
        self.protocol = self.orch.phase_protocol(self.knowledge)
        result = self._release()
        self.assertEqual(result.get('state'), 'RELEASE_SUCCESS', result)
        content = self.orch.artifacts.latest_phase(self.run, 'RELEASE', None)['envelope']['content']
        self.assertEqual({m:p['build_id'] for m,p in content['module_releases'].items()},
                         {'A':'a-new', 'B':'b-new'})

    def test_independent_unchanged_a_reused_after_b_repair(self):
        self._start_plan()
        self._build('A', 'a-old')
        self._publish('a-old')
        self._build('B', 'b-fail', 'FAILURE')
        self._submit(self._descriptor('B', 'b2', 'B-2'))
        self.assertEqual(self.protocol.ipipe_outstanding_modules(self.run), ['B'])
        self._build('B', 'b-new')
        self._publish('b-new')
        result = self._release()
        self.assertEqual(result.get('state'), 'RELEASE_SUCCESS', result)

    def test_test_change_and_undeclared_dependency_each_invalidate_a(self):
        self._start_plan()
        self._build('A', 'a-old')
        self._build('B', 'b-fail', 'FAILURE')
        self._submit(self._descriptor('B', 'b2', 'B-2', tests='t2'))
        self.assertEqual(self.protocol.ipipe_outstanding_modules(self.run), ['A', 'B'])
        from pipeline_plan import create_plan, successful_builds
        conservative = copy.deepcopy(self.profile)
        for entry in conservative['pipeline_profile']['pipelines']:
            entry.pop('depends_on')
        plan = create_plan(self.orch, self.run, conservative)
        self.assertFalse(successful_builds(self.orch.artifacts, self.orch.state, self.run, plan))

    def test_direct_release_cannot_bypass_other_modules_or_platform_verifier(self):
        self._start_plan()
        self._build('A', 'a-ok')
        self._build('B', 'b-ok')
        self._publish('a-ok')
        self._publish('b-ok')
        action = self.protocol.next(self.run)
        self.assertEqual(len(action['input_artifacts']), 2)
        approval = approved(self.orch.approvals, 'G9', action['input_hash'], self.run)
        from pipeline_plan import frozen_plan
        plan = frozen_plan(self.orch.state.events(self.run))
        last = plan['modules']['B']
        fabricated = {'pipeline_id': last['pipeline_id'], 'module':'B', 'build_id':'b-ok',
                      'revisions':last['source_revisions'], 'release_id':'release-b-ok',
                      'environment_fingerprint':last['environment_fingerprint'],
                      'release_rule':last['release_rule'], 'status':'SUCCESS',
                      'release_evidence':['ipipe:release/release-b-ok'],
                      'remote_evidence_refs':['ipipe:release/release-b-ok']}
        rejected = self.protocol.ingest_release_evidence(self.run, fabricated, approval)
        self.assertEqual(rejected['reason_code'], 'PIPELINE_PLAN_MISMATCH')
        fabricated['pipeline_plan_hash'] = plan['plan_hash']
        fabricated['module_releases'] = {}
        rejected = self.protocol.ingest_release_evidence(self.run, fabricated, approval)
        self.assertFalse(rejected['ok'])
        self.assertEqual(self.orch.state.events(self.run)[-1]['state'], 'RELEASE')

    def test_plan_tamper_and_wrong_build_binding_fail_closed(self):
        payload = self._start_plan()
        from pipeline_plan import frozen_plan
        payload = copy.deepcopy(payload)
        payload['pipeline_plan']['modules']['A']['source_revisions']['business'] = 'forged'
        self.orch.state.transition(self.run, 'IPIPE', payload)
        with self.assertRaisesRegex(ValueError, 'PIPELINE_PLAN_INVALID'):
            frozen_plan(self.orch.state.events(self.run))
        self.assertEqual(self.protocol.next(self.run)['reason_code'], 'PIPELINE_PLAN_INVALID')

    def test_newer_shared_test_revision_wins_even_when_older_snapshot_reviewed_last(self):
        a = self._descriptor('A', 'a1', 'A-1', tests='t2')
        b = self._descriptor('B', 'b1', 'B-1', tests='t1')
        self._submit(a)
        self._submit(b)
        from pipeline_plan import frozen_plan
        plan = frozen_plan(self.orch.state.events(self.run))
        self.assertEqual(plan['modules']['A']['source_revisions']['tests'], 't2')
        self.assertEqual(plan['modules']['B']['source_revisions']['tests'], 't2')

    def test_conflicting_unverifiable_test_revisions_stop_at_submit(self):
        a = self._descriptor('A', 'a1', 'A-1', tests='missing-revision')
        b = self._descriptor('B', 'b1', 'B-1', tests='t1')
        self._submit(a)
        result = self._submit(b, expect_ok=False)
        self.assertEqual(result['reason_code'], 'REVISION_AMBIGUOUS', result)
        self.assertEqual(self.orch.state.events(self.run)[-1]['state'], 'SUBMIT')

    def test_identical_inputs_new_receipt_reuse_and_finalize_without_retrigger(self):
        self._start_plan()
        self._build('A', 'a-ok')
        self._build('B', 'b-ok')
        self._publish('a-ok')
        self._publish('b-ok')
        old_action = self.protocol.next(self.run)
        self._submit(self._descriptor('B', 'b1', 'B-2'))
        self.assertEqual(self.protocol.ipipe_outstanding_modules(self.run), [])
        self.assertEqual(worker_driver._finalize_ipipe(self.orch, self.run, self.knowledge)['state'], 'RELEASE')
        new_action = self.protocol.next(self.run)
        self.assertNotEqual(new_action['input_hash'], old_action['input_hash'])
        self.assertEqual(self._release().get('state'), 'RELEASE_SUCCESS')

    def test_crash_after_final_build_archive_can_finalize_same_plan(self):
        self._start_plan()
        self._build('A', 'a-ok')
        with patch.object(self.orch.state, 'commit_transition_result', side_effect=RuntimeError('crash')):
            result = self._build('B', 'b-ok', expect_ok=False)
        self.assertFalse(result['ok'])
        self.assertEqual(self.orch.state.events(self.run)[-1]['state'], 'IPIPE')
        self.orch = Orchestrator(self.root)
        self.protocol = self.orch.phase_protocol(self.knowledge)
        self.assertEqual(worker_driver._finalize_ipipe(self.orch, self.run, self.knowledge)['state'], 'RELEASE')

    def test_missing_durable_binding_does_not_fall_back_to_older_success(self):
        self._start_plan()
        self._build('A', 'a-ok')
        with self.orch.state._connect() as connection:
            connection.execute('DELETE FROM idempotency_results WHERE idempotency_key = ?',
                               (f'ipipe.build-binding:{self.run}:a-ok',))
        with self.assertRaisesRegex(ValueError, 'BUILD_BINDING_MISMATCH'):
            self.protocol.ipipe_outstanding_modules(self.run)

    def test_successful_build_without_complete_remote_release_revisions_waits(self):
        self._start_plan()
        self._build('A', 'a-ok')
        self._build('B', 'b-ok')
        self._publish('a-ok')
        self._publish('b-ok')
        self.api.releases[0]['revisions'].pop('tests')
        self.assertEqual(self._release().get('parked'), 'RELEASE_WAITING')
        self.assertEqual(self.orch.state.events(self.run)[-1]['state'], 'RELEASE')

    def test_new_review_without_descriptor_cannot_reuse_prior_submission(self):
        self._start_plan()
        from test_phase_protocol_repair import final_envelope
        from test_schema_validation import specialized_examples
        review = copy.deepcopy(specialized_examples()['review'])
        self.orch.artifacts.put_envelope(final_envelope(self.run, 'REVIEW', 'task-A', review))
        from pipeline_plan import current_submissions, current_descriptors
        self.assertNotIn('task-A', current_descriptors(self.orch, self.run))
        submissions = current_submissions(self.orch, self.run)
        self.assertEqual([s['controller_binding']['module'] for s in submissions], ['B'])

    def test_planless_legacy_release_cannot_write_success(self):
        self.orch.state.transition(self.run, 'RELEASE', {})
        self.assertEqual(self.protocol.next(self.run)['reason_code'], 'PIPELINE_PLAN_REQUIRED')

    def test_dependency_graph_follows_transitive_inputs_and_rejects_unknown_modules(self):
        self._start_plan()
        from pipeline_plan import create_plan
        profile = copy.deepcopy(self.profile)
        profile['pipeline_profile']['pipelines'][0]['depends_on'] = ['B']
        profile['pipeline_profile']['pipelines'][1].pop('depends_on')
        expected = create_plan(self.orch, self.run, profile)['modules']['A']['expected_revisions']
        self.assertEqual(expected, {'A': 'a1', 'B': 'b1', 'tests': 't1'})
        profile['pipeline_profile']['pipelines'][0]['depends_on'] = ['missing']
        from project_registry import validate_profile
        self.assertFalse(validate_profile(profile, check_paths=False)['ready'])

    def test_tests_only_submission_updates_business_pipeline_test_input(self):
        self._start_plan()
        self._submit(self._descriptor('A', 'a1', 'A-tests', tests='t2', tests_only=True))
        self._build('A', 'a-tests')
        self._build('B', 'b-tests')
        self._publish('a-tests')
        self._publish('b-tests')
        self.assertEqual(self._release().get('state'), 'RELEASE_SUCCESS')

    def test_required_test_repository_pipeline_is_part_of_aggregate(self):
        self.profile['pipeline_profile']['pipelines'].append({
            'module': 'tests', 'pipeline_id': 'pipe-tests', 'stage_classes': ['unit'],
            'required_for_release': True, 'depends_on': []})
        self._write_profile()
        self.run = 'run-with-test-pipeline'
        self.orch.state.transition(self.run, 'INTAKE', {
            'requirement_id': 'BGW-1', 'project': 'bgw', 'profile_path': str(self.path),
            'profile_hash': hashlib.sha256(self.path.read_bytes()).hexdigest()})
        self._start_plan()
        for module in ['A', 'B', 'tests']:
            self._build(module, 'build-'+module)
            self._publish('build-'+module)
        self.assertEqual(self._release().get('state'), 'RELEASE_SUCCESS')
        content = self.orch.artifacts.latest_phase(self.run, 'RELEASE', None)['envelope']['content']
        self.assertEqual(set(content['module_releases']), {'A', 'B', 'tests'})

    def test_fabricated_complete_aggregate_needs_runtime_verification_receipts(self):
        self._start_plan()
        self._build('A', 'a-ok')
        self._build('B', 'b-ok')
        from pipeline_plan import frozen_plan
        plan = frozen_plan(self.orch.state.events(self.run))
        proofs = {}
        for module in plan['required_modules']:
            build_id = module.lower()+'-ok'
            target = plan['modules'][module]
            proofs[module] = {'pipeline_id': target['pipeline_id'], 'module': module,
                'build_id': build_id, 'release_id': 'invented-'+module, 'status': 'SUCCESS',
                'revisions': target['source_revisions'], 'release_rule': target['release_rule'],
                'environment_fingerprint': target['environment_fingerprint'],
                'release_evidence': ['ipipe:release/invented'], 'remote_evidence_refs': ['ipipe:release/invented']}
        content = {**proofs['B'], 'module_releases': proofs, 'pipeline_plan_hash': plan['plan_hash']}
        action = self.protocol.next(self.run)
        approval = approved(self.orch.approvals, 'G9', action['input_hash'], self.run)
        rejected = self.protocol.ingest_release_evidence(self.run, content, approval)
        self.assertEqual(rejected['reason_code'], 'RELEASE_VERIFICATION_REQUIRED')
        self.assertEqual(self.orch.state.events(self.run)[-1]['state'], 'RELEASE')

    def test_profile_change_cannot_use_old_planned_release_binding(self):
        self._start_plan()
        self._build('A', 'a-ok')
        from pipeline_plan import frozen_plan
        plan = frozen_plan(self.orch.state.events(self.run))
        runtime = self._runtime()
        runtime.validated_profile = copy.deepcopy(self.profile)
        runtime.validated_profile['pipeline_profile']['pipelines'][0]['stage_classes'] = ['integration']
        before = list(self.api.calls)
        result = runtime.verify_planned_release('a-ok', plan['modules']['A'])
        self.assertEqual(result['reason_code'], 'PIPELINE_PLAN_PROFILE_MISMATCH')
        self.assertEqual(self.api.calls, before)


if __name__ == '__main__':
    unittest.main()
