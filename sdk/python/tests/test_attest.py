import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from stuckbot.attest import N,SigningKey,VerifyingKey,attestation,check_attestation,unb64url
from stuckbot.verifier import Config,plan,run_command
RFC_KEY=0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
class AttestTest(unittest.TestCase):
    def test_rfc6979_vectors(self):
        k=SigningKey(RFC_KEY)
        self.assertEqual(k.public,(0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6,0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299))
        self.assertEqual(k.sign(b"sample").hex().upper(),"EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8")
        self.assertEqual(k.sign(b"test").hex().upper(),"F1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083")
    def test_sign_verify_and_tamper(self):
        k=SigningKey.generate()
        vk=k.verifying_key()
        for i in range(20):
            m=("m%d"%i).encode()
            s=k.sign(m)
            self.assertTrue(vk.verify(m,s))
            self.assertFalse(vk.verify(m+b"x",s))
            self.assertFalse(vk.verify(m,s[:63]+bytes([s[63]^1])))
        self.assertFalse(vk.verify(b"m",b"\x00"*64))
        self.assertFalse(vk.verify(b"m",N.to_bytes(32,"big")*2))
    def test_pem_round_trip_and_fingerprint(self):
        k=SigningKey.generate()
        k2=SigningKey.from_pem(k.to_pem())
        self.assertEqual(k.d,k2.d)
        vk=VerifyingKey.from_pem(k.verifying_key().to_pem())
        self.assertEqual(vk.point,k.public)
        self.assertEqual(len(vk.fingerprint()),64)
        with self.assertRaises(ValueError):
            VerifyingKey.from_pem(k.to_pem())
    @unittest.skipUnless(shutil.which("openssl"),"openssl not installed")
    def test_openssl_interop(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["openssl","genpkey","-algorithm","EC","-pkeyopt","ec_paramgen_curve:P-256","-out",d+"/k.pem"],check=True,capture_output=True)
            k=SigningKey.load(d+"/k.pem")
            pub=subprocess.run(["openssl","pkey","-in",d+"/k.pem","-pubout"],check=True,capture_output=True,text=True).stdout
            self.assertEqual(VerifyingKey.from_pem(pub).point,k.public)
            open(d+"/pub.pem","w").write(k.verifying_key().to_pem())
            open(d+"/m","wb").write(b"hello")
            s=k.sign(b"hello")
            r,t=int.from_bytes(s[:32],"big"),int.from_bytes(s[32:],"big")
            def i(x):
                b=x.to_bytes((x.bit_length()+8)//8,"big")
                return b"\x02"+bytes([len(b)])+b
            body=i(r)+i(t)
            open(d+"/sig","wb").write(b"\x30"+bytes([len(body)])+body)
            v=subprocess.run(["openssl","dgst","-sha256","-verify",d+"/pub.pem","-signature",d+"/sig",d+"/m"],capture_output=True,text=True)
            self.assertIn("Verified OK",v.stdout)
    def test_attestation_binds_its_fields(self):
        k=SigningKey.generate()
        a=attestation(k,"v1","t1","b1","blocker","command_run",{"command":"make test","exit_code":0})
        st=check_attestation(k.verifying_key(),a)
        self.assertEqual((st["v"],st["verification_id"],st["ticket_id"],st["criterion_key"],st["stage"],st["type"]),(1,"v1","t1","b1","blocker","command_run"))
        self.assertEqual(json.loads(unb64url(a["statement"])),st)
        forged=dict(a,statement=base64.urlsafe_b64encode(unb64url(a["statement"]).replace(b'"v1"',b'"v2"')).rstrip(b"=").decode())
        self.assertIsNone(check_attestation(k.verifying_key(),forged))
        self.assertIsNone(check_attestation(SigningKey.generate().verifying_key(),a))
class VerifierTest(unittest.TestCase):
    def cfg(self,**kw):
        d=tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree,d)
        data={"workdir":d,"allow":["python3 -c 'print(1)'",["true"]],"env":{"PATH":os.environ["PATH"]},"timeout_seconds":5}
        data.update(kw)
        return Config(data)
    def ticket(self,*criteria):
        return {"id":"t","content":{"package":{"blocker_criteria":list(criteria)}}}
    def test_plan_runs_only_listed_commands(self):
        cfg=self.cfg()
        ok=plan(self.ticket({"key":"b1","method":"command","command":"python3 -c \"print(1)\"","expected":"1"},{"key":"b2","method":"manual","expected":"looks right"}),"blocker",cfg)
        self.assertEqual([c["key"] for c in ok],["b1"])
        self.assertIsNone(plan(self.ticket({"key":"b1","method":"command","command":"python3 -c 'print(1)'; rm -rf /","expected":"x"}),"blocker",cfg))
        self.assertIsNone(plan(self.ticket({"key":"b1","method":"command","command":"true --extra","expected":"x"}),"blocker",cfg))
        self.assertIsNone(plan(self.ticket({"key":"b1","method":"external_check","url":"https://example.com","expected":"200"}),"blocker",cfg))
        self.assertIsNone(plan(self.ticket({"key":"b1","method":"manual","expected":"x"}),"blocker",cfg))
    def test_config_needs_an_allow_list(self):
        with self.assertRaises(ValueError):
            Config({"allow":[]})
    def test_run_command_uses_no_shell_env_or_endless_time(self):
        cfg=self.cfg(timeout_seconds=1)
        os.environ["STUCKBOT_SECRET_PROBE"]="leak"
        try:
            r=run_command([sys.executable,"-c","import os;print(os.environ.get('STUCKBOT_SECRET_PROBE'));exit(3)"],cfg)
        finally:
            del os.environ["STUCKBOT_SECRET_PROBE"]
        self.assertEqual(r["exit_code"],3)
        self.assertIn("None",r["output_excerpt"])
        self.assertEqual(run_command(["echo","$HOME;","id"],cfg)["output_excerpt"].strip(),"$HOME; id")
        self.assertEqual(run_command([sys.executable,"-c","import time;time.sleep(5)"],cfg)["exit_code"],124)
        self.assertEqual(run_command(["/no/such/program"],cfg)["exit_code"],127)
        big=run_command([sys.executable,"-c","print('x'*100000)"],cfg)
        self.assertLessEqual(len(big["output_excerpt"]),2000)
        self.assertEqual(len(big["output_sha256"]),64)
if __name__=="__main__":
    unittest.main()
