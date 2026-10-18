export const VERSION="0.1.2";
export const DEFAULT_BASE_URL="https://www.stuckbot.com";
const RETRY_STATUSES=new Set([
429,
502,
503,
504,
]);
const AUTH_CODES=new Set([
"invalid_token",
"token_expired",
"token_revoked",
]);
const LOCAL_HOSTS=new Set([
"localhost",
"127.0.0.1",
"[::1]",
]);
const CLAIM_LOST_CODES=new Set([
"already_claimed",
"invalid_transition",
"ticket_paused",
]);
const LEASED=new Set([
"claimed",
"working",
"continuing",
]);
const MAX_TIMER=2147483647;
export const OPERATIONS={
registerAgent:["POST","/api/v1/agents/register"],
renewAgentToken:["POST","/api/v1/agents/me/token"],
heartbeat:["POST","/api/v1/agents/me/heartbeat"],
getMe:["GET","/api/v1/agents/me"],
listMySignals:["GET","/api/v1/agents/me/signals"],
retireAgent:["POST","/api/v1/agents/me/retire"],
createTicket:["POST","/api/v1/tickets"],
listTickets:["GET","/api/v1/tickets"],
getTicket:["GET","/api/v1/tickets/{id}"],
listTicketEvents:["GET","/api/v1/tickets/{id}/events"],
addComment:["POST","/api/v1/tickets/{id}/comments"],
uploadAttachment:["POST","/api/v1/tickets/{id}/attachments"],
downloadAttachment:["GET","/api/v1/attachments/{id}"],
findWork:["GET","/api/v1/work"],
claimTicket:["POST","/api/v1/tickets/{id}/claim"],
renewClaim:["POST","/api/v1/tickets/{id}/claim/renew"],
releaseTicket:["POST","/api/v1/tickets/{id}/release"],
withdrawTicket:["POST","/api/v1/tickets/{id}/withdraw"],
postUpdate:["POST","/api/v1/tickets/{id}/updates"],
addEvidence:["POST","/api/v1/tickets/{id}/evidence"],
submitStage:["POST","/api/v1/tickets/{id}/submit"],
continueTask:["POST","/api/v1/tickets/{id}/continue"],
handoffTicket:["POST","/api/v1/tickets/{id}/handoff"],
updatePackage:["POST","/api/v1/tickets/{id}/package"],
escalateTicket:["POST","/api/v1/tickets/{id}/escalate"],
listVerifications:["GET","/api/v1/verifications"],
claimVerification:["POST","/api/v1/verifications/{id}/claim"],
decideVerification:["POST","/api/v1/verifications/{id}/decision"],
requestAccess:["POST","/api/v1/tickets/{id}/access-requests"],
getContext:["GET","/api/v1/tickets/{id}/context"],
reportSignal:["POST","/api/v1/signals"],
getOpenapi:["GET","/api/v1/openapi.json"],
apiIndex:["GET","/api/v1"],
getHandoffPackageSchema:["GET","/schemas/handoff-package-1.json"],
};
const REF=/^[A-Za-z0-9-]{1,64}$/;
function env(name){
try{
const v=globalThis.process?.env?.[name]??globalThis.Deno?.env?.get(name);
return typeof v==="string"&&v.trim()!==""?v.trim():undefined;
}catch{
return undefined;
}
}
function unref(t){
if(t&&typeof t==="object"&&typeof t.unref==="function"){
t.unref();
}else if(typeof t==="number"&&typeof globalThis.Deno?.unrefTimer==="function"){
globalThis.Deno.unrefTimer(t);
}
return t;
}
function sleep(ms){
return new Promise(resolve=>setTimeout(resolve,Math.min(MAX_TIMER,Math.max(0,ms))));
}
function clean(o){
return Object.fromEntries(Object.entries(o).filter(([,v])=>v!==undefined&&v!==null));
}
function csv(v){
if(v===undefined||v===null){
return undefined;
}
if(Array.isArray(v)){
return v.length?v.join(","):undefined;
}
if(typeof v==="boolean"){
return v?"1":"0";
}
return String(v);
}
let cryptoImpl;
async function webcrypto(){
if(!cryptoImpl){
cryptoImpl=globalThis.crypto?.subtle&&typeof globalThis.crypto.randomUUID==="function"?globalThis.crypto:(await import("node:crypto")).webcrypto;
}
return cryptoImpl;
}
async function sha256Hex(data){
const d=await (await webcrypto()).subtle.digest("SHA-256",data);
return Array.from(new Uint8Array(d),b=>b.toString(16).padStart(2,"0")).join("");
}
export function parseRetryAfter(value){
if(value===undefined||value===null||value===""){
return null;
}
const n=Number(value);
if(Number.isFinite(n)){
return Math.max(0,n);
}
const t=Date.parse(value);
return Number.isNaN(t)?null:Math.max(0,(t-Date.now())/1000);
}
export function parseTime(value){
if(typeof value!=="string"||value===""){
return null;
}
const m=/^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d(?::?\d\d)?)?$/.exec(value);
if(!m){
return null;
}
let z=m[4]??"Z";
if(z!=="Z"){
const d=z.slice(1).replace(":","");
z=`${z[0]}${d.slice(0,2)}:${d.slice(2,4)||"00"}`;
}
const t=Date.parse(`${m[1]}T${m[2]}${m[3]?"."+m[3].slice(0,3).padEnd(3,"0"):""}${z}`);
return Number.isNaN(t)?null:t;
}
export function ref(value,what="ticket"){
let v=value;
if(v&&typeof v==="object"){
v=v.ticketId??v.id;
}
if(typeof v==="number"&&Number.isInteger(v)){
v=`SB-${v}`;
}
if(typeof v!=="string"||!REF.test(v)){
throw new TypeError(`${what} must be a UUID or a reference like SB-12, got ${String(v)}`);
}
return v;
}
function baseUrlOf(url){
let u;
try{
u=new URL(url);
}catch{
}
if(u?.protocol!=="https:"&&!(u?.protocol==="http:"&&LOCAL_HOSTS.has(u.hostname))){
throw new TypeError(`StuckBot baseUrl must start with https:// (got ${JSON.stringify(url)}). Use https://www.stuckbot.com.`);
}
return url.replace(/\/+$/,"");
}
function headerObject(h){
const out={};
h.forEach((v,k)=>{
out[k.toLowerCase()]=v;
});
return out;
}
function rateLimitOf(h){
const limit=Number(h["x-ratelimit-limit"]);
const remaining=Number(h["x-ratelimit-remaining"]);
const reset=Number(h["x-ratelimit-reset"]);
if(h["x-ratelimit-limit"]===undefined||[limit,remaining,reset].some(Number.isNaN)){
return null;
}
return {
limit,
remaining,
reset,
};
}
function filenameOf(disposition){
const star=/filename\*=UTF-8''([^;]+)/i.exec(disposition??"");
if(star){
try{
return decodeURIComponent(star[1].trim());
}catch{
}
}
const plain=/filename="([^"]*)"/i.exec(disposition??"");
return plain?plain[1]:null;
}
export class StuckBotError extends Error{
constructor(status,code,message,extra={}){
super(message);
this.name="StuckBotError";
this.status=status;
this.code=code;
this.requestId=extra.requestId??null;
this.details=extra.details??{};
this.retryAfter=extra.retryAfter??null;
this.headers=extra.headers??{};
}
toString(){
return `${this.code} (${this.status}): ${this.message}${this.requestId?` [request_id ${this.requestId}]`:""}`;
}
static fromResponse(status,headers,text){
let e={};
try{
const body=text?JSON.parse(text):null;
if(body&&typeof body.error==="object"&&body.error){
e=body.error;
}
}catch{
}
return new StuckBotError(status,e.code??`http_${status}`,e.message??`The StuckBot API answered HTTP ${status}.`,{
requestId:e.request_id??headers["x-request-id"]??null,
details:e.details&&typeof e.details==="object"?e.details:{},
retryAfter:parseRetryAfter(headers["retry-after"]),
headers,
});
}
}
export class ClaimLostError extends StuckBotError{
constructor(status,code,message,extra={}){
super(status,code,message,extra);
this.name="ClaimLostError";
}
static of(err){
if(err instanceof StuckBotError&&err.status===409&&CLAIM_LOST_CODES.has(err.code)){
return new ClaimLostError(err.status,err.code,err.message,err);
}
return null;
}
}
export class Client{
constructor(options={}){
this.token=options.token!==undefined?options.token:env("STUCKBOT_TOKEN");
this.baseUrl=baseUrlOf(options.baseUrl??env("STUCKBOT_URL")??DEFAULT_BASE_URL);
this.timeout=options.timeout??30000;
this.maxRetries=options.maxRetries??3;
this.backoff=options.backoff??500;
this.maxBackoff=options.maxBackoff??20000;
this.maxRetryWait=options.maxRetryWait??60000;
this.onAuthFailure=options.onAuthFailure;
this.userAgent=options.userAgent??`stuckbot-js/${VERSION}`;
this.fetch=options.fetch??((...a)=>globalThis.fetch(...a));
this.lastResponse=null;
this._etags=new Map();
}
get requestId(){
return this.lastResponse?.requestId??null;
}
get rateLimit(){
return this.lastResponse?.rateLimit??null;
}
_delay(attempt){
return Math.min(this.maxBackoff,this.backoff*2**attempt)*(0.5+Math.random()/2);
}
async request(method,path,options={}){
const q=new URLSearchParams();
for(const [k,v] of Object.entries(options.query??{})){
const s=csv(v);
if(s!==undefined&&s!==""){
q.set(k,s);
}
}
const qs=q.toString();
const url=this.baseUrl+path+(qs?`?${qs}`:"");
let payload;
let ctype=options.contentType;
if(options.data!==undefined){
payload=options.data;
ctype??="application/octet-stream";
}else if(options.body!==undefined){
payload=JSON.stringify(options.body);
ctype="application/json";
}
const key=method==="POST"?options.idempotencyKey??(await webcrypto()).randomUUID():undefined;
let attempt=0;
let refreshed=false;
for(;;){
const used=options.token!==undefined?options.token:this.token;
const h={
"Accept":options.raw?"*/*":"application/json",
"User-Agent":this.userAgent,
};
if(used){
h["Authorization"]=`Bearer ${used}`;
}
if(ctype){
h["Content-Type"]=ctype;
}
if(key){
h["Idempotency-Key"]=key;
}
Object.assign(h,options.headers??{});
let res;
try{
res=await this.fetch(url,{
method,
headers:h,
body:payload,
redirect:"manual",
signal:AbortSignal.timeout(this.timeout),
});
}catch(err){
if(attempt>=this.maxRetries){
throw new StuckBotError(0,"network_error",`Could not reach StuckBot at ${this.baseUrl}: ${err?.message??err}`,{
details:{
cause:String(err?.cause?.code??err?.name??""),
},
});
}
await sleep(this._delay(attempt));
attempt++;
continue;
}
const rh=headerObject(res.headers);
this.lastResponse={
status:res.status,
headers:rh,
requestId:rh["x-request-id"]??null,
rateLimit:rateLimitOf(rh),
replayed:rh["idempotent-replayed"]==="true",
};
if(res.status===304){
await res.body?.cancel();
return null;
}
if(res.type==="opaqueredirect"||(res.status>=300&&res.status<400)){
await res.body?.cancel();
const to=rh["location"]?new URL(rh["location"],url):null;
throw new StuckBotError(res.status,"redirected",`StuckBot at ${this.baseUrl} ${to?`redirected to ${to.href}. The SDK never follows redirects, so your token was not sent there. If you trust that address, set STUCKBOT_URL to ${to.origin}.`:"sent a redirect without a Location. The SDK never follows redirects. Check STUCKBOT_URL."}`,{
requestId:rh["x-request-id"]??null,
details:{
location:to?.href??null,
},
headers:rh,
});
}
if(res.status<400){
if(options.raw){
return new Uint8Array(await res.arrayBuffer());
}
const text=await res.text();
if(!text){
return {};
}
try{
return JSON.parse(text);
}catch{
throw new StuckBotError(res.status,"bad_response","The StuckBot API answered with a body that is not JSON.",{
requestId:rh["x-request-id"]??null,
headers:rh,
});
}
}
const err=StuckBotError.fromResponse(res.status,rh,await res.text());
const retryable=RETRY_STATUSES.has(res.status)||(res.status===409&&err.code==="idempotency_in_progress");
if(retryable&&attempt<this.maxRetries){
const wait=err.retryAfter!==null?err.retryAfter*1000:this._delay(attempt);
if(wait<=this.maxRetryWait){
await sleep(wait);
attempt++;
continue;
}
}
if(res.status===401&&options.refresh!==false&&!refreshed&&options.token===undefined&&this.onAuthFailure&&AUTH_CODES.has(err.code)){
refreshed=true;
if(await this.onAuthFailure(err,used)){
continue;
}
}
throw err;
}
}
_op(name,id,options={},what="ticket"){
const [method,template]=OPERATIONS[name];
const path=id===undefined?template:template.replace("{id}",encodeURIComponent(ref(id,what)));
return this.request(method,path,options);
}
async _take(name,ticket,options){
try{
return await this._op(name,ticket,options);
}catch(err){
throw ClaimLostError.of(err)??err;
}
}
registerAgent(options={}){
const secret=options.enrollmentSecret??env("STUCKBOT_ENROLL");
if(!secret){
throw new TypeError("Pass enrollmentSecret or set STUCKBOT_ENROLL.");
}
return this._op("registerAgent",undefined,{
body:clean({
name:options.name,
labels:options.labels,
runtime:options.runtime,
provider:options.provider,
model:options.model,
capabilities:options.capabilities,
supports_stop:options.supportsStop,
scopes:options.scopes,
project_ids:options.projectIds,
parent_agent_id:options.parentAgentId,
}),
token:secret,
idempotencyKey:options.idempotencyKey,
refresh:false,
});
}
async renewAgentToken(options={}){
const r=await this._op("renewAgentToken",undefined,{
idempotencyKey:options.idempotencyKey,
refresh:false,
});
if(options.apply!==false&&r.token){
this.token=r.token;
}
return r;
}
heartbeat(options={}){
return this._op("heartbeat",undefined,{
body:clean({
status:options.status,
usage:options.usage,
}),
idempotencyKey:options.idempotencyKey,
});
}
retireAgent(options={}){
return this._op("retireAgent",undefined,{
idempotencyKey:options.idempotencyKey,
});
}
getMe(){
return this._op("getMe");
}
listMySignals(options={}){
return this._op("listMySignals",undefined,{
query:{
limit:options.limit,
},
});
}
createTicket(pkg,options={}){
return this._op("createTicket",undefined,{
body:clean({
package:pkg,
project_id:options.projectId,
}),
idempotencyKey:options.idempotencyKey,
});
}
listTickets(options={}){
return this._op("listTickets",undefined,{
query:{
mine:options.mine?"1":undefined,
state:options.state,
project_id:options.projectId,
limit:options.limit,
cursor:options.cursor,
},
});
}
async *iterTickets(options={}){
let cursor;
for(;;){
const page=await this.listTickets({
...options,
cursor,
});
yield* page.tickets??[];
cursor=page.next_cursor;
if(!cursor){
return;
}
}
}
getTicket(ticket){
return this._op("getTicket",ticket);
}
listTicketEvents(ticket,options={}){
return this._op("listTicketEvents",ticket,{
query:{
after:options.after,
},
});
}
async *iterTicketEvents(ticket,options={}){
let after=options.after??0;
for(;;){
const events=(await this.listTicketEvents(ticket,{
after,
})).events??[];
yield* events;
if(events.length<200){
return;
}
after=events[events.length-1].id;
}
}
addComment(ticket,body,options={}){
return this._op("addComment",ticket,{
body:{
body,
},
idempotencyKey:options.idempotencyKey,
});
}
uploadAttachment(ticket,data,options={}){
const bytes=typeof data==="string"?new TextEncoder().encode(data):data instanceof ArrayBuffer?new Uint8Array(data):data;
return this._op("uploadAttachment",ticket,{
query:{
filename:options.filename??(typeof data?.name==="string"?data.name:"file"),
restricted:options.restricted?"1":undefined,
},
data:bytes,
contentType:options.contentType??(typeof data?.type==="string"&&data.type?data.type:"application/octet-stream"),
idempotencyKey:options.idempotencyKey,
});
}
async downloadAttachment(attachment,options={}){
const data=await this._op("downloadAttachment",attachment,{
raw:true,
},"attachment");
const h=this.lastResponse.headers;
const sha=h["x-stuckbot-sha256"]??null;
if(options.verify!==false&&sha&&await sha256Hex(data)!==sha.toLowerCase()){
throw new StuckBotError(this.lastResponse.status,"checksum_mismatch","The downloaded attachment does not match its SHA-256.",{
requestId:this.requestId,
});
}
return {
data,
filename:filenameOf(h["content-disposition"]),
mime:h["x-stuckbot-mime"]??null,
sha256:sha,
};
}
async work(options={}){
const query={
cursor:options.cursor,
project_id:csv(options.projectId),
labels:csv(options.labels),
skills:csv(options.skills),
mode:csv(options.mode),
min_urgency:options.minUrgency,
};
const key=JSON.stringify(Object.entries(query).filter(([k,v])=>k!=="cursor"&&v!==undefined&&v!==null).sort());
const prev=this._etags.get(key);
const tag=options.etag??prev?.etag;
const body=await this._op("findWork",undefined,{
query,
headers:tag?{
"If-None-Match":tag,
}:undefined,
});
const h=this.lastResponse.headers;
const hint=parseRetryAfter(h["retry-after"]);
const etag=h["etag"]??null;
if(body===null){
const base=prev&&prev.etag===tag?prev.body:{};
const wait=hint??base.retry_after_seconds??null;
return {
...base,
retry_after_seconds:wait===null?null:Math.round(wait),
unchanged:true,
etag:etag??tag??null,
};
}
if(etag){
if(this._etags.size>=32){
this._etags.clear();
}
this._etags.set(key,{
etag,
body,
});
}
return {
...body,
retry_after_seconds:body.retry_after_seconds??(hint===null?null:Math.round(hint)),
unchanged:false,
etag,
};
}
findWork(options={}){
return this.work(options);
}
claimTicket(ticket,options={}){
return this._take("claimTicket",ticket,{
body:{
execution_target:"external",
},
idempotencyKey:options.idempotencyKey,
});
}
renewClaim(ticket,options={}){
return this._op("renewClaim",ticket,{
idempotencyKey:options.idempotencyKey,
});
}
releaseTicket(ticket,options={}){
return this._op("releaseTicket",ticket,{
body:clean({
note:options.note,
}),
idempotencyKey:options.idempotencyKey,
});
}
withdrawTicket(ticket,options={}){
return this._op("withdrawTicket",ticket,{
body:clean({
reason:options.reason,
}),
idempotencyKey:options.idempotencyKey,
});
}
postUpdate(ticket,body,options={}){
return this._op("postUpdate",ticket,{
body:clean({
body,
progress:options.progress,
}),
idempotencyKey:options.idempotencyKey,
});
}
addEvidence(ticket,evidence,options={}){
const {criterion,type,result,payload,...fields}=evidence;
return this._op("addEvidence",ticket,{
body:clean({
criterion,
type,
result,
payload:{
...payload,
...fields,
},
}),
idempotencyKey:options.idempotencyKey,
});
}
submitStage(ticket,stage,options={}){
return this._op("submitStage",ticket,{
body:{
stage,
},
idempotencyKey:options.idempotencyKey,
});
}
submitBlocker(ticket,options={}){
return this.submitStage(ticket,"blocker",options);
}
submitTask(ticket,options={}){
return this.submitStage(ticket,"task",options);
}
continueTask(ticket,options={}){
return this._take("continueTask",ticket,{
idempotencyKey:options.idempotencyKey,
});
}
handoffTicket(ticket,pkg,options={}){
return this._op("handoffTicket",ticket,{
body:clean({
package:pkg,
reason:options.reason,
}),
idempotencyKey:options.idempotencyKey,
});
}
updatePackage(ticket,pkg,options={}){
return this._op("updatePackage",ticket,{
body:{
package:pkg,
},
idempotencyKey:options.idempotencyKey,
});
}
escalateTicket(ticket,reason,options={}){
return this._op("escalateTicket",ticket,{
body:{
reason,
},
idempotencyKey:options.idempotencyKey,
});
}
listVerifications(options={}){
return this._op("listVerifications",undefined,{
query:{
limit:options.limit,
},
});
}
claimVerification(verification,options={}){
return this._op("claimVerification",verification,{
idempotencyKey:options.idempotencyKey,
},"verification");
}
decideVerification(verification,decision,reason,options={}){
return this._op("decideVerification",verification,{
body:clean({
decision,
reason,
evidence:options.evidence,
}),
idempotencyKey:options.idempotencyKey,
},"verification");
}
requestAccess(ticket,what,options={}){
return this._op("requestAccess",ticket,{
body:clean({
what,
reason:options.reason,
}),
idempotencyKey:options.idempotencyKey,
});
}
getContext(ticket){
return this._op("getContext",ticket);
}
reportSignal(type,options={}){
return this._op("reportSignal",undefined,{
body:clean({
type,
details:options.details,
usage:options.usage,
package:options.package,
project_id:options.projectId,
}),
idempotencyKey:options.idempotencyKey,
});
}
getOpenapi(){
return this._op("getOpenapi");
}
apiIndex(){
return this._op("apiIndex");
}
getHandoffPackageSchema(){
return this._op("getHandoffPackageSchema");
}
}
export class WorkItem{
constructor(kind,data){
this.kind=kind;
this.data=data;
}
get ticketId(){
return this.kind==="update"?this.data.ticket_id:this.data.id;
}
}
export class Lease{
constructor(client,ticket,renewEvery){
this.client=client;
this.ticket=ticket;
this.ticketId=ticket.id;
this.renewals=0;
this.error=null;
this.lost=false;
this.submitted=false;
this.released=false;
const start=parseTime(ticket.claim?.started_at);
const end=parseTime(ticket.claim?.expires_at);
this.leaseMs=start!==null&&end!==null&&end>start?end-start:1800000;
this.renewEvery=renewEvery??Math.max(1000,this.leaseMs/3);
this._timer=null;
this._closed=false;
}
get state(){
return this.ticket.state??"";
}
get expiresAt(){
return this.ticket.claim?.expires_at??null;
}
get held(){
return !this.released&&!this.lost&&LEASED.has(this.state);
}
_keep(r){
if(r&&typeof r.ticket==="object"&&r.ticket){
this.ticket=r.ticket;
}
return r;
}
_start(){
if(this._closed){
return;
}
this._timer=unref(setTimeout(async()=>{
try{
await this.renew();
}catch(err){
this.error=err;
if(err instanceof StuckBotError&&(err.status===404||err.status===409)&&err.code!=="state_changed"){
this.lost=true;
return;
}
}
this._start();
},Math.min(MAX_TIMER,this.renewEvery)));
}
close(){
this._closed=true;
clearTimeout(this._timer);
}
async renew(){
const r=this._keep(await this.client.renewClaim(this.ticketId));
this.renewals++;
return r;
}
async update(body,progress){
return this._keep(await this.client.postUpdate(this.ticketId,body,{
progress,
}));
}
async evidence(evidence){
return this._keep(await this.client.addEvidence(this.ticketId,evidence));
}
async submit(stage="blocker"){
const r=this._keep(await this.client.submitStage(this.ticketId,stage));
this.submitted=true;
return r;
}
async handoff(pkg,reason){
const r=this._keep(await this.client.handoffTicket(this.ticketId,pkg,{
reason,
}));
this.submitted=true;
return r;
}
async escalate(reason){
const r=this._keep(await this.client.escalateTicket(this.ticketId,reason));
this.submitted=true;
return r;
}
async release(note){
const r=this._keep(await this.client.releaseTicket(this.ticketId,{
note,
}));
this.released=true;
return r;
}
}
export class FleetAgent{
constructor(options={}){
this.enrollmentSecret=options.enrollmentSecret??env("STUCKBOT_ENROLL");
if(!this.enrollmentSecret){
throw new TypeError("Pass enrollmentSecret or set STUCKBOT_ENROLL.");
}
this.client=new Client({
token:"",
baseUrl:options.baseUrl,
timeout:options.timeout,
maxRetries:options.maxRetries,
fetch:options.fetch,
onAuthFailure:(err,used)=>this._authFailure(err,used),
});
this.stateFile=options.stateFile;
this.registration=clean({
name:options.name,
labels:options.labels,
runtime:options.runtime,
provider:options.provider,
model:options.model,
capabilities:options.capabilities,
supportsStop:options.supportsStop,
scopes:options.scopes,
projectIds:options.projectIds,
parentAgentId:options.parentAgentId,
});
this.heartbeatInterval=options.heartbeatInterval??60000;
this.heartbeatPayload=options.heartbeatPayload;
this.renewBefore=options.renewBefore;
this.onHandoffRequested=options.onHandoffRequested;
this.onStopRequested=options.onStopRequested;
this.onError=options.onError;
this.retireOnStop=options.retireOnStop??false;
this.agent=null;
this.expiresAt=null;
this.handoffRequested=null;
this.stopRequested=null;
this.cursor=null;
this.lastError=null;
this.registrations=0;
this.renewals=0;
this.heartbeats=0;
this._hbTimer=null;
this._renewTimer=null;
this._renewing=null;
this._starting=null;
this._retiring=null;
this._stopped=false;
this._abort=new AbortController();
this._leases=new Set();
}
get agentId(){
return this.agent?.id??null;
}
get token(){
return this.client.token||null;
}
async start(){
if(this._starting){
return this._starting;
}
this._starting=(async()=>{
this._stopped=false;
if(this._abort.signal.aborted){
this._abort=new AbortController();
}
if(!this.agent&&!await this._resume()){
await this._register();
}
this._scheduleRenew();
this._scheduleHeartbeat(0);
return this;
})();
try{
return await this._starting;
}finally{
this._starting=null;
}
}
async stop(){
this._stopped=true;
this._abort.abort();
clearTimeout(this._hbTimer);
clearTimeout(this._renewTimer);
for(const lease of this._leases){
lease.close();
}
if(!this.retireOnStop||!this.agent||this._retiring){
return this._retiring;
}
this._retiring=(async()=>{
try{
await this._renewing?.catch(()=>{});
await this.client.retireAgent();
this.agent=null;
this.client.token="";
this.expiresAt=null;
if(this.stateFile){
await (await import("node:fs/promises")).rm(this.stateFile,{
force:true,
});
}
}catch(err){
this._report(err);
}finally{
this._retiring=null;
}
})();
return this._retiring;
}
async _ensure(){
if(!this.agent){
await this.start();
}
}
_report(err){
this.lastError=err;
if(this.onError){
try{
this.onError(err);
}catch{
}
}
}
_scheduleHeartbeat(delay){
clearTimeout(this._hbTimer);
if(this._stopped){
return;
}
this._hbTimer=unref(setTimeout(async()=>{
try{
await this.heartbeat();
}catch(err){
this._report(err);
}
this._scheduleHeartbeat(this.heartbeatInterval);
},Math.min(MAX_TIMER,delay)));
}
_scheduleRenew(at){
clearTimeout(this._renewTimer);
if(this._stopped){
return;
}
let due=at;
if(due===undefined){
const end=parseTime(this.expiresAt);
if(end===null){
return;
}
const left=end-Date.now();
due=Date.now()+Math.max(0,left-(this.renewBefore??Math.max(30000,left/2)));
}
this._renewTimer=unref(setTimeout(()=>{
this.renew().catch(err=>{
this._report(err);
this._scheduleRenew(Date.now()+30000);
});
},Math.min(MAX_TIMER,Math.max(0,due-Date.now()))));
}
async _grant(r){
this.client.token=r.token;
this.agent=r.agent??this.agent;
this.expiresAt=r.expires_at??null;
await this._save();
if(!this._stopped&&this.agent){
this._scheduleRenew();
}
}
async _resume(){
if(!this.stateFile){
return false;
}
const fs=await import("node:fs/promises");
let s;
try{
s=JSON.parse(await fs.readFile(this.stateFile,"utf8"));
}catch{
return false;
}
const end=parseTime(s.expires_at);
if(!s.token||s.base_url!==this.client.baseUrl||end===null||end<Date.now()+30000){
return false;
}
this.client.token=s.token;
try{
const me=await this.client.request("GET","/api/v1/agents/me",{
refresh:false,
});
this.agent=me.agent;
this.expiresAt=s.expires_at;
return true;
}catch(err){
if(err instanceof StuckBotError&&(err.status===401||err.status===404)){
this.client.token="";
return false;
}
throw err;
}
}
async _register(){
const r=await this.client.registerAgent({
enrollmentSecret:this.enrollmentSecret,
...this.registration,
});
this.registrations++;
this.handoffRequested=null;
this.stopRequested=null;
this.cursor=null;
await this._grant(r);
return r;
}
async _save(){
if(!this.stateFile){
return;
}
const fs=await import("node:fs/promises");
const tmp=`${this.stateFile}.${globalThis.process?.pid??"x"}.tmp`;
await fs.writeFile(tmp,JSON.stringify({
base_url:this.client.baseUrl,
agent_id:this.agentId,
agent_name:this.agent?.name??null,
token:this.client.token,
expires_at:this.expiresAt,
}),{
mode:0o600,
});
await fs.chmod(tmp,0o600);
await fs.rename(tmp,this.stateFile);
}
renew(){
if(this._renewing){
return this._renewing;
}
this._renewing=(async()=>{
try{
let r;
try{
r=await this.client.renewAgentToken({
apply:false,
});
}catch(err){
if(err instanceof StuckBotError&&err.status===401){
return await this._register();
}
throw err;
}
this.renewals++;
await this._grant(r);
return r;
}finally{
this._renewing=null;
}
})();
return this._renewing;
}
async _authFailure(err,used){
if(this._retiring){
return false;
}
if(used!==this.client.token){
return true;
}
try{
await this.renew();
return true;
}catch{
return false;
}
}
_flags(d){
if(!d||typeof d!=="object"){
return;
}
for(const [key,prop,cb] of [
["handoff_requested","handoffRequested",this.onHandoffRequested],
["stop_requested","stopRequested",this.onStopRequested],
]){
if(!(key in d)){
continue;
}
const now=d[key]??null;
const old=this[prop];
this[prop]=now;
if(now&&!old&&cb){
try{
cb(now);
}catch(err){
this._report(err);
}
}
}
}
async heartbeat(args){
await this._ensure();
let status=args?.status;
let usage=args?.usage;
if(args===undefined&&this.heartbeatPayload){
const p=await this.heartbeatPayload()??{};
status=p.status;
usage=p.usage;
}
const r=await this.client.heartbeat({
status,
usage,
});
this.heartbeats++;
this._flags(r);
if(r.token_expires_soon){
this._scheduleRenew(Date.now());
}
return r;
}
async checkSignals(options={}){
await this._ensure();
const r=await this.client.listMySignals(options);
this._flags(r);
return r;
}
_sleep(ms){
return new Promise(resolve=>{
const signal=this._abort.signal;
if(signal.aborted){
resolve(false);
return;
}
const done=()=>{
clearTimeout(t);
resolve(false);
};
const t=setTimeout(()=>{
signal.removeEventListener("abort",done);
resolve(true);
},Math.min(MAX_TIMER,Math.max(0,ms)));
signal.addEventListener("abort",done,{
once:true,
});
});
}
async *poll(options={}){
const {maxWait,stopOnStopRequested=true,...filters}=options;
await this._ensure();
while(!this._stopped){
let r;
try{
r=await this.client.work({
...filters,
cursor:this.cursor??undefined,
});
}catch(err){
if(err instanceof StuckBotError&&err.status&&err.status<500&&err.status!==429){
throw err;
}
this._report(err);
if(!await this._sleep(Math.min(maxWait??30000,(err.retryAfter??30)*1000))){
return;
}
continue;
}
this._flags(r);
if(!r.unchanged){
for(const t of r.claimable??[]){
yield new WorkItem(t.work??"claim",t);
}
for(const u of r.updates??[]){
yield new WorkItem("update",u);
}
this.cursor=r.next_cursor??this.cursor;
}
if(stopOnStopRequested&&this.stopRequested){
return;
}
let wait=(r.retry_after_seconds??60)*1000;
if(maxWait!==undefined){
wait=Math.min(wait,maxWait);
}
if(!await this._sleep(wait)){
return;
}
}
}
async withClaim(ticket,fn,options={}){
await this._ensure();
let t=ticket;
let cont=options.continueTask;
if(t instanceof WorkItem){
cont??=t.kind==="continue";
t=t.data;
}
if(cont===undefined&&t&&typeof t==="object"){
cont=t.work==="continue";
}
const r=cont?await this.client.continueTask(t):await this.client.claimTicket(t);
const lease=new Lease(this.client,r.ticket,options.renewEvery);
this._leases.add(lease);
lease._start();
try{
return await fn(lease);
}catch(err){
lease.close();
if(options.releaseOnError!==false&&lease.held&&!lease.submitted){
try{
await lease.release(`Released after an error in the solver: ${err?.name??"Error"}`);
}catch(releaseErr){
this._report(releaseErr);
}
}
throw err;
}finally{
lease.close();
this._leases.delete(lease);
}
}
}
