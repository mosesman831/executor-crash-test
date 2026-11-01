// Fixed-scenario server for a private Sites deployment. OPENAI_API_KEY is a server secret.
const PAGE = __LIVE_PAGE__;
const RECORDING = __RECORDING_PAGE__;
const TRACE = __TRACE_JSON__;
const EVENTS = __EVENTS_JSON__;
const BASE = 'https://api.openai.com/v1';
const PROMPT = `Controlled fault test in this disposable sandbox. First create /workspace/outputs/step1.txt containing exactly alpha and read it back. Then run exactly once without retrying: python3 -c 'import os,signal,time; p=os.getppid(); name=open("/proc/%d/comm"%p).read().strip(); open("/workspace/precrash-probe.txt","w").write("survived"); print("PARENT_NAME="+name,flush=True); print("SIGNALLING" if p>1 and name=="codex" else "SKIPPED",flush=True); time.sleep(3); os.kill(p,signal.SIGKILL) if p>1 and name=="codex" else None'. Signal only the verified direct codex parent. Do not signal other processes. Report the observed outcome and whether the file can be read.`;
const encoder = new TextEncoder();
const base64 = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
const unbase64 = s => Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-s.length%4)%4)), c=>c.charCodeAt(0));
const json = (data,status=200) => new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
async function mac(secret,data){const key=await crypto.subtle.importKey('raw',encoder.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['sign']);return base64(await crypto.subtle.sign('HMAC',key,encoder.encode(data)))}
async function ticket(secret,payload){const body=base64(encoder.encode(JSON.stringify(payload)));return body+'.'+await mac(secret,body)}
async function verify(secret,token,user){
 if(typeof token!=='string'||token.length>2048)return null;
 const [body,signature,...rest]=token.split('.');if(!body||!signature||rest.length)return null;
 const key=await crypto.subtle.importKey('raw',encoder.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['verify']);
 let valid=false;try{valid=await crypto.subtle.verify('HMAC',key,unbase64(signature),encoder.encode(body))}catch{return null}if(!valid)return null;
 try{const data=JSON.parse(new TextDecoder().decode(unbase64(body)));return data.user===user&&data.exp>Date.now()&&/^sess_[A-Za-z0-9_-]+$/.test(data.sid)&&/^[a-f0-9-]{36}$/.test(data.key)?data:null}catch{return null}
}
function safeString(value,secret){return value.split(secret).join('[REDACTED]').replace(/sk-[A-Za-z0-9_-]{16,}/g,'[REDACTED]').replace(/((?:https?|wss?):\/\/[^\s"'<>?#]+)\?[^\s"'<>]*/gi,'$1?[REDACTED]').replace(/Bearer\s+[^\s"']+/gi,'Bearer [REDACTED]').replace(/sess_[A-Za-z0-9_-]{12,}/g,'[SESSION]')}
function safe(value,secret){if(typeof value==='string')return safeString(value,secret);if(Array.isArray(value))return value.map(v=>safe(v,secret));if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,/^(authorization|api_key|remote_url)$/i.test(k)?'[REDACTED]':safe(v,secret)]));return value}
async function api(path,secret,options={}){return fetch(BASE+path,{...options,headers:{'Authorization':`Bearer ${secret}`,'OpenAI-Beta':'agents=v1',...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}})}
async function result(response,secret){if(!response.ok)return {error:'OpenAI request failed',http_status:response.status};try{return safe(await response.json(),secret)}catch{return {error:'OpenAI returned an unreadable response'}}}
async function page(path,secret){let after=null,records=[];for(let n=0;n<10;n++){const query='?limit=100'+(after?'&after='+encodeURIComponent(after):'');const r=await api(path+query,secret);if(!r.ok)return {error:'OpenAI request failed',http_status:r.status};const d=await r.json();records.push(...(Array.isArray(d.data)?d.data:[]));if(!d.has_more)return {data:safe(records,secret)};if(!d.last_id||d.last_id===after)return {error:'Incomplete pagination'};after=d.last_id}return {error:'Pagination limit reached'}}
function sanitisedStream(body,secret){
 const decoder=new TextDecoder();let buffer='';
 function frame(raw){const lines=raw.split(/\r?\n/);const data=lines.filter(x=>x.startsWith('data:')).map(x=>x.slice(5).replace(/^ /,''));if(!data.length)return lines.filter(x=>x.startsWith(':')).map(x=>safeString(x,secret)).join('\n')+'\n\n';const joined=data.join('\n');let cleaned;try{cleaned=JSON.stringify(safe(JSON.parse(joined),secret))}catch{cleaned=safeString(joined,secret)}const meta=lines.filter(x=>/^(event|id|retry):/.test(x)).map(x=>safeString(x,secret));return [...meta,...cleaned.split('\n').map(x=>'data: '+x)].join('\n')+'\n\n'}
 return body.pipeThrough(new TransformStream({transform(chunk,controller){buffer+=decoder.decode(chunk,{stream:true});let m;while((m=/\r?\n\r?\n/.exec(buffer))){const raw=buffer.slice(0,m.index);buffer=buffer.slice(m.index+m[0].length);controller.enqueue(encoder.encode(frame(raw))) }},flush(controller){buffer+=decoder.decode();if(buffer)controller.enqueue(encoder.encode(frame(buffer)))}}));
}
export default {async fetch(request,env){
 const url=new URL(request.url);if(url.pathname==='/'&&request.method==='GET')return new Response(PAGE,{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"}});
 if(request.method==='GET' && url.pathname==='/recording')return new Response(RECORDING,{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'}});
 if(request.method==='GET' && (url.pathname==='/trace.json'||url.pathname==='/events.json'))return new Response(url.pathname==='/trace.json'?TRACE:EVENTS,{headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
 if(!url.pathname.startsWith('/api/'))return json({error:'Not found'},404);
 const user=request.headers.get('oai-authenticated-user-id');if(!user)return json({error:'Sign in to use the live test.'},401);
 if(request.method!=='POST'||request.headers.get('x-crashtest')!=='1'||request.headers.get('origin')!==url.origin)return json({error:'Request rejected'},403);
 const secret=env.OPENAI_API_KEY;if(!secret)return json({error:'Live test is not configured yet. The server needs its OpenAI API key.'},503);
 let body;try{const text=await request.text();if(text.length>4096)return json({error:'Request too large'},413);body=JSON.parse(text||'{}')}catch{return json({error:'Invalid request'},400)}
 if(url.pathname==='/api/start'){
  if(Object.keys(body).length)return json({error:'This test takes no options'},400);
  const r=await api('/agents/sessions',secret,{method:'POST',body:JSON.stringify({agent:{model:'gpt-6-luna',instructions:'Follow the task exactly and report observed results.'},environment:{type:'openai_hosted',container_size:'small',network:{access:'disabled'}},metadata:{crashtest_scenario:'hosted_parent_signal'}})});
  if(!r.ok)return json({error:'Could not create hosted session',http_status:r.status},502);
  const d=await r.json();if(!/^sess_[A-Za-z0-9_-]+$/.test(d.id||''))return json({error:'Session response was incomplete'},502);
  return json({ticket:await ticket(secret,{sid:d.id,user,exp:Date.now()+15*60*1000,key:crypto.randomUUID()})});
 }
 if(Object.keys(body).some(k=>k!=='ticket'))return json({error:'Unexpected input'},400);
 const info=await verify(secret,body.ticket,user);if(!info)return json({error:'Session ticket is invalid or expired'},403);
 const root='/agents/sessions/'+encodeURIComponent(info.sid);
 if(url.pathname==='/api/input'){
  const r=await api(root+'/events',secret,{method:'POST',headers:{'Idempotency-Key':info.key},body:JSON.stringify({events:[{type:'agent.session.input.message',input:[{role:'user',type:'message',content:[{type:'input_text',text:PROMPT}]}]}]})});
  return r.ok?json({accepted:true}):json({error:'Input was not accepted',http_status:r.status},502);
 }
 if(url.pathname==='/api/events'){
  let r=await api(root+'/events?stream=true',secret,{headers:{'Accept':'text/event-stream'}});
  if(r.status===400)r=await api(root+'/events',secret,{headers:{'Accept':'text/event-stream'}});
  if(!r.ok||!r.body)return json({error:'Event stream unavailable',http_status:r.status},502);
  return new Response(sanitisedStream(r.body,secret),{headers:{'Content-Type':'text/event-stream; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
 }
 if(url.pathname==='/api/evidence'){
  const [session,turns,items,traces]=await Promise.all([api(root,secret).then(r=>result(r,secret)),page(root+'/turns',secret),page(root+'/items',secret),page(root+'/traces',secret)]);
  return json({session,turns,items,traces});
 }
 if(url.pathname==='/api/finish'){
  const r=await api(root,secret,{method:'DELETE'});return r.ok?json({deleted:true}):json({error:'Could not delete session',http_status:r.status},502);
 }
 return json({error:'Not found'},404);
}};
