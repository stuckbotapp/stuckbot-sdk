"""stuckbot-datanode: keep ticket content on your own servers.

A small file server you run inside your network. Agents store ticket details, logs and files here,
and tickets on StuckBot hold only a reference (store name plus SHA-256). To read or write, a client
shows a grant: a token StuckBot signs with ES256 that names this store, your org, the action, the
objects and an expiry of a few minutes. The node checks the signature against StuckBot's public
keys, so it needs no secret from StuckBot and StuckBot never connects to it.

What it checks on every request, in order: the signature and key id, the issuer, that the grant is
for this store and this org, the time window, the action, and for reads that the object is listed
in the grant. Objects are stored by their SHA-256 and a write is kept only if the bytes match it.
Every request is appended to the audit log with who the grant was issued to.

Keep the node off the public internet where you can: a grant is a bearer token, and StuckBot (or
anyone holding its signing key) can issue one, so network position is what keeps the operator out.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import http.server
import io
import json
import os
import re
import socketserver
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Any,Dict,Optional,Tuple
from .attest import VerifyingKey,unb64url
OBJECT=re.compile(r"^sha256:([0-9a-f]{64})$")
PATH=re.compile(r"^/objects/(sha256:[0-9a-f]{64})$")
SKEW=60
MAX_LIFETIME=900
class GrantError(Exception):
    def __init__(self,status:int,code:str,message:str):
        super().__init__(message)
        self.status=status
        self.code=code
def keys_from_jwks(data:Dict[str,Any])->Dict[str,VerifyingKey]:
    out={}
    for k in data.get("keys",[]):
        if k.get("kty")!="EC" or k.get("crv")!="P-256" or not isinstance(k.get("kid"),str):
            continue
        x=unb64url(k["x"])
        y=unb64url(k["y"])
        if len(x)!=32 or len(y)!=32:
            continue
        out[k["kid"]]=VerifyingKey((int.from_bytes(x,"big"),int.from_bytes(y,"big")))
    return out
def fetch_jwks(issuer:str,timeout:float=10)->Dict[str,Any]:
    u=urllib.parse.urlsplit(issuer)
    if u.scheme!="https" and u.hostname not in ("127.0.0.1","localhost","::1"):
        raise ValueError("issuer must be https")
    with urllib.request.urlopen(issuer.rstrip("/")+"/.well-known/stuckbot-grant-keys.json",timeout=timeout) as r:
        return json.loads(r.read(1<<20))
class Config:
    def __init__(self,data:Dict[str,Any],base:str="."):
        path=lambda v:os.path.abspath(os.path.join(base,os.path.expanduser(str(v))))
        self.store_id=str(data.get("store_id",""))
        self.org_id=str(data.get("org_id",""))
        if not re.match(r"^[0-9a-f-]{36}$",self.store_id) or not re.match(r"^[0-9a-f-]{36}$",self.org_id):
            raise ValueError("store_id and org_id must be the ids shown under Policy, Private data")
        self.issuer=str(data.get("issuer","https://www.stuckbot.com")).rstrip("/")
        self.listen=str(data.get("listen","127.0.0.1:8750"))
        self.storage=path(data.get("storage","/var/lib/stuckbot-data"))
        self.keys_file=path(data["keys_file"]) if data.get("keys_file") else None
        self.tls_cert=path(data["tls_cert"]) if data.get("tls_cert") else None
        self.tls_key=path(data["tls_key"]) if data.get("tls_key") else None
        self.max_object_bytes=int(float(data.get("max_object_mb",100))*1024*1024)
        self.audit_log=path(data.get("audit_log",os.path.join(self.storage,"audit.jsonl")))
        self.allow_query_token=bool(data.get("allow_query_token",True))
        self.trusted_proxies=[str(x) for x in data.get("trusted_proxies",[])]
    @classmethod
    def load(cls,path:str)->"Config":
        with open(path) as f:
            return cls(json.load(f),os.path.dirname(os.path.abspath(path)))
EXAMPLE={
"store_id":"<store id from Policy, Private data>",
"org_id":"<org id from the same place>",
"issuer":"https://www.stuckbot.com",
"listen":"127.0.0.1:8750",
"storage":"/var/lib/stuckbot-data",
"keys_file":"/etc/stuckbot-datanode/grant-keys.json",
"tls_cert":None,
"tls_key":None,
"max_object_mb":100,
"allow_query_token":True,
}
class Grants:
    """Checks StuckBot data grants for one store."""
    def __init__(self,cfg:Config,keys:Optional[Dict[str,VerifyingKey]]=None,now=time.time):
        self.cfg=cfg
        self.now=now
        self.keys=keys if keys is not None else self._load()
        self._lock=threading.Lock()
        self._seen:Dict[str,Tuple[float,Dict[str,Any]]]={}
        self._refetched=0.0
    def _load(self)->Dict[str,VerifyingKey]:
        if self.cfg.keys_file:
            if not os.path.isfile(self.cfg.keys_file):
                raise ValueError("keys_file %s does not exist yet. Run stuckbot-datanode trust --config <file> first."%self.cfg.keys_file)
            with open(self.cfg.keys_file) as f:
                return keys_from_jwks(json.load(f))
        return keys_from_jwks(fetch_jwks(self.cfg.issuer))
    def _key(self,kid:str)->Optional[VerifyingKey]:
        k=self.keys.get(kid)
        if k is None and not self.cfg.keys_file and self.now()-self._refetched>60:
            self._refetched=self.now()
            try:
                self.keys=keys_from_jwks(fetch_jwks(self.cfg.issuer))
            except Exception:
                return None
            k=self.keys.get(kid)
        return k
    def check(self,token:str,act:str,obj:Optional[str]=None)->Dict[str,Any]:
        if not token or len(token)>8192:
            raise GrantError(401,"missing_grant","Send a StuckBot data grant as Authorization: Bearer <token>.")
        digest=hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            hit=self._seen.get(digest)
        if hit and hit[0]>self.now():
            claims=hit[1]
        else:
            claims=self._verify(token)
            with self._lock:
                if len(self._seen)>2000:
                    self._seen={k:v for k,v in self._seen.items() if v[0]>self.now()}
                self._seen[digest]=(float(claims["exp"]),claims)
        now=self.now()
        if claims["exp"]<=now-SKEW:
            raise GrantError(401,"grant_expired","This grant expired. Ask StuckBot for a new one.")
        if claims.get("act")!=act:
            raise GrantError(403,"wrong_action",f"This grant allows {claims.get('act')}, not {act}.")
        if obj is not None and act=="read" and obj not in (claims.get("obj") or []):
            raise GrantError(403,"wrong_object","This grant does not cover that object.")
        return claims
    def _verify(self,token:str)->Dict[str,Any]:
        parts=token.split(".")
        if len(parts)!=3:
            raise GrantError(401,"bad_grant","The grant is not a signed token.")
        try:
            head=json.loads(unb64url(parts[0]))
            claims=json.loads(unb64url(parts[1]))
            sig=unb64url(parts[2])
        except Exception:
            raise GrantError(401,"bad_grant","The grant is not a signed token.")
        if not isinstance(head,dict) or head.get("alg")!="ES256" or not isinstance(head.get("kid"),str):
            raise GrantError(401,"bad_grant","The grant must be signed with ES256.")
        key=self._key(head["kid"])
        if key is None:
            raise GrantError(401,"unknown_key","The grant was signed with a key this node does not trust. Run stuckbot-datanode trust if StuckBot rotated its key.")
        if not key.verify((parts[0]+"."+parts[1]).encode(),sig):
            raise GrantError(401,"bad_signature","The grant signature is not valid.")
        if not isinstance(claims,dict):
            raise GrantError(401,"bad_grant","The grant has no claims.")
        now=self.now()
        for k in ("iat","nbf","exp"):
            if not isinstance(claims.get(k),(int,float)) or isinstance(claims.get(k),bool):
                raise GrantError(401,"bad_grant",f"The grant has no {k}.")
        if claims.get("iss")!=self.cfg.issuer:
            raise GrantError(403,"wrong_issuer","The grant was issued by someone else.")
        if claims.get("aud")!="stuckbot-data:"+self.cfg.store_id or claims.get("store")!=self.cfg.store_id:
            raise GrantError(403,"wrong_store","The grant is for another data store.")
        if claims.get("org")!=self.cfg.org_id:
            raise GrantError(403,"wrong_org","The grant is for another org.")
        if claims["nbf"]>now+SKEW or claims["iat"]>now+SKEW:
            raise GrantError(401,"grant_not_yet_valid","The grant is not valid yet. Check this machine's clock.")
        if claims["exp"]-claims["iat"]>MAX_LIFETIME:
            raise GrantError(401,"bad_grant","The grant lives longer than this node accepts.")
        if claims["exp"]<=now-SKEW:
            raise GrantError(401,"grant_expired","This grant expired. Ask StuckBot for a new one.")
        return claims
class Store:
    def __init__(self,root:str):
        self.root=root
        os.makedirs(os.path.join(root,"objects"),mode=0o700,exist_ok=True)
        os.makedirs(os.path.join(root,"tmp"),mode=0o700,exist_ok=True)
    def path(self,obj:str)->str:
        m=OBJECT.match(obj)
        if not m:
            raise ValueError("bad object id")
        h=m.group(1)
        return os.path.join(self.root,"objects",h[:2],h)
    def has(self,obj:str)->bool:
        return os.path.isfile(self.path(obj))
    def put_stream(self,obj:str,stream,length:int)->bool:
        dest=self.path(obj)
        if os.path.isfile(dest):
            left=length
            while left>0:
                chunk=stream.read(min(65536,left))
                if not chunk:
                    break
                left-=len(chunk)
            return False
        fd,tmp=tempfile.mkstemp(dir=os.path.join(self.root,"tmp"))
        h=hashlib.sha256()
        got=0
        try:
            with os.fdopen(fd,"wb") as f:
                while got<length:
                    chunk=stream.read(min(65536,length-got))
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
                    got+=len(chunk)
                f.flush()
                os.fsync(f.fileno())
            if got!=length:
                raise ValueError("the upload ended early")
            if not hmac.compare_digest("sha256:"+h.hexdigest(),obj):
                raise ValueError("the bytes do not match the object id")
            os.makedirs(os.path.dirname(dest),mode=0o700,exist_ok=True)
            os.replace(tmp,dest)
            tmp=None
            return True
        finally:
            if tmp:
                os.unlink(tmp)
    def put_bytes(self,data:bytes)->str:
        obj="sha256:"+hashlib.sha256(data).hexdigest()
        self.put_stream(obj,io.BytesIO(data),len(data))
        return obj
def _filename(name:str)->str:
    safe=re.sub(r"[^A-Za-z0-9._-]+","_",name)[:150].strip("._") or "download"
    return 'attachment; filename="%s"; filename*=UTF-8\'\'%s'%(safe,urllib.parse.quote(name[:200],safe=""))
class Handler(http.server.BaseHTTPRequestHandler):
    server_version="stuckbot-datanode"
    sys_version=""
    protocol_version="HTTP/1.1"
    node:"Node"
    def log_message(self,fmt,*args):
        pass
    def _ip(self)->str:
        ip=self.client_address[0] if self.client_address else ""
        fwd=self.headers.get("X-Forwarded-For")
        if fwd and ip in self.node.cfg.trusted_proxies:
            return fwd.split(",")[-1].strip()
        return ip
    def _send(self,status:int,body:Dict[str,Any],extra:Optional[Dict[str,str]]=None):
        data=(json.dumps(body)+"\n").encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        for k,v in (extra or {}).items():
            self.send_header(k,v)
        self.end_headers()
        if self.command!="HEAD":
            self.wfile.write(data)
    def _fail(self,e:GrantError,obj:Optional[str]):
        self.node.audit(self,obj,e.status,None,reason=e.code)
        self._send(e.status,{"error":{"code":e.code,"message":str(e)}})
    def _token(self,query:Dict[str,Any])->str:
        auth=self.headers.get("Authorization","")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if self.command in ("GET","HEAD") and self.node.cfg.allow_query_token:
            return (query.get("grant") or [""])[0]
        return ""
    def _route(self)->Tuple[Optional[str],Dict[str,Any]]:
        u=urllib.parse.urlsplit(self.path)
        if u.path=="/healthz":
            return "healthz",{}
        m=PATH.match(u.path)
        return (m.group(1) if m else None),urllib.parse.parse_qs(u.query)
    def do_HEAD(self):
        self.do_GET()
    def do_GET(self):
        obj,query=self._route()
        if obj=="healthz":
            return self._send(200,{"ok":True})
        if obj is None:
            return self._send(404,{"error":{"code":"not_found","message":"Use /objects/sha256:<hex>."}})
        try:
            claims=self.node.grants.check(self._token(query),"read",obj)
        except GrantError as e:
            return self._fail(e,obj)
        path=self.node.store.path(obj)
        if not os.path.isfile(path):
            self.node.audit(self,obj,404,claims)
            return self._send(404,{"error":{"code":"not_found","message":"This node does not hold that object."}})
        size=os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type","application/octet-stream")
        self.send_header("Content-Length",str(size))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Content-Security-Policy","sandbox; default-src 'none'")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Digest","sha-256="+base64.b64encode(bytes.fromhex(obj[7:])).decode())
        if "grant" in query:
            self.send_header("Content-Disposition",_filename((query.get("name") or [obj[7:19]])[0]))
        self.end_headers()
        sent=0
        if self.command=="GET":
            with open(path,"rb") as f:
                while True:
                    chunk=f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent+=len(chunk)
        self.node.audit(self,obj,200,claims,sent)
    def do_PUT(self):
        obj,query=self._route()
        if obj is None or obj=="healthz":
            return self._send(404,{"error":{"code":"not_found","message":"PUT to /objects/sha256:<hex of the bytes>."}})
        try:
            claims=self.node.grants.check(self._token({}),"write")
        except GrantError as e:
            self.close_connection=True
            return self._fail(e,obj)
        length=self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self.close_connection=True
            return self._send(411,{"error":{"code":"length_required","message":"Send Content-Length."}})
        n=int(length)
        limit=min(self.node.cfg.max_object_bytes,int(claims.get("max_bytes") or self.node.cfg.max_object_bytes))
        if n>limit:
            self.close_connection=True
            self.node.audit(self,obj,413,claims,reason="too_large")
            return self._send(413,{"error":{"code":"too_large","message":f"Objects can be at most {limit} bytes here."}})
        try:
            created=self.node.store.put_stream(obj,self.rfile,n)
        except ValueError as e:
            self.close_connection=True
            self.node.audit(self,obj,400,claims,reason="digest_mismatch")
            return self._send(400,{"error":{"code":"digest_mismatch","message":str(e)}})
        self.node.audit(self,obj,201 if created else 200,claims,n)
        self._send(201 if created else 200,{"object":obj,"size":n,"created":created})
    def do_POST(self):
        self._send(405,{"error":{"code":"method_not_allowed","message":"Use GET or PUT on /objects/sha256:<hex>."}},{"Allow":"GET, HEAD, PUT"})
    do_DELETE=do_POST
    do_PATCH=do_POST
class Server(socketserver.ThreadingMixIn,http.server.HTTPServer):
    daemon_threads=True
    allow_reuse_address=True
class Node:
    def __init__(self,cfg:Config,grants:Optional[Grants]=None):
        self.cfg=cfg
        self.grants=grants or Grants(cfg)
        self.store=Store(cfg.storage)
        self._audit_lock=threading.Lock()
        os.makedirs(os.path.dirname(cfg.audit_log),mode=0o700,exist_ok=True)
    def audit(self,h:Handler,obj:Optional[str],status:int,claims:Optional[Dict[str,Any]],size:int=0,reason:Optional[str]=None):
        line={"at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"ip":h._ip(),"method":h.command,"object":obj,"status":status,"bytes":size}
        if claims:
            line.update({"sub":claims.get("sub"),"ticket":claims.get("ref"),"jti":claims.get("jti")})
        if reason:
            line["reason"]=reason
        with self._audit_lock:
            fd=os.open(self.cfg.audit_log,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
            with os.fdopen(fd,"a") as f:
                f.write(json.dumps(line)+"\n")
    def server(self)->Server:
        host,_,port=self.cfg.listen.rpartition(":")
        handler=type("BoundHandler",(Handler,),{"node":self})
        srv=Server((host.strip("[]") or "127.0.0.1",int(port)),handler)
        if self.cfg.tls_cert:
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version=ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(self.cfg.tls_cert,self.cfg.tls_key)
            srv.socket=ctx.wrap_socket(srv.socket,server_side=True)
        return srv
def main(argv=None)->int:
    ap=argparse.ArgumentParser(prog="stuckbot-datanode",description="Keep StuckBot ticket content on your own servers.")
    sub=ap.add_subparsers(dest="cmd",required=True)
    s=sub.add_parser("serve",help="run the data node")
    s.add_argument("--config",required=True)
    t=sub.add_parser("trust",help="fetch StuckBot's grant keys and pin them in keys_file")
    t.add_argument("--config",required=True)
    p=sub.add_parser("put",help="store a file directly on this machine and print its data_refs entry")
    p.add_argument("--config",required=True)
    p.add_argument("file")
    p.add_argument("--store",default="main",help="the store's name in StuckBot")
    p.add_argument("--kind",choices=["body","file"],default="file")
    p.add_argument("--name")
    sub.add_parser("example-config",help="print an example config")
    a=ap.parse_args(argv)
    if a.cmd=="example-config":
        print(json.dumps(EXAMPLE,indent=2))
        return 0
    cfg=Config.load(a.config)
    if a.cmd=="trust":
        if not cfg.keys_file:
            print("set keys_file in the config first",file=sys.stderr)
            return 2
        data=fetch_jwks(cfg.issuer)
        keys=keys_from_jwks(data)
        if not keys:
            print("StuckBot returned no usable keys",file=sys.stderr)
            return 1
        os.makedirs(os.path.dirname(cfg.keys_file),exist_ok=True)
        with open(cfg.keys_file+".tmp","w") as f:
            json.dump(data,f,indent=2)
        os.replace(cfg.keys_file+".tmp",cfg.keys_file)
        print("pinned keys %s in %s"%(", ".join(sorted(keys)),cfg.keys_file))
        return 0
    if a.cmd=="put":
        with open(a.file,"rb") as f:
            data=f.read()
        obj=Store(cfg.storage).put_bytes(data)
        ref={"store":a.store,"object":obj,"kind":a.kind,"name":a.name or os.path.basename(a.file),"size":len(data)}
        print(json.dumps(ref))
        return 0
    node=Node(cfg)
    srv=node.server()
    print("stuckbot-datanode listening on %s for store %s (%d trusted keys)"%(cfg.listen,cfg.store_id,len(node.grants.keys)),flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
if __name__=="__main__":
    sys.exit(main())
