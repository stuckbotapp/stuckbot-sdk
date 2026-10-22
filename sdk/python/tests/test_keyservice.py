import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from stuckbot.attest import SigningKey,b64url
from stuckbot.keyservice import KeyConfig,KeyNode,kek_id,seal,unseal
from test_datanode import ISS,ORG,jwk
KEY="5b0c7f53-3c1e-4a49-a8ad-8a43a7e36b0f"
def mint(signer,data,kid="k1",**over):
    now=int(time.time())
    claims={"iss":ISS,"iat":now,"nbf":now-30,"jti":os.urandom(8).hex(),"aud":"stuckbot-keys:"+ORG,"org":ORG,"act":"wrap","key":KEY,"body":hashlib.sha256(data).hexdigest(),"exp":now+60}
    claims.update(over)
    head=b64url(json.dumps({"alg":"ES256","typ":"JWT","kid":kid}).encode())
    payload=b64url(json.dumps(claims).encode())
    return head+"."+payload+"."+b64url(signer.sign((head+"."+payload).encode()))
class KeyServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=SigningKey.generate()
        cls.other=SigningKey.generate()
    def setUp(self):
        self.dir=tempfile.mkdtemp()
        self.keys=os.path.join(self.dir,"keys.json")
        with open(self.keys,"w") as f:
            json.dump({"keys":[jwk(self.key,"k1"),jwk(self.other,"k2")]},f)
        self.keks=[]
        for i in range(2):
            p=os.path.join(self.dir,"kek%d.key"%i)
            with open(p,"w") as f:
                f.write(base64.b64encode(os.urandom(32)).decode())
            self.keks.append(p)
        self.start([self.keks[0]])
    def start(self,keks,cache=300):
        if getattr(self,"srv",None):
            self.stop()
        self.cfg=KeyConfig({"org_id":ORG,"issuer":ISS,"listen":"127.0.0.1:0","keys_file":self.keys,"kek_files":keks,"cache_seconds":cache,"audit_log":os.path.join(self.dir,"audit.jsonl")})
        self.node=KeyNode(self.cfg)
        self.node.grants.keys.pop("k2")
        self.srv=self.node.server()
        self.url="http://127.0.0.1:%d"%self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever,kwargs={"poll_interval":0.02},daemon=True).start()
    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
    def tearDown(self):
        self.stop()
    def post(self,path,body,token=None,act=None,**over):
        data=json.dumps(body).encode()
        if token is None:
            token=mint(self.key,data,act=act or path.rsplit("/",1)[-1],**over)
        req=urllib.request.Request(self.url+path,data=data,method="POST",headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(req,timeout=5) as r:
                return r.status,json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code,json.loads(e.read())
    def wrap(self,plain,**over):
        return self.post("/v1/wrap",{"org":ORG,"key":KEY,"plaintext":base64.b64encode(plain).decode()},**over)
    def unwrap(self,wrapped,key=KEY,**over):
        return self.post("/v1/unwrap",{"org":ORG,"key":key,"wrapped":wrapped},key=key,**over)
    def audit(self):
        with open(self.cfg.audit_log) as f:
            return [json.loads(x) for x in f]
    def test_seal_round_trip_and_tamper(self):
        k=os.urandom(32)
        blob=seal(k,b"secret org key bytes 32 long....",b"aad")
        self.assertEqual(unseal(k,blob,b"aad"),b"secret org key bytes 32 long....")
        self.assertIsNone(unseal(k,blob,b"other"))
        self.assertIsNone(unseal(os.urandom(32),blob,b"aad"))
        bad=bytearray(blob)
        bad[20]^=1
        self.assertIsNone(unseal(k,bytes(bad),b"aad"))
        self.assertNotEqual(seal(k,b"x"*32,b"aad"),seal(k,b"x"*32,b"aad"))
    def test_wrap_and_unwrap(self):
        plain=os.urandom(32)
        st,out=self.wrap(plain)
        self.assertEqual(st,200,out)
        self.assertEqual(out["cache_seconds"],300)
        self.assertTrue(out["wrapped"].startswith(self.node.keks[0][0]+"."))
        self.assertNotIn(base64.b64encode(plain).decode(),json.dumps(out))
        st,back=self.unwrap(out["wrapped"])
        self.assertEqual(st,200,back)
        self.assertEqual(base64.b64decode(back["plaintext"]),plain)
        log=self.audit()
        self.assertEqual([x["act"] for x in log],["wrap","unwrap"])
        self.assertTrue(all(x["status"]==200 and x["key"]==KEY for x in log))
        self.assertFalse(any(base64.b64encode(plain).decode() in json.dumps(x) for x in log))
    def test_bound_to_org_and_key(self):
        _,out=self.wrap(os.urandom(32))
        other="7d6f3c8a-0b0e-4c1e-9d55-0d1c2b3a4f5e"
        st,err=self.unwrap(out["wrapped"],key=other)
        self.assertEqual((st,err["error"]["code"]),(422,"cannot_unwrap"))
        data=json.dumps({"org":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","key":KEY,"wrapped":out["wrapped"]}).encode()
        st,err=self.post("/v1/unwrap",{"org":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","key":KEY,"wrapped":out["wrapped"]},token=mint(self.key,data,act="unwrap"))
        self.assertEqual((st,err["error"]["code"]),(403,"wrong_org"))
    def test_token_checks(self):
        plain=base64.b64encode(os.urandom(32)).decode()
        body={"org":ORG,"key":KEY,"plaintext":plain}
        data=json.dumps(body).encode()
        cases=[
            (mint(self.other,data,kid="k2"),401,"unknown_key"),
            (mint(self.other,data),401,"bad_signature"),
            (mint(self.key,data,aud="stuckbot-keys:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),403,"wrong_audience"),
            (mint(self.key,data,aud="stuckbot-data:x"),403,"wrong_audience"),
            (mint(self.key,data,iss="https://evil.example"),403,"wrong_issuer"),
            (mint(self.key,data,act="unwrap"),403,"wrong_action"),
            (mint(self.key,data,body=hashlib.sha256(b"other").hexdigest()),403,"body_mismatch"),
            (mint(self.key,data,key="7d6f3c8a-0b0e-4c1e-9d55-0d1c2b3a4f5e"),403,"wrong_key"),
            (mint(self.key,data,exp=int(time.time())+600),401,"bad_grant"),
            (mint(self.key,data,iat=int(time.time())-400,nbf=int(time.time())-400,exp=int(time.time())-300),401,"grant_expired"),
        ]
        for token,status,code in cases:
            st,err=self.post("/v1/wrap",body,token=token)
            self.assertEqual((st,err["error"]["code"]),(status,code),code)
        st,err=self.post("/v1/wrap",body,token=mint(self.key,data,iat=int(time.time())-30,nbf=int(time.time())-30,exp=int(time.time())+30))
        self.assertEqual((st,err["error"]["code"]),(401,"issued_before_start"),"a token from before a restart cannot be replayed")
        token=mint(self.key,data)
        self.assertEqual(self.post("/v1/wrap",body,token=token)[0],200)
        st,err=self.post("/v1/wrap",body,token=token)
        self.assertEqual((st,err["error"]["code"]),(401,"replayed"))
        self.assertEqual([x.get("reason") for x in self.audit()][-1],"replayed")
    def test_rotation_and_shred(self):
        plain=os.urandom(32)
        _,old=self.wrap(plain)
        self.start([self.keks[1],self.keks[0]],cache=0)
        _,new=self.wrap(plain)
        self.assertEqual(new["cache_seconds"],0)
        self.assertNotEqual(old["wrapped"].split(".")[0],new["wrapped"].split(".")[0])
        self.assertEqual(base64.b64decode(self.unwrap(old["wrapped"])[1]["plaintext"]),plain)
        self.assertEqual(base64.b64decode(self.unwrap(new["wrapped"])[1]["plaintext"]),plain)
        self.start([self.keks[1]])
        st,err=self.unwrap(old["wrapped"])
        self.assertEqual((st,err["error"]["code"]),(422,"unknown_kek"))
    def test_bad_requests(self):
        st,err=self.wrap(b"short")
        self.assertEqual((st,err["error"]["code"]),(400,"bad_request"))
        st,err=self.post("/v1/other",{},act="wrap")
        self.assertEqual((st,err["error"]["code"]),(404,"not_found"))
        with urllib.request.urlopen(self.url+"/healthz",timeout=5) as r:
            self.assertEqual(json.loads(r.read()),{"ok":True})
    def test_config_checks(self):
        with self.assertRaises(ValueError):
            KeyConfig({"org_id":ORG,"kek_files":[]})
        with self.assertRaises(ValueError):
            KeyConfig({"org_id":ORG,"kek_files":["a"],"cache_seconds":301})
        bad=os.path.join(self.dir,"bad.key")
        with open(bad,"w") as f:
            f.write(base64.b64encode(b"x"*16).decode())
        with self.assertRaises(ValueError):
            KeyNode(KeyConfig({"org_id":ORG,"issuer":ISS,"keys_file":self.keys,"kek_files":[bad],"audit_log":os.path.join(self.dir,"a.jsonl")}))
        self.assertEqual(len(kek_id(os.urandom(32))),12)
if __name__=="__main__":
    unittest.main()
