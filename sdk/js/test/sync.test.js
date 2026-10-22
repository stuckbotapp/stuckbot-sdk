import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {Client,OPERATIONS} from "../index.js";
const SPEC=process.env.STUCKBOT_OPENAPI??new URL("../../../docs/openapi.json",import.meta.url);
const T="0b4f6c1e-1111-4222-8333-944445555666";
const CALLS={
registerAgent:c=>c.registerAgent({
enrollmentSecret:"sbe_x",
}),
renewAgentToken:c=>c.renewAgentToken({
apply:false,
}),
heartbeat:c=>c.heartbeat({
status:"ok",
}),
getMe:c=>c.getMe(),
listMySignals:c=>c.listMySignals({
limit:5,
}),
retireAgent:c=>c.retireAgent(),
createTicket:c=>c.createTicket({
title:"t",
}),
listTickets:c=>c.listTickets({
mine:true,
}),
getTicket:c=>c.getTicket("SB-1"),
listTicketEvents:c=>c.listTicketEvents(T,{
after:3,
}),
addComment:c=>c.addComment(T,"hi"),
uploadAttachment:c=>c.uploadAttachment(T,"data",{
filename:"a.txt",
}),
downloadAttachment:c=>c.downloadAttachment(T),
findWork:c=>c.findWork(),
claimTicket:c=>c.claimTicket(T),
renewClaim:c=>c.renewClaim(T),
releaseTicket:c=>c.releaseTicket(T,{
note:"bye",
}),
withdrawTicket:c=>c.withdrawTicket(T,{
reason:"duplicate",
}),
postUpdate:c=>c.postUpdate(T,"progress"),
addEvidence:c=>c.addEvidence(T,{
criterion:"b1",
type:"command_run",
command:"true",
exit_code:0,
}),
submitStage:c=>c.submitStage(T,"blocker"),
continueTask:c=>c.continueTask(T),
handoffTicket:c=>c.handoffTicket(T,{
title:"t",
}),
updatePackage:c=>c.updatePackage(T,{
title:"t",
}),
escalateTicket:c=>c.escalateTicket(T,"help"),
listVerifications:c=>c.listVerifications(),
claimVerification:c=>c.claimVerification(T),
decideVerification:c=>c.decideVerification(T,"approve","checked"),
requestAccess:c=>c.requestAccess(T,"the log"),
getContext:c=>c.getContext(T),
getDataGrants:c=>c.getDataGrants(T,{
objects:[
`sha256:${"a".repeat(64)}`,
],
}),
listDataStores:c=>c.listDataStores(),
getUploadGrant:c=>c.getUploadGrant("main"),
reportSignal:c=>c.reportSignal("watchdog",{
details:{
reason:"loop",
},
}),
getOpenapi:c=>c.getOpenapi(),
apiIndex:c=>c.apiIndex(),
getHandoffPackageSchema:c=>c.getHandoffPackageSchema(),
getGrantKeys:c=>c.getGrantKeys(),
};
function camel(s){
return s.replace(/_([a-z])/g,(_,x)=>x.toUpperCase());
}
function matches(template,path){
return new RegExp(`^${template.replace(/[.*+?^${}()|[\]\\]/g,"\\$&").replace(/\\\{\w+\\\}/g,"[^/]+")}$`).test(path);
}
test("every spec operation has an SDK method and every SDK call is in the spec",async()=>{
const spec=JSON.parse(readFileSync(SPEC,"utf8"));
const ops=[];
for(const [path,item] of Object.entries(spec.paths)){
for(const [method,op] of Object.entries(item)){
if([
"get",
"post",
"put",
"patch",
"delete",
].includes(method)){
ops.push({
method:method.toUpperCase(),
path,
id:op.operationId,
});
}
}
}
assert.ok(ops.length>=29);
const table=Object.values(OPERATIONS).map(([m,p])=>`${m} ${p}`);
assert.deepEqual(ops.filter(o=>!table.includes(`${o.method} ${o.path}`)).map(o=>`${o.method} ${o.path} (${o.id})`),[],"operations in docs/openapi.json without an SDK method");
const specKeys=ops.map(o=>`${o.method} ${o.path}`);
assert.deepEqual(table.filter(k=>!specKeys.includes(k)),[],"SDK operations that are not in docs/openapi.json");
for(const o of ops){
assert.deepEqual(OPERATIONS[camel(o.id)],[o.method,o.path],`operationId ${o.id} should be the SDK method ${camel(o.id)}`);
}
assert.deepEqual(Object.keys(CALLS).sort(),Object.keys(OPERATIONS).sort(),"every SDK operation needs a call in this test");
const sent=[];
const c=new Client({
token:"sba_x",
baseUrl:"https://sdk.invalid",
maxRetries:0,
fetch:async(url,init)=>{
sent.push([init.method,new URL(url).pathname]);
return new Response("{}",{
status:200,
headers:{
"Content-Type":"application/json",
},
});
},
});
for(const [name,call] of Object.entries(CALLS)){
assert.equal(typeof c[name],"function",`Client.${name} is missing`);
const before=sent.length;
await call(c);
const mine=sent.slice(before);
assert.equal(mine.length,1,name);
const [method,path]=mine[0];
assert.equal(method,OPERATIONS[name][0],name);
assert.ok(matches(OPERATIONS[name][1],path),`${name} sent ${path}, expected ${OPERATIONS[name][1]}`);
assert.ok(ops.some(o=>o.method===method&&matches(o.path,path)),`${name} called ${method} ${path}, which is not in the spec`);
}
});
