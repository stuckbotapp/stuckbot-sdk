import http from "node:http";
export function iso(ms){
return new Date(ms).toISOString();
}
export function err(status,code,message="x",details,headers={}){
const error={
code,
message,
request_id:"req_mock",
};
if(details){
error.details=details;
}
return {
status,
headers,
body:{
error,
},
};
}
export function ok(body,headers={},status=200){
return {
status,
headers,
body,
};
}
export class Mock{
constructor(){
this.routes=new Map();
this.requests=[];
}
async start(){
this.server=http.createServer((req,res)=>this.handle(req,res));
await new Promise(resolve=>this.server.listen(0,"127.0.0.1",resolve));
this.url=`http://127.0.0.1:${this.server.address().port}`;
return this;
}
handle(req,res){
const chunks=[];
req.on("data",c=>chunks.push(c));
req.on("end",()=>{
const u=new URL(req.url,"http://mock");
const auth=req.headers.authorization??"";
const r={
method:req.method,
path:u.pathname,
query:Object.fromEntries(u.searchParams),
headers:req.headers,
body:Buffer.concat(chunks),
at:performance.now(),
token:auth.startsWith("Bearer ")?auth.slice(7):null,
};
r.json=r.body.length&&String(req.headers["content-type"]).includes("json")?JSON.parse(r.body.toString("utf8")):null;
this.requests.push(r);
const a=this.answer(r);
if(a==="drop"){
req.socket.destroy();
return;
}
const headers={
"X-Request-Id":"req_mock",
"Connection":"close",
...a.headers,
};
let raw=Buffer.alloc(0);
if(a.body!==null&&a.body!==undefined){
if(Buffer.isBuffer(a.body)||a.body instanceof Uint8Array){
raw=Buffer.from(a.body);
}else{
raw=Buffer.from(JSON.stringify(a.body));
headers["Content-Type"]??="application/json";
}
}
res.writeHead(a.status,headers);
res.end(a.status===304?undefined:raw);
});
}
on(method,path,...answers){
this.routes.set(`${method} ${path}`,answers);
}
answer(r){
const q=this.routes.get(`${r.method} ${r.path}`);
if(!q){
return err(404,"not_found","No such endpoint.");
}
const a=q.length>1?q.shift():q[0];
return typeof a==="function"?a(r):a;
}
calls(method,path){
return this.requests.filter(r=>r.method===method&&r.path===path);
}
async close(){
this.server.closeAllConnections?.();
await new Promise(resolve=>this.server.close(resolve));
}
}
export function waitFor(fn,timeout=5000,step=20){
return new Promise((resolve,reject)=>{
const end=Date.now()+timeout;
const tick=async()=>{
try{
const v=await fn();
if(v){
resolve(v);
return;
}
}catch(e){
reject(e);
return;
}
if(Date.now()>end){
resolve(false);
return;
}
setTimeout(tick,step);
};
tick();
});
}
