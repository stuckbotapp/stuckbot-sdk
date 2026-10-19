import {test,describe,beforeEach,afterEach} from "node:test";
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {mkdtempSync,readFileSync,rmSync,statSync,writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {ClaimLostError,FleetAgent,StuckBotError,WorkItem} from "../index.js";
import {Mock,err,iso,ok,waitFor} from "./helpers.js";
const INDEX=new URL("../index.js",import.meta.url).href;
class FakeFleet{
constructor(m){
this.m=m;
this.ttl=3600000;
this.n=0;
this.valid=new Map();
this.renewable=new Map();
this.handoff=null;
this.stop=null;
m.on("POST","/api/v1/agents/register",r=>r.token==="sbe_secret"?ok(this.issue(this.agent(this.n+1)),{},201):err(401,"invalid_token"));
m.on("POST","/api/v1/agents/me/token",r=>{
const a=this.valid.get(r.token)??this.renewable.get(r.token);
return a?ok(this.issue(a)):err(401,"token_expired");
});
m.on("GET","/api/v1/agents/me",this.guard(a=>ok({
agent:a,
org_id:"o",
retry_after_seconds:60,
})));
m.on("POST","/api/v1/agents/me/heartbeat",this.guard(()=>ok({
ok:true,
handoff_requested:this.handoff,
stop_requested:this.stop,
retry_after_seconds:60,
token_expires_soon:false,
})));
}
agent(i){
return {
id:`00000000-0000-4000-8000-${String(i).padStart(12,"0")}`,
name:`fleet/agent-${i}`,
};
}
issue(a){
this.n++;
const token=`sba_${this.n}`;
this.valid.set(token,a);
return {
token,
token_type:"Bearer",
expires_at:iso(Date.now()+this.ttl),
agent:a,
};
}
guard(fn){
return r=>{
const a=this.valid.get(r.token);
return a?fn(a,r):err(401,"token_expired");
};
}
expire(token,renewable=false){
const a=this.valid.get(token);
this.valid.delete(token);
if(renewable){
this.renewable.set(token,a);
}
}
}
let m;
let f;
let dir;
let state;
let agents;
function make(options={}){
const a=new FleetAgent({
enrollmentSecret:"sbe_secret",
baseUrl:m.url,
maxRetries:0,
heartbeatInterval:3600000,
...options,
});
agents.push(a);
return a;
}
function mode(path){
return statSync(path).mode&0o777;
}
function readState(){
return JSON.parse(readFileSync(state,"utf8"));
}
beforeEach(async()=>{
m=await new Mock().start();
f=new FakeFleet(m);
dir=mkdtempSync(join(tmpdir(),"sbsdk-"));
state=join(dir,"agent.json");
agents=[];
});
afterEach(async()=>{
for(const a of agents){
a.stop();
}
await m.close();
rmSync(dir,{
recursive:true,
force:true,
});
});
describe("registration and tokens",()=>{
test("registers with labels and sends heartbeats with the payload",async()=>{
const a=await make({
labels:{
host:"ci-7",
},
provider:"anthropic",
supportsStop:true,
heartbeatPayload:()=>({
usage:{
tokens:5,
},
status:"busy",
}),
}).start();
const reg=m.calls("POST","/api/v1/agents/register")[0];
assert.equal(reg.headers.authorization,"Bearer sbe_secret");
assert.deepEqual(reg.json,{
labels:{
host:"ci-7",
},
provider:"anthropic",
supports_stop:true,
});
assert.ok(reg.headers["idempotency-key"]);
assert.ok(await waitFor(()=>m.calls("POST","/api/v1/agents/me/heartbeat").length));
const hb=m.calls("POST","/api/v1/agents/me/heartbeat")[0];
assert.deepEqual(hb.json,{
status:"busy",
usage:{
tokens:5,
},
});
assert.equal(hb.token,a.token);
});
test("the state file is 0600 and a restart reuses the agent",async()=>{
const a=await make({
stateFile:state,
}).start();
a.stop();
assert.equal(mode(state),0o600);
assert.deepEqual([readState().agent_id,readState().token],[a.agentId,a.token]);
const b=await make({
stateFile:state,
}).start();
assert.equal(b.agentId,a.agentId);
assert.equal(b.registrations,0);
assert.equal(m.calls("POST","/api/v1/agents/register").length,1);
assert.equal(m.calls("GET","/api/v1/agents/me")[0].token,a.token);
});
test("a rejected saved token registers a new agent",async()=>{
const a=await make({
stateFile:state,
}).start();
a.stop();
f.expire(a.token);
const b=await make({
stateFile:state,
}).start();
assert.notEqual(b.agentId,a.agentId);
assert.equal(b.registrations,1);
assert.equal(readState().agent_id,b.agentId);
assert.equal(mode(state),0o600);
});
test("an expired saved token registers without trying it",async()=>{
writeFileSync(state,JSON.stringify({
base_url:m.url,
agent_id:"x",
token:"sba_old",
expires_at:iso(Date.now()-10000),
}));
const b=await make({
stateFile:state,
}).start();
assert.equal(b.registrations,1);
assert.equal(m.calls("GET","/api/v1/agents/me").length,0);
});
test("a 401 renews the token once and retries the call",async()=>{
const a=await make().start();
const old=a.token;
f.expire(old,true);
const me=await a.client.getMe();
assert.equal(me.agent.id,a.agentId);
assert.notEqual(a.token,old);
const seq=m.requests.filter(r=>r.path!=="/api/v1/agents/me/heartbeat").map(r=>[r.method,r.path,r.token]);
assert.deepEqual(seq.slice(-3),[
["GET","/api/v1/agents/me",old],
["POST","/api/v1/agents/me/token",old],
["GET","/api/v1/agents/me",a.token],
]);
assert.equal(a.renewals,1);
});
test("a 401 registers again when renewal fails",async()=>{
const a=await make().start();
const first=a.agentId;
f.expire(a.token);
const me=await a.client.getMe();
assert.notEqual(a.agentId,first);
assert.equal(me.agent.id,a.agentId);
assert.equal(a.registrations,2);
});
test("the 401 retry happens only once",async()=>{
const a=await make().start();
m.on("GET","/api/v1/agents/me",err(401,"token_expired"));
await assert.rejects(a.client.getMe(),e=>e instanceof StuckBotError&&e.code==="token_expired");
assert.equal(m.calls("GET","/api/v1/agents/me").length,2);
});
test("the token is renewed before it expires",async()=>{
f.ttl=3000;
const a=await make({
stateFile:state,
renewBefore:2500,
}).start();
const first=a.token;
assert.ok(await waitFor(()=>a.renewals>=1&&readState().token===a.token,3000));
assert.notEqual(a.token,first);
assert.equal(readState().token,a.token);
assert.equal(m.calls("POST","/api/v1/agents/me/token")[0].token,first);
});
test("handoff and stop requests reach the callbacks once",async()=>{
const seen=[];
const a=await make({
supportsStop:true,
onHandoffRequested:h=>seen.push(["handoff",h.reason]),
onStopRequested:s=>seen.push(["stop",s.reason]),
}).start();
await a.heartbeat();
assert.equal(a.handoffRequested,null);
f.handoff={
at:iso(Date.now()),
reason:"budget",
signal_id:null,
};
await a.heartbeat();
await a.heartbeat();
f.stop={
at:iso(Date.now()),
reason:"done",
};
await a.heartbeat();
assert.deepEqual(seen,[
["handoff","budget"],
["stop","done"],
]);
f.handoff=null;
await a.heartbeat();
assert.equal(a.handoffRequested,null);
assert.equal(a.stopRequested.reason,"done");
});
});
describe("polling and leases",()=>{
test("poll yields claimable tickets and updates, honoring Retry-After and the ETag",async()=>{
m.on("GET","/api/v1/work",ok({
untrusted_content_notice:"n",
claimable:[
{
id:"t1",
work:"claim",
},
{
id:"t2",
work:"continue",
},
],
updates:[
{
event_id:3,
ticket_id:"t9",
type:"ticket.claimed",
},
],
verifications_waiting:0,
handoff_requested:null,
stop_requested:null,
next_cursor:"c1",
retry_after_seconds:1,
},{
"ETag":"W/\"e1\"",
"Retry-After":"1",
}),{
status:304,
headers:{
"ETag":"W/\"e1\"",
"Retry-After":"1",
},
body:null,
});
const a=await make().start();
setTimeout(()=>a.stop(),2600);
const items=[];
for await(const i of a.poll()){
items.push([i.kind,i.ticketId]);
}
assert.deepEqual(items,[
["claim","t1"],
["continue","t2"],
["update","t9"],
]);
const calls=m.calls("GET","/api/v1/work");
assert.ok(calls.length>=2);
assert.deepEqual(calls[0].query,{});
assert.deepEqual(calls[1].query,{
cursor:"c1",
});
assert.equal(calls[1].headers["if-none-match"],"W/\"e1\"");
assert.ok(calls[1].at-calls[0].at>=950);
assert.equal(a.cursor,"c1");
});
function leaseRoutes(seconds){
const id="0b4f6c1e-1111-4222-8333-944445555666";
const ticket=s=>{
const now=Date.now();
return ok({
untrusted_content_notice:"n",
ticket:{
id,
ref:"SB-1",
state:s,
claim:{
started_at:iso(now),
expires_at:iso(now+seconds*1000),
},
},
});
};
m.on("POST",`/api/v1/tickets/${id}/claim`,()=>ticket("claimed"));
m.on("POST",`/api/v1/tickets/${id}/claim/renew`,()=>ticket("claimed"));
m.on("POST",`/api/v1/tickets/${id}/release`,()=>ticket("open"));
m.on("POST",`/api/v1/tickets/${id}/submit`,()=>ticket("blocker_pending_verification"));
return id;
}
test("withClaim renews the lease at a third of its length",async()=>{
const id=leaseRoutes(3);
const a=await make().start();
await a.withClaim(id,async lease=>{
assert.ok(Math.abs(lease.renewEvery-1000)<50);
await new Promise(r=>setTimeout(r,3500));
});
const renews=m.calls("POST",`/api/v1/tickets/${id}/claim/renew`);
assert.equal(renews.length,3);
for(let i=1;i<renews.length;i++){
assert.ok(Math.abs(renews[i].at-renews[i-1].at-1000)<250,`gap ${renews[i].at-renews[i-1].at}`);
}
assert.equal(m.calls("POST",`/api/v1/tickets/${id}/release`).length,0);
await new Promise(r=>setTimeout(r,1200));
assert.equal(m.calls("POST",`/api/v1/tickets/${id}/claim/renew`).length,3);
});
test("withClaim releases on error, but not after submit",async()=>{
const id=leaseRoutes(1800);
const a=await make().start();
await assert.rejects(a.withClaim(id,async()=>{
throw new RangeError("solver crashed");
}),RangeError);
const rel=m.calls("POST",`/api/v1/tickets/${id}/release`);
assert.equal(rel.length,1);
assert.match(rel[0].json.note,/RangeError/);
await assert.rejects(a.withClaim(id,async lease=>{
await lease.submit("blocker");
throw new Error("after submit");
}));
assert.equal(m.calls("POST",`/api/v1/tickets/${id}/release`).length,1);
});
test("a lost claim stops renewing",async()=>{
const id=leaseRoutes(1800);
m.on("POST",`/api/v1/tickets/${id}/claim/renew`,err(409,"not_claim_holder"));
const a=await make().start();
await a.withClaim(new WorkItem("claim",{
id,
}),async lease=>{
await new Promise(r=>setTimeout(r,700));
assert.equal(lease.lost,true);
assert.equal(lease.error.code,"not_claim_holder");
assert.equal(lease.held,false);
},{
renewEvery:200,
});
assert.equal(m.calls("POST",`/api/v1/tickets/${id}/claim/renew`).length,1);
});
test("a lost claim throws ClaimLostError before the callback runs",async()=>{
const id=leaseRoutes(1800);
m.on("POST",`/api/v1/tickets/${id}/claim`,err(409,"already_claimed"));
const a=await make().start();
let ran=false;
await assert.rejects(a.withClaim(id,async()=>{
ran=true;
}),ClaimLostError);
assert.equal(ran,false);
assert.equal(a._leases.size,0);
assert.equal(m.calls("POST",`/api/v1/tickets/${id}/release`).length,0);
});
test("retireOnStop retires the agent and deletes the state file",async()=>{
m.on("POST","/api/v1/agents/me/retire",r=>f.valid.has(r.token)?ok({
retired:true,
released:[
"SB-1",
],
}):err(401,"token_revoked"));
const a=await make({
stateFile:state,
}).start();
await a.stop();
assert.equal(m.calls("POST","/api/v1/agents/me/retire").length,0);
readState();
const b=await make({
stateFile:state,
retireOnStop:true,
}).start();
const token=b.token;
await b.stop();
assert.deepEqual(m.calls("POST","/api/v1/agents/me/retire").map(r=>r.token),[token]);
assert.throws(()=>readState(),/ENOENT/);
assert.equal(b.agent,null);
await b.stop();
assert.equal(m.calls("POST","/api/v1/agents/me/retire").length,1);
});
test("a failed retire keeps the state file and reports the error",async()=>{
m.on("POST","/api/v1/agents/me/retire",err(503,"unavailable"));
const seen=[];
const a=await make({
stateFile:state,
retireOnStop:true,
onError:e=>seen.push(e.code),
}).start();
await a.stop();
assert.equal(readState().agent_id,a.agentId);
assert.deepEqual(seen,[
"unavailable",
]);
});
test("stop ends heartbeats",async()=>{
const a=await make({
heartbeatInterval:100,
}).start();
assert.ok(await waitFor(()=>m.calls("POST","/api/v1/agents/me/heartbeat").length>=3,3000));
a.stop();
const n=m.calls("POST","/api/v1/agents/me/heartbeat").length;
assert.ok(n>=3,`${n} heartbeats`);
await new Promise(r=>setTimeout(r,300));
assert.equal(m.calls("POST","/api/v1/agents/me/heartbeat").length,n);
});
test("background timers do not keep the process alive",async()=>{
const code=`import {FleetAgent,Lease} from ${JSON.stringify(INDEX)};
const a=new FleetAgent({enrollmentSecret:"sbe_secret",baseUrl:${JSON.stringify(m.url)},heartbeatInterval:60000,renewBefore:1000});
await a.start();
const now=Date.now();
new Lease(a.client,{id:"0b4f6c1e-1111-4222-8333-944445555666",state:"claimed",claim:{started_at:new Date(now).toISOString(),expires_at:new Date(now+600000).toISOString()}})._start();
console.log("started",a.agentId);`;
const t=performance.now();
const out=await new Promise((resolve,reject)=>{
const p=spawn(process.execPath,[
"--input-type=module",
"-e",
code,
]);
let s="";
p.stdout.on("data",d=>s+=d);
p.stderr.on("data",d=>s+=d);
const kill=setTimeout(()=>{
p.kill();
reject(new Error(`child still running: ${s}`));
},8000);
p.on("exit",c=>{
clearTimeout(kill);
resolve({
code:c,
s,
});
});
});
assert.equal(out.code,0,out.s);
assert.match(out.s,/started 00000000-0000-4000-8000-000000000001/);
assert.ok(performance.now()-t<5000);
});
});
test("a stale response cannot clear or repeat a handoff",()=>{
const calls=[];
const a=new FleetAgent({enrollmentSecret:"sbe_test",baseUrl:"http://127.0.0.1:9",onHandoffRequested:r=>calls.push(r)});
const req={at:"2026-10-19T21:00:00Z",reason:"budget",signal_id:"s1"};
const old=a._seq();
const now=a._seq();
a._flags({handoff_requested:req},now);
a._flags({handoff_requested:null},old);
assert.deepEqual(a.handoffRequested,req);
a._flags({handoff_requested:req},a._seq());
assert.equal(calls.length,1);
a._flags({handoff_requested:{...req,signal_id:"s2"}},a._seq());
assert.equal(calls.length,2);
});
