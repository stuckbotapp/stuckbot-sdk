"""stuckbot-datanode keys: keep your org's encryption key on your own server (BYOK).

StuckBot encrypts ticket content with a key per ticket chain, and those keys with one org key.
Normally StuckBot's own master key locks the org key. With a key service, the org key is locked
by a key-encryption key (KEK) that only this program holds, and StuckBot keeps just the locked copy.
To read or add content, StuckBot asks this service to unlock the org key and keeps the result in
memory for cache_seconds (StuckBot keeps it at least 30 and at most 300 seconds).

So you decide: stop this service and StuckBot loses access to your org's ticket content once its
cache runs out; delete every KEK file and that content is gone for good, in StuckBot's database and
in all of its backups. Keep a safe copy of each KEK file: without it nothing can be recovered.

Every request carries a token StuckBot signs with the same ES256 key as data grants. This service
checks the signature against StuckBot's pinned keys, that the token is for this org, for this
action, for this exact request body, lives at most two minutes and is used once. Every request is
appended to the audit log. StuckBot must reach it over public https on port 443, so put it behind a
reverse proxy or give it a certificate, and allow only StuckBot's address in your firewall.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from typing import Any,Dict,List,Optional,Tuple
from .attest import b64url,unb64url
from .datanode import Grants,GrantError,Handler,Node,pin_keys
MAX_BODY=16384
EXAMPLE={
"org_id":"<org id from Security, Your own key>",
"issuer":"https://www.stuckbot.com",
"listen":"127.0.0.1:8760",
"keys_file":"/etc/stuckbot-keys/grant-keys.json",
"kek_files":["/etc/stuckbot-keys/kek-1.key"],
"cache_seconds":300,
"audit_log":"/var/lib/stuckbot-keys/audit.jsonl",
"tls_cert":None,
"tls_key":None,
}
def kek_id(kek:bytes)->str:
    return b64url(hashlib.sha256(b"stuckbot-kek-id|"+kek).digest())[:12]
def _sub(kek:bytes,label:bytes)->bytes:
    return hmac.new(kek,b"stuckbot-kek|"+label,hashlib.sha256).digest()
def _stream(key:bytes,nonce:bytes,n:int)->bytes:
    out=b""
    i=0
    while len(out)<n:
        out+=hmac.new(key,nonce+i.to_bytes(4,"big"),hashlib.sha256).digest()
        i+=1
    return out[:n]
def _mac(kek:bytes,aad:bytes,nonce:bytes,ct:bytes)->bytes:
    return hmac.new(_sub(kek,b"mac"),len(aad).to_bytes(4,"big")+aad+nonce+ct,hashlib.sha256).digest()
def seal(kek:bytes,plain:bytes,aad:bytes)->bytes:
    nonce=os.urandom(16)
    ct=bytes(a^b for a,b in zip(plain,_stream(_sub(kek,b"enc"),nonce,len(plain))))
    return nonce+ct+_mac(kek,aad,nonce,ct)
def unseal(kek:bytes,blob:bytes,aad:bytes)->Optional[bytes]:
    if len(blob)<48:
        return None
    nonce,ct,tag=blob[:16],blob[16:-32],blob[-32:]
    if not hmac.compare_digest(tag,_mac(kek,aad,nonce,ct)):
        return None
    return bytes(a^b for a,b in zip(ct,_stream(_sub(kek,b"enc"),nonce,len(ct))))
def read_kek(path:str)->bytes:
    with open(path) as f:
        raw=base64.b64decode(f.read().strip(),validate=True)
    if len(raw)!=32:
        raise ValueError("%s must hold 32 random bytes in base64 (make one with stuckbot-datanode keys new-kek)"%path)
    return raw
class KeyConfig:
    def __init__(self,data:Dict[str,Any],base:str="."):
        path=lambda v:os.path.abspath(os.path.join(base,os.path.expanduser(str(v))))
        self.org_id=str(data.get("org_id",""))
        if not re.match(r"^[0-9a-f-]{36}$",self.org_id):
            raise ValueError("org_id must be the org id shown under Security, Your own key")
        self.store_id=""
        self.issuer=str(data.get("issuer","https://www.stuckbot.com")).rstrip("/")
        self.listen=str(data.get("listen","127.0.0.1:8760"))
        self.keys_file=path(data["keys_file"]) if data.get("keys_file") else None
        self.kek_files=[path(x) for x in data.get("kek_files") or []]
        if not self.kek_files:
            raise ValueError("kek_files must list at least one key file; the first one locks new keys")
        self.cache_seconds=int(data.get("cache_seconds",300))
        if not 0<=self.cache_seconds<=300:
            raise ValueError("cache_seconds must be 0 to 300")
        self.audit_log=path(data.get("audit_log","/var/lib/stuckbot-keys/audit.jsonl"))
        self.tls_cert=path(data["tls_cert"]) if data.get("tls_cert") else None
        self.tls_key=path(data["tls_key"]) if data.get("tls_key") else None
        self.trusted_proxies=[str(x) for x in data.get("trusted_proxies",[])]
        self.allow_query_token=False
    @classmethod
    def load(cls,path:str)->"KeyConfig":
        with open(path) as f:
            return cls(json.load(f),os.path.dirname(os.path.abspath(path)))
class KeyGrants(Grants):
    max_lifetime=120
    def _audience(self,claims:Dict[str,Any]):
        if claims.get("aud")!="stuckbot-keys:"+self.cfg.org_id:
            raise GrantError(403,"wrong_audience","This token is not for this key service.")
class KeyHandler(Handler):
    server_version="stuckbot-keyservice"
    node:"KeyNode"
    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path=="/healthz":
            return self._send(200,{"ok":True})
        self._send(404,{"error":{"code":"not_found","message":"POST to /v1/wrap or /v1/unwrap."}})
    def do_PUT(self):
        self._send(405,{"error":{"code":"method_not_allowed","message":"POST to /v1/wrap or /v1/unwrap."}},{"Allow":"GET, POST"})
    def do_POST(self):
        act={"/v1/wrap":"wrap","/v1/unwrap":"unwrap"}.get(urllib.parse.urlsplit(self.path).path)
        claims=None
        key=None
        try:
            if act is None:
                raise GrantError(404,"not_found","POST to /v1/wrap or /v1/unwrap.")
            length=self.headers.get("Content-Length")
            if length is None or not length.isdigit() or int(length)>MAX_BODY:
                self.close_connection=True
                raise GrantError(413,"bad_length","Send a JSON body of at most %d bytes with Content-Length."%MAX_BODY)
            raw=self.rfile.read(int(length))
            claims=self.node.grants.check(self._token({}),act)
            key=claims.get("key")
            self.node.once(claims)
            if not hmac.compare_digest(str(claims.get("body","")),hashlib.sha256(raw).hexdigest()):
                raise GrantError(403,"body_mismatch","The token was signed for a different request body.")
            try:
                req=json.loads(raw)
            except ValueError:
                raise GrantError(400,"bad_request","The body is not JSON.")
            if not isinstance(req,dict) or req.get("org")!=self.node.cfg.org_id:
                raise GrantError(403,"wrong_org","The request is for another org.")
            if not isinstance(key,str) or not re.match(r"^[0-9a-f-]{36}$",key) or req.get("key")!=key:
                raise GrantError(403,"wrong_key","The request names a different key than its token.")
            aad=("stuckbot-org-key|%s|%s"%(self.node.cfg.org_id,key)).encode()
            out,kid=self.node.wrap(req,aad) if act=="wrap" else self.node.unwrap(req,aad)
        except GrantError as e:
            self.node.audit(self,act,key,e.status,claims,reason=e.code)
            return self._send(e.status,{"error":{"code":e.code,"message":str(e)}})
        self.node.audit(self,act,key,200,claims,kek=kid)
        out["cache_seconds"]=self.node.cfg.cache_seconds
        self._send(200,out)
class KeyNode(Node):
    handler=KeyHandler
    def __init__(self,cfg:KeyConfig,grants:Optional[Grants]=None):
        self.cfg=cfg
        self.grants=grants or KeyGrants(cfg)
        self.keks:List[Tuple[str,bytes]]=[]
        for f in cfg.kek_files:
            k=read_kek(f)
            self.keks.append((kek_id(k),k))
        self._audit_lock=threading.Lock()
        self._used:Dict[str,float]={}
        self.started=time.time()
        os.makedirs(os.path.dirname(cfg.audit_log),mode=0o700,exist_ok=True)
    def once(self,claims:Dict[str,Any]):
        jti=str(claims.get("jti") or "")
        now=time.time()
        with self._audit_lock:
            if len(self._used)>5000:
                self._used={k:v for k,v in self._used.items() if v>now-60}
            if float(claims.get("iat",0))<self.started-2:
                raise GrantError(401,"issued_before_start","This token was issued before the key service started. StuckBot signs a new one for every request.")
            if not jti or jti in self._used:
                raise GrantError(401,"replayed","This token was already used. StuckBot signs a new one for every request.")
            self._used[jti]=float(claims["exp"])
    def wrap(self,req:Dict[str,Any],aad:bytes)->Tuple[Dict[str,Any],str]:
        try:
            plain=base64.b64decode(str(req.get("plaintext","")),validate=True)
        except ValueError:
            plain=b""
        if not 16<=len(plain)<=64:
            raise GrantError(400,"bad_request","plaintext must be a base64 key of 16 to 64 bytes.")
        kid,kek=self.keks[0]
        return {"wrapped":kid+"."+b64url(seal(kek,plain,aad))},kid
    def unwrap(self,req:Dict[str,Any],aad:bytes)->Tuple[Dict[str,Any],str]:
        kid,_,blob=str(req.get("wrapped","")).partition(".")
        kek=dict(self.keks).get(kid)
        if kek is None:
            raise GrantError(422,"unknown_kek","This key service does not hold the key file this org key was locked with (%s). Add it back to kek_files."%kid[:12])
        try:
            plain=unseal(kek,unb64url(blob),aad)
        except ValueError:
            plain=None
        if plain is None:
            raise GrantError(422,"cannot_unwrap","The locked key does not open with %s for this org and key."%kid)
        return {"plaintext":base64.b64encode(plain).decode()},kid
    def audit(self,h:Handler,act:Optional[str],key:Optional[str],status:int,claims:Optional[Dict[str,Any]],reason:Optional[str]=None,kek:Optional[str]=None):
        line={"at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"ip":h._ip(),"act":act,"key":key,"status":status}
        if claims:
            line["jti"]=claims.get("jti")
        if kek:
            line["kek"]=kek
        if reason:
            line["reason"]=reason
        with self._audit_lock:
            fd=os.open(self.cfg.audit_log,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
            with os.fdopen(fd,"a") as f:
                f.write(json.dumps(line)+"\n")
def main(argv=None)->int:
    ap=argparse.ArgumentParser(prog="stuckbot-datanode keys",description="Hold your org's StuckBot encryption key on your own server.")
    sub=ap.add_subparsers(dest="cmd",required=True)
    s=sub.add_parser("serve",help="run the key service")
    s.add_argument("--config",required=True)
    t=sub.add_parser("trust",help="fetch StuckBot's signing keys and pin them in keys_file")
    t.add_argument("--config",required=True)
    n=sub.add_parser("new-kek",help="write a new random key-encryption key file")
    n.add_argument("path")
    sub.add_parser("example-config",help="print an example config")
    a=ap.parse_args(argv)
    if a.cmd=="example-config":
        print(json.dumps(EXAMPLE,indent=2))
        return 0
    if a.cmd=="new-kek":
        kek=os.urandom(32)
        os.makedirs(os.path.dirname(os.path.abspath(a.path)),exist_ok=True)
        fd=os.open(a.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as f:
            f.write(base64.b64encode(kek).decode()+"\n")
        print("wrote %s (id %s). Put it first in kek_files to lock new keys with it, keep the older files listed, and keep a safe copy: without it your content cannot be recovered."%(a.path,kek_id(kek)))
        return 0
    cfg=KeyConfig.load(a.config)
    if a.cmd=="trust":
        return pin_keys(cfg)
    node=KeyNode(cfg)
    srv=node.server()
    print("stuckbot key service listening on %s for org %s (key files: %s; current %s)"%(cfg.listen,cfg.org_id,", ".join(k for k,_ in node.keks),node.keks[0][0]),flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
if __name__=="__main__":
    sys.exit(main())
