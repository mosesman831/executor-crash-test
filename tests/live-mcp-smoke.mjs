import assert from 'node:assert/strict';
import { buildWorker } from '../demo/live/build.mjs';
import { webcrypto } from 'node:crypto';

globalThis.crypto ??= webcrypto;
const secret = 'sk-test-only-12345678901234567890';
const sessionId = 'sess_test12345678901234567890';
const rows = new Map();
const calls = [];
const DB = { prepare(sql) { return { bind(...args) { return {
  async first() { assert.match(sql, /SELECT/i); return rows.get(args[0]) ?? null; },
  async run() {
    if (/INSERT/i.test(sql)) { const [user_id, scenario, ticket, session_prefix, created_at] = args; if (rows.has(user_id)) throw Error('UNIQUE constraint failed: shared_runs.user_id'); rows.set(user_id, {user_id, scenario, ticket, session_prefix, created_at}); }
    else if (/UPDATE/i.test(sql)) { const [ticket, session_prefix, created_at, user_id, oldTicket] = args; const row = rows.get(user_id); if (row && row.ticket === oldTicket) Object.assign(row, {ticket, session_prefix, created_at}); }
    else if (/DELETE/i.test(sql)) { const row = rows.get(args[0]); if (row && (args.length === 1 || row.ticket === args[1])) rows.delete(args[0]); }
    else throw Error(`Unexpected SQL: ${sql}`);
    return {success:true, meta:{changes:1}};
  },
}; } }; } };
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, options = {}) => {
  const parsed = new URL(url); calls.push({path:parsed.pathname, method:options.method || 'GET', body:options.body});
  assert.equal(options.headers.Authorization, `Bearer ${secret}`);
  const path = parsed.pathname.replace('/v1/agents/sessions', '');
  const response = value => Response.json(value);
  if (path === '' && options.method === 'POST') return response({id:sessionId});
  if (path === `/${sessionId}/events` && options.method === 'POST') return response({accepted:true});
  if (path === `/${sessionId}` && options.method === 'DELETE') return response({deleted:true});
  if (path === `/${sessionId}`) return response({id:sessionId, status:'completed', note:`Bearer ${secret}`});
  if (path === `/${sessionId}/turns`) return response({data:[{id:'turn_1',status:'completed'}],has_more:false});
  if (path === `/${sessionId}/items`) return response({data:[{type:'command_execution',status:'completed',exit_code:0}],has_more:false});
  if (path === `/${sessionId}/traces`) return response({data:[{note:secret}],has_more:false});
  if (path === `/${sessionId}/artifacts`) return response({data:[{id:'artifact_1',turn_id:'turn_1',path:'/workspace/outputs/step1.txt'}],has_more:false});
  if (path === `/${sessionId}/artifacts/artifact_1/content`) return new Response('alpha');
  throw Error(`Unexpected OpenAI request: ${options.method || 'GET'} ${parsed}`);
};
try {
  const source = await buildWorker();
  const worker = (await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)).default;
  const env = {OPENAI_API_KEY:secret, DB};
  async function request(path, body, user = 'owner') {
    const headers = {'content-type':'application/json', 'origin':'https://example.test', 'x-crashtest':'1'};
    if (user) headers['oai-authenticated-user-id'] = user;
    const response = await worker.fetch(new Request(`https://example.test${path}`, {method:'POST',headers,body:JSON.stringify(body)}), env);
    const text = await response.text();
    assert(!text.includes(secret), `Secret leaked in ${path}`);
    return {status:response.status, value:JSON.parse(text)};
  }
  let id = 0;
  const rpc = (method, params = {}, user = 'owner') => request('/mcp', {jsonrpc:'2.0',id:++id,method,params}, user);
  const tool = (name, args = {}, user = 'owner') => rpc('tools/call', {name, arguments:args}, user);
  const data = response => {
    assert.equal(response.status, 200, JSON.stringify(response.value));
    assert(!response.value.error, JSON.stringify(response.value));
    assert(!response.value.result?.isError, JSON.stringify(response.value));
    const result = response.value.result;
    const content = result?.content?.find(item => item.type === 'text')?.text;
    try { return JSON.parse(content); } catch { return content ?? result; }
  };
  const discovery = data(await rpc('server/discover'));
  assert(discovery.supportedVersions.includes('2026-07-28'));
  const list = data(await rpc('tools/list'));
  for (const name of ['start_test','get_run_status','get_report','end_test']) {
    const entry = list.tools.find(item => item.name === name);
    assert(entry?.inputSchema, `missing ${name} schema`);
    assert.equal(entry.inputSchema.additionalProperties, false);
  }
  const unauthorized = await tool('start_test', {scenario:'F0'}, null);
  assert(unauthorized.status === 401 || unauthorized.value.error || unauthorized.value.result?.isError);
  const rejected = await tool('start_test', {scenario:'F0',command:'echo unsafe'});
  assert(rejected.value.error || rejected.value.result?.isError || rejected.status >= 400);
  assert.equal(calls.filter(call => call.path.endsWith('/sessions')).length, 0);
  const started = data(await tool('start_test', {scenario:'F0'}));
  const duplicate = await tool('start_test', {scenario:'F0'});
  assert(duplicate.value.error || duplicate.value.result?.isError || duplicate.status >= 400);
  assert.equal(calls.filter(call => call.path.endsWith('/sessions') && call.method === 'POST').length, 1);
  const shared = await request('/api/shared', {});
  assert.equal(shared.status, 200);
  assert.equal(shared.value.scenario, 'F0');
  assert(shared.value.ticket && shared.value.session_prefix);
  assert.equal(rows.get('owner').ticket, shared.value.ticket);
  assert.equal((await request('/api/shared', {}, 'other')).value.ticket, null);
  assert.equal((await request('/api/evidence', {ticket:shared.value.ticket}, 'other')).status, 403);
  const status = data(await tool('get_run_status'));
  const report = data(await tool('get_report'));
  for (const value of [started, status, report, duplicate.value]) assert(!JSON.stringify(value).includes(shared.value.ticket), 'MCP exposed the shared ticket');
  assert.match(JSON.stringify(report), /PASS/);
  const ended = data(await tool('end_test'));
  assert(!JSON.stringify(ended).includes(shared.value.ticket), 'MCP exposed the shared ticket');
  assert.equal((await request('/api/shared', {})).value.ticket, null);
  assert(calls.some(call => call.path === `/v1/agents/sessions/${sessionId}` && call.method === 'DELETE'));
  console.log('MCP shared-run smoke test passed');
} finally { globalThis.fetch = originalFetch; }
