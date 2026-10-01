"""Execute the actual gateway and installed card SDK with all I/O replaced.

No credentials, sockets, real journal, platform calls or running gateway are used.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


_GATEWAY = Path(__file__).resolve().parents[2] / "infoflow-gateway"
_SOURCE = _GATEWAY / "src" / "bot_gateway.mjs"

_HARNESS = r"""
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { dirname } from 'node:path';
const require = createRequire(GATEWAY + '/package.json');
// Real SDK field mapping and limit checks; only the HTTP boundary is fake.
const { BubbleWorkflowCardApi } = require('@baidu/infoflow-sdk-nodejs');
const rendered = [], journal = [], handlers = {}, files = new Map();
let httpHandler, failRender = false;
const sdk = new BubbleWorkflowCardApi({
  async post(path, body) {
    assert.equal(path, '/robot/card/bubble-workflow-card/render');
    rendered.push(body);
    if (failRender) throw new Error('unknown remote outcome');
    return { code: 'ok', data: { errcode: 0, data: {
      ok: true, recognized: true, updated: true, created: true,
      card_info: { card_id: body.card_id, revision: 1 },
    }}};
  },
}, { debug() {} });
const context = vm.createContext({
  process: { env: { INFOFLOW_APP_KEY: 'fake-key', INFOFLOW_APP_SECRET: 'fake-secret' } },
  console: { log() {} }, Buffer, URL,
  fetch: async (url) => {
    if (url.endsWith('/api/v1/auth/app_access_token')) {
      return { json: async () => ({ data: { app_access_token: 'fake-token', expire: 7200 } }) };
    }
    if (url.endsWith('/api/v1/imRobot/detail')) {
      return { status: 200, json: async () => ({ data: { agentId: 1 } }) };
    }
    if (url.endsWith('/api/v1/robot/group/create')) {
      return {
        status: 200,
        json: async () => ({ code: 0, data: { errcode: 0, data: { groupid: '9001' } } }),
      };
    }
    if (url.endsWith('/api/v1/robot/group/getGroupInfo')) {
      return {
        status: 200,
        json: async () => ({ data: { errcode: 0, data: { groupId: '9001', name: 'BGW-19-syncookie' } } }),
      };
    }
    if (url.endsWith('/api/v1/robot/group/memberList')) {
      return {
        status: 200,
        json: async () => ({ data: { errcode: 0, data: { userInfoList: [{ email: 'owner@example.test' }] } } }),
      };
    }
    throw new Error('unexpected outbound request: ' + url);
  },
});
const modules = {
  'node:http': { createServer(handler) {
    httpHandler = handler;
    return { listen() {} };
  }},
  'node:fs': {
    existsSync(path) { return files.has(path); },
    readFileSync(path) {
      if (files.has(path)) return files.get(path);
      throw new Error('must not read credentials');
    },
    mkdirSync() {},
    appendFileSync(path, text) {
      journal.push(JSON.parse(text));
      files.set(path, (files.get(path) || '') + text);
    },
  },
  'node:crypto': { createHash },
  'node:path': { dirname },
  'node:os': { homedir: () => '/nonexistent-test-home' },
  '@baidu/infoflow-sdk-nodejs': {
    LogLevel: { info: 1 },
    WSClient: class {
      on(key, handler) { handlers[key] = handler; }
      async connect() {}
    },
    Client: class { constructor() { this.im = { bubbleWorkflowCard: sdk }; } },
  },
};
const module = new vm.SourceTextModule(SOURCE + `
export { sendApprovalCard, answerCard, journalInteraction, live, renderCard };
`, { context });
await module.link(async (name) => {
  assert.ok(modules[name], 'unexpected import: ' + name);
  const exports = modules[name];
  return new vm.SyntheticModule(Object.keys(exports), function() {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, { context });
});
await module.evaluate();
const api = module.namespace;
const request = {
  approval_id: 'a'.repeat(32), target_type: 'user', target_id: 'owner',
  title: '审批', question: '请先核对材料',
  lines: Array.from({ length: 7 }, (_, i) => '材料' + i),
  approvers: ['owner', 'qa'],
};
async function post(path, body) {
  const events = {};
  const req = {
    method: 'POST', url: path, setEncoding() {},
    on(key, fn) {
      events[key] = fn;
      if (key === 'error') queueMicrotask(() => {
        events.data(JSON.stringify(body)); events.end();
      });
    },
  };
  let status, payload;
  await httpHandler(req, {
    writeHead(code) { status = code; },
    end(text) { payload = JSON.parse(text); },
  });
  return { status, payload };
}
"""


@unittest.skipUnless(shutil.which("node"), "node is required for gateway execution tests")
class InfoflowGatewayTests(unittest.TestCase):
    def _run(self, assertions):
        script = (
            f"const GATEWAY = {json.dumps(str(_GATEWAY))};\n"
            f"const SOURCE = {json.dumps(_SOURCE.read_text())};\n"
            + _HARNESS + assertions
        )
        result = subprocess.run(
            ["node", "--experimental-vm-modules", "--input-type=module", "-"],
            input=script, text=True, capture_output=True, timeout=20, cwd=_GATEWAY,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_user_group_endpoint_and_private_card_identity(self):
        self._run(r"""
const owner = await post('/card/approval', request);
const qa = await post('/card/approval', { ...request, target_id: 'qa' });
const group = await post('/card/approval', { ...request, target_type: 'group', target_id: '123' });
const retry = await post('/card/approval', request);
assert.equal(owner.status, 200);
assert.equal(qa.status, 200);
assert.equal(group.status, 200);
assert.notEqual(owner.payload.card_id, qa.payload.card_id);
assert.notEqual(owner.payload.card_id, group.payload.card_id);
assert.equal(owner.payload.card_id, retry.payload.card_id);
assert.deepEqual(rendered.map(x => x.receiver), [
  { type: 'user', id: 'owner' }, { type: 'user', id: 'qa' },
  { type: 'group', id: '123' }, { type: 'user', id: 'owner' },
]);
assert.equal(rendered[0].card.components.length, 7);
assert.equal(rendered[0].card.buttons.length, 2);
assert.equal(rendered[0].card.buttons[0].event_key, 'approve.' + request.approval_id);
assert.equal(rendered[0].refresh_default_intermediate_state, false);
""")

    def test_approver_click_locks_card_as_selected_and_keeps_seven_materials(self):
        self._run(r"""
const initial = await api.sendApprovalCard(request);
await api.answerCard({
  cardId: initial.card_id, cardInstanceId: 'instance-owner',
}, 'approve.' + request.approval_id, 'owner');
const card = rendered.at(-1);
assert.equal(card.question, '已由 owner 处理：同意');
assert.equal(card.card.buttons.length, 0);
assert.equal(card.card.components.length, 8);
assert.deepEqual(Array.from(card.card.components.slice(0, 7), x => x.text), request.lines);
assert.equal(card.card.components.at(-1).component_id, 'click-status');
assert.match(card.card.components.at(-1).text, /^已选择「同意」· owner · /);
assert.equal(card.card_instance_id, 'instance-owner');
assert.equal(api.live.has(initial.card_id), false);
""")

    def test_outsider_click_keeps_buttons_and_claims_no_decision(self):
        self._run(r"""
const initial = await api.sendApprovalCard({ ...request, target_type: 'group', target_id: '123' });
for (const button of ['approve', 'reject']) {
  await api.answerCard({
    cardId: initial.card_id, cardInstanceId: 'instance-group',
  }, button + '.' + request.approval_id, 'not-an-authorized-member');
  const card = rendered.at(-1);
  assert.match(card.question, /待权限和有效期校验/);
  assert.doesNotMatch(card.question, /已批准|已同意|已由.*处理/);
  assert.equal(card.card.buttons.length, 2);
  assert.equal(card.card.components.length, 8);
  assert.deepEqual(Array.from(card.card.components.slice(0, 7), x => x.text), request.lines);
  assert.match(card.card.components.at(-1).text, /^点击待校验/);
}
// The approver can still answer with one tap after the onlooker's clicks.
assert.equal(api.live.has(initial.card_id), true);
await api.answerCard({ cardId: initial.card_id }, 'approve.' + request.approval_id, 'qa');
assert.equal(rendered.at(-1).question, '已由 qa 处理：同意');
assert.equal(rendered.at(-1).card.buttons.length, 0);
""")

    def test_restart_without_known_approvers_keeps_the_card_answerable(self):
        self._run(r"""
const initial = await api.sendApprovalCard(request);
api.live.clear();
await api.answerCard({
  cardId: initial.card_id, receiver: { type: 'user', id: 'owner' },
  caller: { requestId: request.approval_id },
}, 'approve.' + request.approval_id, 'owner');
const card = rendered.at(-1);
assert.match(card.question, /待权限和有效期校验/);
assert.equal(card.card.buttons.length, 2);
""")

    def test_restart_refresh_removes_previous_click_status_and_keeps_target(self):
        self._run(r"""
const initial = await api.sendApprovalCard(request);
api.live.clear();
await api.answerCard({
  cardId: initial.card_id, cardInstanceId: 'instance-owner',
  receiver: { type: 'user', id: 'owner' }, caller: { requestId: request.approval_id },
  submit: { components: [
    { componentId: 'line-0', type: 'text', label: '固定材料' },
    { component_id: 'click-status', type: 'text', label: '过时点击状态' },
  ] },
}, 'approve.' + request.approval_id, 'owner');
const card = rendered.at(-1);
assert.deepEqual(card.receiver, { type: 'user', id: 'owner' });
assert.equal(card.card.components.length, 2);
assert.equal(card.card.components[0].text, '固定材料');
assert.match(card.card.components[1].text, /点击待校验「同意」/);
assert.equal(card.card.buttons.length, 2);
""")

    def test_component_limit_initial_and_repeated_clicks(self):
        self._run(r"""
const initial = await api.sendApprovalCard({
  ...request, lines: Array.from({ length: 10 }, (_, i) => '材料' + i),
});
assert.equal(rendered[0].card.components.length, 7);
await api.answerCard({ cardId: initial.card_id }, 'approve.' + request.approval_id, 'owner');
assert.equal(rendered.at(-1).card.components.length, 8);
const short = await api.sendApprovalCard({ ...request, target_id: 'qa', lines: ['材料'] });
// Repeated onlooker taps must not accumulate a status line either.
for (let i = 0; i < 3; i++) {
  await api.answerCard({ cardId: short.card_id }, 'reject.' + request.approval_id, 'outsider');
  assert.equal(rendered.at(-1).card.components.length, 2);
}
await api.answerCard({ cardId: short.card_id }, 'reject.' + request.approval_id, 'qa');
assert.equal(rendered.at(-1).card.components.length, 2);
assert.equal(rendered.at(-1).question, '已由 qa 处理：驳回');
""")

    def test_journal_callback_records_untrusted_click_without_approving(self):
        self._run(r"""
const initial = await api.sendApprovalCard({ ...request, target_type: 'group', target_id: '123' });
handlers['event.interaction_submit']({ data: {
  eventId: 'click-1', cardId: initial.card_id, groupId: '123', userId: 'outsider',
  submit: { eventKey: 'approve.' + request.approval_id },
}});
await new Promise(resolve => setImmediate(resolve));
assert.equal(journal.length, 1);
assert.equal(journal[0].text, 'APPROVE ' + request.approval_id);
assert.equal(journal[0].sender, 'outsider'); // authorization belongs to the consumer
assert.equal(journal[0].group_id, '123');
assert.equal(journal[0].chat_type, 'group');
assert.match(rendered.at(-1).question, /待权限和有效期校验/);
assert.equal(rendered.at(-1).card.buttons.length, 2);
""")

    def test_render_failure_is_not_retried_and_invalid_endpoint_does_not_send(self):
        self._run(r"""
const bad = await post('/card/approval', { ...request, target_type: 'single' });
assert.equal(bad.status, 400);
assert.equal(rendered.length, 0);
failRender = true;
const failed = await post('/card/approval', request);
assert.equal(failed.status, 500);
assert.equal(rendered.length, 1);
assert.equal(api.live.size, 0);
""")

    def test_group_create_reads_nested_receipt_and_uses_requested_bot_identity(self):
        self._run(r"""
const created = await post('/group/create', {
  group_name: 'BGW-19-syncookie',
  owner: 'owner@example.test',
  members: ['owner@example.test'],
  friendly_level: 3,
  idempotency_key: 'infoflow.group.create:run-1',
});
assert.equal(created.status, 200);
assert.deepEqual(created.payload, {
  ok: true, group_id: '9001', bot_id: '1', group_name: 'BGW-19-syncookie',
});
const recovered = await post('/group/receipt', {
  idempotency_key: 'infoflow.group.create:run-1',
});
assert.equal(recovered.status, 200);
assert.equal(recovered.payload.receipt.group_id, '9001');
assert.equal(recovered.payload.receipt.bot_id, '1');
const verified = await post('/group/verify', { group_id: '9001' });
assert.equal(verified.status, 200);
assert.deepEqual(verified.payload.verification, {
  group_id: '9001', group_name: 'BGW-19-syncookie',
  members_queried: true, member_emails: ['owner@example.test'],
});
""")


if __name__ == "__main__":
    unittest.main()
