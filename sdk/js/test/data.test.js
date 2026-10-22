import {test,beforeEach,afterEach} from "node:test";
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {Client,PrivateData,StuckBotError,mergeBody} from "../index.js";
import {Mock,err,ok} from "./helpers.js";
const sha=b=>`sha256:${createHash("sha256").update(b).digest("hex")}`;
let sb;
let store;
let c;
let objects;
let tamper;
beforeEach(async()=>{
sb=await new Mock().start();
store=await new Mock().start();
objects=new Map();
tamper=false;
store.answer=r=>{
const m=/^\/objects\/(sha256:[0-9a-f]{64})$/.exec(r.path);
if(!m){
return err(404,"not_found");
}
if(r.method==="PUT"){
if(r.token!=="grant_write"){
return err(401,"invalid_grant","Bad grant.");
}
if(sha(r.body)!==m[1]){
return err(422,"digest_mismatch","The bytes do not match.");
}
const had=objects.has(m[1]);
objects.set(m[1],r.body);
return ok({
object:m[1],
},{},had?200:201);
}
if(r.token!=="grant_read"){
return err(401,"invalid_grant","Bad grant.");
}
const body=objects.get(m[1]);
return body?{
status:200,
headers:{
"Content-Type":"application/octet-stream",
},
body:tamper?Buffer.concat([body,Buffer.from("x")]):body,
}:err(404,"not_found");
};
sb.on("POST","/api/v1/data-stores/main/upload-grants",ok({
store:"main",
url:`${store.url}/objects/`,
token:"grant_write",
expires_at:"2026-10-21T15:10:00Z",
max_bytes:104857600,
note:"n",
}));
c=new Client({
token:"sba_test",
baseUrl:sb.url,
backoff:10,
timeout:5000,
});
});
afterEach(async()=>{
await sb.close();
await store.close();
});
function grants(refs){
sb.on("POST","/api/v1/tickets/SB-7/data-grants",r=>ok({
expires_at:"2026-10-21T15:05:00Z",
grants:refs.filter(x=>!r.json?.objects||r.json.objects.includes(x.object)).map(x=>x.available===false?x:{
...x,
name:x.name??null,
available:true,
url:`${store.url}/objects/${x.object}`,
token:"grant_read",
}),
note:"n",
}));
}
test("putData stores bytes on the data store and getData reads them back verified",async()=>{
const body={
objective:"Ship v2",
stuck_reason:"jest fails",
context:{
errors:[
"boom",
],
},
};
const b=await c.putData("main",body,{
kind:"body",
});
const raw=Buffer.from(JSON.stringify(body));
assert.deepEqual(b,{
store:"main",
object:sha(raw),
kind:"body",
size:raw.length,
media_type:"application/json",
});
const f=await c.putData("main","line one\nlíne two",{
name:"run.log",
mediaType:"text/plain",
});
assert.equal(f.kind,"file");
assert.equal(f.name,"run.log");
assert.equal(f.size,Buffer.byteLength("line one\nlíne two"));
const again=await c.putData("main",new Uint8Array(Buffer.from("line one\nlíne two")));
assert.equal(again.object,f.object);
assert.equal(again.media_type,undefined);
const puts=store.requests.filter(r=>r.method==="PUT");
assert.equal(puts.length,3);
assert.equal(puts[0].path,`/objects/${b.object}`);
assert.ok(puts[0].body.equals(raw));
grants([
b,
f,
]);
const items=await c.getData("SB-7");
assert.equal(items.length,2);
assert.ok(items[0] instanceof PrivateData);
assert.equal(items[0].kind,"body");
assert.ok(items[0].data instanceof Uint8Array);
assert.deepEqual(items[0].json(),body);
assert.equal(items[1].name,"run.log");
assert.equal(new TextDecoder().decode(items[1].data),"line one\nlíne two");
assert.equal(items[1].reason,null);
const one=await c.getData("SB-7",{
objects:[
f.object,
],
});
assert.equal(one.length,1);
assert.deepEqual(sb.calls("POST","/api/v1/tickets/SB-7/data-grants")[1].json,{
objects:[
f.object,
],
});
assert.deepEqual(sb.calls("POST","/api/v1/tickets/SB-7/data-grants")[0].json,{});
});
test("the StuckBot token never reaches the data store and grants never reach StuckBot",async()=>{
const f=await c.putData("main","secret log");
grants([
f,
]);
await c.getData("SB-7");
assert.ok(store.requests.length>=2);
for(const r of store.requests){
assert.match(r.token,/^grant_(read|write)$/);
assert.ok(!JSON.stringify(r.headers).includes("sba_test"));
}
assert.ok(sb.requests.length>=2);
for(const r of sb.requests){
assert.equal(r.token,"sba_test");
assert.ok(!JSON.stringify(r.headers).includes("grant_"));
assert.ok(!r.body.toString().includes("grant_"));
assert.ok(!r.body.toString().includes("secret log"));
}
});
test("bytes that do not match the object id throw digest_mismatch",async()=>{
const f=await c.putData("main","the log");
grants([
f,
]);
tamper=true;
await assert.rejects(c.getData("SB-7"),e=>e instanceof StuckBotError&&e.code==="digest_mismatch"&&e.message.includes(f.object));
});
test("an unavailable item comes back without data and without a store request",async()=>{
const object=sha(Buffer.from("gone"));
grants([
{
store:"old",
object,
kind:"file",
name:null,
available:false,
reason:"This data store was removed from the org.",
},
]);
const [item]=await c.getData("SB-7");
assert.equal(item.data,null);
assert.equal(item.json(),null);
assert.equal(item.reason,"This data store was removed from the org.");
assert.equal(item.ref.object,object);
assert.equal(store.requests.length,0);
});
test("data store errors are parsed and redirects are not followed",async()=>{
const object=sha(Buffer.from("missing"));
grants([
{
store:"main",
object,
kind:"file",
},
]);
const e=await c.getData("SB-7").catch(x=>x);
assert.ok(e instanceof StuckBotError);
assert.equal(e.status,404);
assert.equal(e.code,"not_found");
store.answer=()=>({
status:302,
headers:{
"Location":`${sb.url}/steal`,
},
});
const r=await c.getData("SB-7").catch(x=>x);
assert.ok(r instanceof StuckBotError);
assert.equal(r.status,302);
assert.equal(r.code,"data_store_error");
assert.equal(sb.calls("GET","/steal").length,0);
store.answer=()=>err(401,"grant_expired","The grant has expired.");
const x=await c.putData("main","late").catch(y=>y);
assert.equal(x.code,"grant_expired");
assert.equal(x.message,"The grant has expired.");
await store.close();
const u=await c.getData("SB-7").catch(y=>y);
assert.equal(u.code,"data_store_unreachable");
store=await new Mock().start();
});
test("mergeBody: hosted fields win and hosted context keys win",()=>{
const pkg={
title:"Tests fail",
mode:"blocker_only",
objective:"hosted objective",
context:{
workspace:{
repo:"git@example:app.git",
},
errors:[
"hosted",
],
},
data_refs:[
{
store:"main",
object:sha(Buffer.from("x")),
kind:"body",
},
],
};
const body={
objective:"private objective",
stuck_reason:"private reason",
context:{
errors:[
"private",
],
logs:[
"l",
],
},
};
assert.deepEqual(mergeBody(pkg,body),{
objective:"hosted objective",
stuck_reason:"private reason",
title:"Tests fail",
mode:"blocker_only",
data_refs:pkg.data_refs,
context:{
errors:[
"hosted",
],
logs:[
"l",
],
workspace:{
repo:"git@example:app.git",
},
},
});
assert.equal("context" in mergeBody({
title:"t",
},{
objective:"o",
}),false);
assert.deepEqual(mergeBody({
title:"t",
},{
context:{
a:1,
},
}).context,{
a:1,
});
assert.throws(()=>mergeBody(pkg,[
1,
]),TypeError);
assert.throws(()=>mergeBody(pkg,null),TypeError);
});
