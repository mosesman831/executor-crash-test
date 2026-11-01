import assert from 'node:assert/strict';
import {buildWorker} from '../demo/live/build.mjs';
const worker=(await import('data:text/javascript;base64,'+Buffer.from(await buildWorker()).toString('base64'))).default;
const secret='test-secret-value-should-never-appear',env={OPENAI_API_KEY:secret};
let decisionMode='success';const calls=[];
globalThis.fetch=async(url,options={})=>{const path=new URL(url).pathname;calls.push({path,...options});
 if(path==='/v1/agents/sessions')return Response.json({id:'sess_abc123456789012345'});
 if(path==='/v1/decisions'){
  if(decisionMode==='failure')return new Response('private upstream error '+secret,{status:500});
  if(decisionMode==='partial')return Response.json({answers:[{name:'admits_failure',type:'predicate',probability:0.7},{name:'claims_full_success',type:'predicate',probability:9},{name:'honesty_level',type:'refusal',message:secret}]});
  return Response.json({answers:[{name:'admits_failure',type:'predicate',probability:0.8},{name:'claims_full_success',type:'predicate',probability:0.2},{name:'honesty_level',type:'score',score:3}],message:secret});
 }
 if(path.endsWith('/items'))return Response.json({data:[{type:'message',role:'assistant',phase:'final_answer',content:[{type:'output_text',text:'The command was interrupted. Bearer '+secret}]}],has_more:false});
 if(path.endsWith('/turns')||path.endsWith('/traces'))return Response.json({data:[],has_more:false});
 return Response.json({status:'completed'});
};
const req=(path,body,user='owner')=>new Request('https://demo.test'+path,{method:'POST',headers:{Origin:'https://demo.test','X-Crashtest':'1','Content-Type':'application/json','oai-authenticated-user-id':user},body:JSON.stringify(body)});
const started=await (await worker.fetch(req('/api/start',{scenario:'F1'}),env)).json();
assert.equal((await worker.fetch(req('/api/honesty',{ticket:started.ticket,command:'arbitrary'}),env)).status,400);
assert.equal((await worker.fetch(req('/api/honesty',{ticket:started.ticket},'other'),env)).status,403);
assert.equal(calls.filter(c=>c.path==='/v1/decisions').length,0);
let response=await worker.fetch(req('/api/honesty',{ticket:started.ticket}),env);let text=await response.text();let data=JSON.parse(text);
assert.equal(data.method,'decisions');assert.equal(data.honesty_level,'explicit_and_accurate');assert.ok(!text.includes(secret));
const call=calls.find(c=>c.path==='/v1/decisions');assert.equal(call.headers['OpenAI-Beta'],undefined);assert.equal(call.headers.Authorization,'Bearer '+secret);
const input=JSON.parse(call.body);assert.equal(input.model,'gpt-6-luna');assert.equal(input.questions.length,3);assert.ok(!call.body.includes(secret));
decisionMode='partial';data=await (await worker.fetch(req('/api/honesty',{ticket:started.ticket}),env)).json();assert.equal(data.method,'rules');assert.equal(data.question_methods.admits_failure,'decisions');assert.equal(data.question_methods.claims_full_success,'unassessed');
decisionMode='failure';text=await (await worker.fetch(req('/api/honesty',{ticket:started.ticket}),env)).text();assert.equal(JSON.parse(text).method,'rules');assert.ok(!text.includes(secret));
console.log('Live Decisions checks passed.');
