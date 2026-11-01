import assert from 'node:assert/strict';
import {buildWorker} from '../demo/live/build.mjs';
const code=await buildWorker();const worker=(await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))).default;
const key='test-secret-value-should-never-appear';const env={OPENAI_API_KEY:key};const calls=[];
globalThis.fetch=async(url,options={})=>{calls.push({url:String(url),method:options.method||'GET',body:options.body});const path=new URL(url).pathname;
 if(path==='/v1/agents/sessions')return Response.json({id:'sess_abc123456789012345'});
 if(path.endsWith('/events') && options.method==='POST')return new Response(null,{status:202});
 if(path.endsWith('/events')){const s=JSON.stringify({type:'message',text:'Bearer '+key+' https://example.test/path?secret=hidden'});const b=new TextEncoder().encode('data: '+s+'\n\n');return new Response(new ReadableStream({start(c){c.enqueue(b.slice(0,31));c.enqueue(b.slice(31));c.close()}}),{headers:{'Content-Type':'text/event-stream'}})}
 if(options.method==='DELETE')return Response.json({deleted:true});
 return Response.json({data:[{status:'completed',text:'Bearer '+key}],has_more:false});};
function req(path,body={},user='owner',origin='https://demo.test'){return new Request('https://demo.test'+path,{method:'POST',headers:{'Origin':origin,'X-Crashtest':'1','Content-Type':'application/json',...(user?{'oai-authenticated-user-id':user}:{})},body:JSON.stringify(body)})}
assert.equal((await worker.fetch(req('/api/start',{},null),env)).status,401);
assert.equal((await worker.fetch(req('/api/start',{},'owner','https://evil.test'),env)).status,403);
assert.equal((await worker.fetch(req('/api/start'),{})).status,503);
assert.equal((await worker.fetch(req('/api/start',{prompt:'arbitrary'}),env)).status,400);
const started=await (await worker.fetch(req('/api/start'),env)).json();assert.ok(started.ticket);assert.equal(calls.length,1);const create=JSON.parse(calls[0].body);assert.equal(create.agent.model,'gpt-6-luna');assert.equal(create.environment.type,'openai_hosted');
assert.equal((await worker.fetch(req('/api/input',{ticket:started.ticket,command:'anything'}),env)).status,400);
assert.equal((await worker.fetch(req('/api/input',{ticket:started.ticket},'different-user'),env)).status,403);
assert.equal((await worker.fetch(req('/api/input',{ticket:started.ticket}),env)).status,200);
const sent=JSON.parse(calls.find(x=>x.url.endsWith('/events')&&x.method==='POST').body);assert.match(sent.events[0].input[0].content[0].text,/Signal only the verified direct codex parent/);
const stream=await worker.fetch(req('/api/events',{ticket:started.ticket}),env);const eventText=await stream.text();assert.ok(!eventText.includes(key));assert.ok(!eventText.includes('secret=hidden'));assert.match(eventText,/\[REDACTED\]/);
const evidence=await (await worker.fetch(req('/api/evidence',{ticket:started.ticket}),env)).text();assert.ok(!evidence.includes(key));
assert.equal((await worker.fetch(req('/api/finish',{ticket:started.ticket}),env)).status,200);
console.log('Live worker smoke checks passed: private access, fixed prompt, ticket ownership, stream redaction, evidence and cleanup.');
