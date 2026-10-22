import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from stuckbot import Client,StuckBotError,merge_body
from stuckbot.attest import SigningKey,b64url
from stuckbot.datanode import Config,Grants,Node,keys_from_jwks
from mock_server import Mock
ISS="https://www.stuckbot.example"
STORE="9ccd239d-ca93-4556-9c8c-8f8aa778444a"
ORG="3889e9ad-5ca3-47a6-8d3a-e88a18dc42bd"
def jwk(key,kid):
    x,y=key.verifying_key().point
    return {"kty":"EC","crv":"P-256","x":b64url(x.to_bytes(32,"big")),"y":b64url(y.to_bytes(32,"big")),"kid":kid,"use":"sig","alg":"ES256"}
def mint(key,kid="k1",alg="ES256",**over):
    now=int(time.time())
    claims={"iss":ISS,"iat":now,"nbf":now-30,"jti":os.urandom(8).hex(),"aud":"stuckbot-data:"+STORE,"org":ORG,"store":STORE,"act":"read","sub":"agent:a1","tkt":"t1","ref":"SB-7","obj":[],"exp":now+300}
    claims.update(over)
    head=b64url(json.dumps({"alg":alg,"typ":"JWT","kid":kid}).encode())
    body=b64url(json.dumps(claims).encode())
    return head+"."+body+"."+b64url(key.sign((head+"."+body).encode()))
def obj(data):
    return "sha256:"+hashlib.sha256(data).hexdigest()
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=SigningKey.generate()
        cls.other=SigningKey.generate()
    def setUp(self):
        self.dir=tempfile.mkdtemp()
        keys=os.path.join(self.dir,"keys.json")
        with open(keys,"w") as f:
            json.dump({"keys":[jwk(self.key,"k1")]},f)
        self.cfg=Config({"store_id":STORE,"org_id":ORG,"issuer":ISS,"listen":"127.0.0.1:0","storage":os.path.join(self.dir,"data"),"keys_file":keys,"max_object_mb":1})
        self.node=Node(self.cfg)
        self.srv=self.node.server()
        self.url="http://127.0.0.1:%d"%self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever,kwargs={"poll_interval":0.02},daemon=True).start()
    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
    def call(self,method,path,token=None,data=None,headers=None):
        req=urllib.request.Request(self.url+path,data=data,method=method,headers=dict(headers or {}))
        if token:
            req.add_header("Authorization","Bearer "+token)
        try:
            with urllib.request.urlopen(req,timeout=5) as r:
                return r.status,dict(r.headers),r.read()
        except urllib.error.HTTPError as e:
            return e.code,dict(e.headers),e.read()
    def put(self,data,**over):
        return self.call("PUT","/objects/"+obj(data),mint(self.key,act="write",obj=None,max_bytes=1<<20,**over),data)
    def code(self,res):
        return json.loads(res[2])["error"]["code"]
    def audit(self):
        with open(self.cfg.audit_log) as f:
            return [json.loads(x) for x in f]
class NodeCase(Base):
    def test_write_then_read_with_bearer_grant(self):
        data=b"ticket details\n"
        self.assertEqual(self.put(data)[0],201)
        self.assertEqual(self.put(data)[0],200)
        s,h,body=self.call("GET","/objects/"+obj(data),mint(self.key,obj=[obj(data)]))
        self.assertEqual((s,body),(200,data))
        self.assertEqual(h["Cache-Control"],"no-store")
        self.assertEqual(h["X-Content-Type-Options"],"nosniff")
        self.assertNotIn("Content-Disposition",h)
        last=self.audit()[-1]
        self.assertEqual((last["method"],last["status"],last["sub"],last["ticket"],last["bytes"]),("GET",200,"agent:a1","SB-7",len(data)))
    def test_browser_link_downloads_with_a_safe_file_name(self):
        data=b"log"
        self.put(data)
        tok=mint(self.key,obj=[obj(data)])
        s,h,body=self.call("GET","/objects/%s?grant=%s&name=%s"%(obj(data),tok,"..%2F..%2Fetc%2Fpasswd%22.txt"))
        self.assertEqual((s,body),(200,data))
        self.assertTrue(h["Content-Disposition"].startswith('attachment; filename="etc_passwd_.txt"'),h["Content-Disposition"])
        self.assertIn("sandbox",h["Content-Security-Policy"])
    def test_query_grants_can_be_turned_off(self):
        data=b"x"
        self.put(data)
        self.cfg.allow_query_token=False
        s=self.call("GET","/objects/%s?grant=%s"%(obj(data),mint(self.key,obj=[obj(data)])))
        self.assertEqual((s[0],self.code(s)),(401,"missing_grant"))
    def test_refusals(self):
        data=b"secret"
        self.put(data)
        o=obj(data)
        cases=[
            (None,401,"missing_grant"),
            (mint(self.key,obj=[o],iat=int(time.time())-700,nbf=int(time.time())-700,exp=int(time.time())-400),401,"grant_expired"),
            (mint(self.key,obj=[o],aud="stuckbot-data:other"),403,"wrong_store"),
            (mint(self.key,obj=[o],store="other"),403,"wrong_store"),
            (mint(self.key,obj=[o],org="other-org"),403,"wrong_org"),
            (mint(self.key,obj=[o],iss="https://evil.example"),403,"wrong_issuer"),
            (mint(self.other,obj=[o]),401,"bad_signature"),
            (mint(self.key,kid="k9",obj=[o]),401,"unknown_key"),
            (mint(self.key,alg="HS256",obj=[o]),401,"bad_grant"),
            (mint(self.key,obj=["sha256:"+"0"*64]),403,"wrong_object"),
            (mint(self.key,obj=[o],act="write"),403,"wrong_action"),
            (mint(self.key,obj=[o],exp=int(time.time())+3600),401,"bad_grant"),
            (mint(self.key,obj=[o],iat=int(time.time())+600,nbf=int(time.time())+600,exp=int(time.time())+800),401,"grant_not_yet_valid"),
            ("a.b.c",401,"bad_grant"),
        ]
        for tok,status,code in cases:
            with self.subTest(code=code):
                s=self.call("GET","/objects/"+o,tok)
                self.assertEqual((s[0],self.code(s)),(status,code))
                self.assertNotIn(data,s[2])
        self.assertTrue(all(x["status"]>=400 for x in self.audit()[1:]))
    def test_writes_need_a_write_grant_and_matching_bytes(self):
        data=b"real bytes"
        s=self.call("PUT","/objects/"+obj(data),mint(self.key,obj=[obj(data)]),data)
        self.assertEqual((s[0],self.code(s)),(403,"wrong_action"))
        s=self.call("PUT","/objects/"+obj(b"other"),mint(self.key,act="write",obj=None),data)
        self.assertEqual((s[0],self.code(s)),(400,"digest_mismatch"))
        self.assertEqual(os.listdir(os.path.join(self.cfg.storage,"tmp")),[])
        self.assertFalse(self.node.store.has(obj(b"other")))
        big=b"x"*(2<<20)
        s=self.call("PUT","/objects/"+obj(big),mint(self.key,act="write",obj=None,max_bytes=1<<30),big)
        self.assertEqual((s[0],self.code(s)),(413,"too_large"))
        s=self.call("PUT","/objects/"+obj(data),mint(self.key,act="write",obj=None,max_bytes=4),data)
        self.assertEqual((s[0],self.code(s)),(413,"too_large"))
    def test_only_object_paths_exist(self):
        tok=mint(self.key,obj=["sha256:"+"a"*64])
        for path in ("/objects/../keys.json","/objects/sha256:"+"A"*64,"/objects/"+"a"*64,"/keys.json","/objects/sha256:"+"a"*64+"/x"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET",path,tok)[0],404)
        self.assertEqual(self.call("POST","/objects/sha256:"+"a"*64,tok,b"")[0],405)
        self.assertEqual(self.call("GET","/healthz")[0],200)
    def test_grants_are_checked_once_then_cached_until_they_expire(self):
        g=Grants(self.cfg,{"k1":self.key.verifying_key()})
        calls=[]
        real=g._verify
        g._verify=lambda t:calls.append(1) or real(t)
        tok=mint(self.key,obj=["sha256:"+"a"*64])
        for _ in range(3):
            g.check(tok,"read","sha256:"+"a"*64)
        self.assertEqual(len(calls),1)
    def test_keys_from_jwks_skips_other_key_types(self):
        keys=keys_from_jwks({"keys":[jwk(self.key,"k1"),{"kty":"RSA","kid":"r"},{"kty":"EC","crv":"P-384","kid":"p"}]})
        self.assertEqual(list(keys),["k1"])
class ClientDataCase(Base):
    def setUp(self):
        super().setUp()
        self.m=Mock()
        self.c=Client(token="sba_secret",base_url=self.m.url,max_retries=0,timeout=5)
    def tearDown(self):
        self.m.close()
        super().tearDown()
    def test_put_then_get_round_trip_without_crossing_tokens(self):
        self.m.on("POST","/api/v1/data-stores/main/upload-grants",lambda r:(200,{},{"store":"main","url":self.url+"/objects/","token":mint(self.key,act="write",obj=None),"expires_at":"x","max_bytes":1<<20,"note":""}))
        body={"objective":"fix it","context":{"logs":["boom"]}}
        ref=self.c.put_data("main",body,kind="body",name="details.json")
        self.assertEqual((ref["kind"],ref["media_type"],ref["store"]),("body","application/json","main"))
        log=self.c.put_data("main",b"build log",name="build.log")
        def grants(r):
            return 200,{},{"expires_at":"x","note":"","grants":[
                {"store":"main","object":ref["object"],"kind":"body","name":"details.json","available":True,"url":self.url+"/objects/"+ref["object"],"token":mint(self.key,obj=[ref["object"],log["object"]])},
                {"store":"main","object":log["object"],"kind":"file","name":"build.log","available":True,"url":self.url+"/objects/"+log["object"],"token":mint(self.key,obj=[ref["object"],log["object"]])},
                {"store":"gone","object":"sha256:"+"b"*64,"kind":"file","available":False,"reason":"removed"}]}
        self.m.on("POST","/api/v1/tickets/SB-7/data-grants",grants)
        items=self.c.get_data("SB-7")
        self.assertEqual(items[0].json(),body)
        self.assertEqual(items[1].data,b"build log")
        self.assertEqual((items[2].data,items[2].reason),(None,"removed"))
        self.assertTrue(all(r.token=="sba_secret" for r in self.m.requests))
        with open(self.cfg.audit_log) as f:
            self.assertNotIn("sba_secret",f.read())
    def test_bytes_that_do_not_match_the_ref_are_rejected(self):
        self.put(b"what the store holds")
        wrong="sha256:"+"c"*64
        self.m.on("POST","/api/v1/tickets/SB-7/data-grants",(200,{},{"expires_at":"x","note":"","grants":[{"store":"main","object":wrong,"kind":"file","available":True,"url":self.url+"/objects/"+obj(b"what the store holds"),"token":mint(self.key,obj=[obj(b"what the store holds")])}]}))
        with self.assertRaises(StuckBotError) as cm:
            self.c.get_data("SB-7")
        self.assertEqual(cm.exception.code,"digest_mismatch")
    def test_store_errors_surface_with_their_code(self):
        self.m.on("POST","/api/v1/tickets/SB-7/data-grants",(200,{},{"expires_at":"x","note":"","grants":[{"store":"main","object":"sha256:"+"d"*64,"kind":"file","available":True,"url":self.url+"/objects/sha256:"+"d"*64,"token":mint(self.key,obj=["sha256:"+"e"*64])}]}))
        with self.assertRaises(StuckBotError) as cm:
            self.c.get_data("SB-7")
        self.assertEqual((cm.exception.status,cm.exception.code),(403,"wrong_object"))
class MergeBodyCase(unittest.TestCase):
    def test_hosted_fields_win_and_context_merges(self):
        pkg={"title":"T","mode":"blocker_only","context":{"workspace":{"repo":"r"}},"data_refs":[]}
        body={"title":"other","objective":"o","context":{"logs":["l"],"workspace":{"repo":"x"}}}
        out=merge_body(pkg,body)
        self.assertEqual(out["title"],"T")
        self.assertEqual(out["objective"],"o")
        self.assertEqual(out["context"],{"logs":["l"],"workspace":{"repo":"r"}})
        with self.assertRaises(ValueError):
            merge_body(pkg,["not","an","object"])
if __name__=="__main__":
    unittest.main()
