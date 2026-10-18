import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from mock_server import Mock,err,iso
from stuckbot import ClaimLost,FleetAgent,StuckBotError,WorkItem
class FakeFleet:
    def __init__(self,ttl=3600):
        self.m=Mock()
        self.ttl=ttl
        self.n=0
        self.valid={}
        self.renewable=set()
        self.renew_ok=True
        self.handoff=None
        self.stop=None
        self.m.on("POST","/api/v1/agents/register",self.register)
        self.m.on("POST","/api/v1/agents/me/token",self.renew)
        self.m.on("GET","/api/v1/agents/me",self.guard(lambda r,a:(200,{},{"agent":a,"org_id":"o","retry_after_seconds":60})))
        self.m.on("POST","/api/v1/agents/me/heartbeat",self.guard(lambda r,a:(200,{},{"ok":True,"handoff_requested":self.handoff,"stop_requested":self.stop,"retry_after_seconds":60,"token_expires_soon":False})))
    def agent(self,i):
        return {"id":f"00000000-0000-4000-8000-{i:012d}","name":f"fleet/agent-{i}"}
    def issue(self,a):
        self.n+=1
        tok=f"sba_{self.n}"
        self.valid[tok]=a
        return {"token":tok,"token_type":"Bearer","expires_at":iso(time.time()+self.ttl),"agent":a}
    def register(self,r):
        if r.token!="sbe_secret":
            return err(401,"invalid_token")
        return 201,{},self.issue(self.agent(self.n+1))
    def renew(self,r):
        a=self.valid.get(r.token)
        if a is None and not (self.renew_ok and r.token in self.renewable):
            return err(401,"token_expired")
        a=a or self.renewable_agent
        return 200,{},self.issue(a)
    def guard(self,fn):
        def h(r):
            a=self.valid.get(r.token)
            if a is None:
                return err(401,"token_expired")
            return fn(r,a)
        return h
    def expire(self,tok,renewable=False):
        a=self.valid.pop(tok)
        if renewable:
            self.renewable.add(tok)
            self.renewable_agent=a
class FleetTest(unittest.TestCase):
    def setUp(self):
        self.f=FakeFleet()
        self.m=self.f.m
        self.dir=tempfile.mkdtemp(prefix="sbsdk-")
        self.state=os.path.join(self.dir,"agent.json")
        self.agents=[]
    def tearDown(self):
        for a in self.agents:
            a.stop()
        self.m.close()
        shutil.rmtree(self.dir,ignore_errors=True)
    def read_state(self):
        with open(self.state) as f:
            return json.load(f)
    def make(self,**kw):
        kw.setdefault("heartbeat_interval",3600)
        a=FleetAgent("sbe_secret",base_url=self.m.url,max_retries=0,**kw)
        self.agents.append(a)
        return a
    def test_registers_with_labels_and_heartbeats(self):
        a=self.make(labels={"host":"ci-7"},provider="anthropic",supports_stop=True,heartbeat_payload=lambda:{"usage":{"tokens":5},"status":"busy"}).start()
        reg=self.m.calls("POST","/api/v1/agents/register")[0]
        self.assertEqual(reg.headers["authorization"],"Bearer sbe_secret")
        self.assertEqual(reg.json,{"labels":{"host":"ci-7"},"provider":"anthropic","supports_stop":True})
        self.assertTrue(reg.headers.get("idempotency-key"))
        for _ in range(100):
            if self.m.calls("POST","/api/v1/agents/me/heartbeat"):
                break
            time.sleep(0.02)
        hb=self.m.calls("POST","/api/v1/agents/me/heartbeat")[0]
        self.assertEqual(hb.json,{"status":"busy","usage":{"tokens":5}})
        self.assertEqual(hb.headers["authorization"],"Bearer "+a.token)
    def test_state_file_is_private_and_reused(self):
        a=self.make(state_file=self.state).start()
        a.stop()
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode),0o600)
        saved=self.read_state()
        self.assertEqual((saved["agent_id"],saved["token"]),(a.agent_id,a.token))
        b=self.make(state_file=self.state).start()
        self.assertEqual(b.agent_id,a.agent_id)
        self.assertEqual(b.registrations,0)
        self.assertEqual(len(self.m.calls("POST","/api/v1/agents/register")),1)
        self.assertEqual(self.m.calls("GET","/api/v1/agents/me")[0].token,a.token)
    def test_state_file_reregisters_when_token_is_rejected(self):
        a=self.make(state_file=self.state).start()
        a.stop()
        self.f.expire(a.token)
        b=self.make(state_file=self.state).start()
        self.assertNotEqual(b.agent_id,a.agent_id)
        self.assertEqual(b.registrations,1)
        self.assertEqual(self.read_state()["agent_id"],b.agent_id)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode),0o600)
    def test_state_file_with_expired_token_registers_without_trying_it(self):
        with open(self.state,"w") as f:
            json.dump({"base_url":self.m.url,"agent_id":"x","token":"sba_old","expires_at":iso(time.time()-10)},f)
        b=self.make(state_file=self.state).start()
        self.assertEqual(b.registrations,1)
        self.assertEqual(self.m.calls("GET","/api/v1/agents/me"),[])
    def test_401_renews_token_once_and_retries(self):
        a=self.make().start()
        old=a.token
        self.f.expire(old,renewable=True)
        me=a.client.get_me()
        self.assertEqual(me["agent"]["id"],a.agent_id)
        self.assertNotEqual(a.token,old)
        seq=[(r.method,r.path,r.token) for r in self.m.requests if r.path!="/api/v1/agents/me/heartbeat"]
        self.assertEqual(seq[-3:],[("GET","/api/v1/agents/me",old),("POST","/api/v1/agents/me/token",old),("GET","/api/v1/agents/me",a.token)])
        self.assertEqual(a.renewals,1)
    def test_401_registers_again_when_renewal_fails(self):
        a=self.make().start()
        first=a.agent_id
        self.f.expire(a.token)
        me=a.client.get_me()
        self.assertNotEqual(a.agent_id,first)
        self.assertEqual(me["agent"]["id"],a.agent_id)
        self.assertEqual(a.registrations,2)
    def test_401_retry_happens_only_once(self):
        a=self.make().start()
        self.m.on("GET","/api/v1/agents/me",err(401,"token_expired"))
        with self.assertRaises(StuckBotError) as cm:
            a.client.get_me()
        self.assertEqual(cm.exception.code,"token_expired")
        self.assertEqual(len(self.m.calls("GET","/api/v1/agents/me")),2)
    def test_token_is_renewed_before_expiry(self):
        self.f.ttl=3
        a=self.make(state_file=self.state,renew_before=2.5).start()
        first=a.token
        for _ in range(150):
            if a.renewals and self.read_state()["token"]==a.token:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(a.renewals,1)
        self.assertNotEqual(a.token,first)
        self.assertEqual(self.read_state()["token"],a.token)
        self.assertEqual(self.m.calls("POST","/api/v1/agents/me/token")[0].token,first)
    def test_handoff_and_stop_requests_reach_callbacks(self):
        seen=[]
        a=self.make(supports_stop=True,on_handoff_requested=lambda h:seen.append(("handoff",h["reason"])),on_stop_requested=lambda s:seen.append(("stop",s["reason"]))).start()
        a.heartbeat()
        self.assertIsNone(a.handoff_requested)
        self.f.handoff={"at":iso(time.time()),"reason":"budget","signal_id":None}
        a.heartbeat()
        a.heartbeat()
        self.f.stop={"at":iso(time.time()),"reason":"done"}
        a.heartbeat()
        self.assertEqual(seen,[("handoff","budget"),("stop","done")])
        self.assertEqual(a.stop_requested["reason"],"done")
        self.f.handoff=None
        a.heartbeat()
        self.assertIsNone(a.handoff_requested)
    def test_poll_yields_items_and_honors_retry_after_and_etag(self):
        body={"untrusted_content_notice":"n","claimable":[{"id":"t1","work":"claim"},{"id":"t2","work":"continue"}],"updates":[{"event_id":3,"ticket_id":"t9","type":"ticket.claimed"}],"verifications_waiting":0,"handoff_requested":None,"stop_requested":None,"next_cursor":"c1","retry_after_seconds":1}
        self.m.on("GET","/api/v1/work",(200,{"ETag":"W/\"e1\"","Retry-After":"1"},body),(304,{"ETag":"W/\"e1\"","Retry-After":"1"},None))
        a=self.make().start()
        threading.Timer(2.6,a.stop).start()
        items=[(i.kind,i.ticket_id) for i in a.poll()]
        self.assertEqual(items,[("claim","t1"),("continue","t2"),("update","t9")])
        calls=self.m.calls("GET","/api/v1/work")
        self.assertGreaterEqual(len(calls),2)
        self.assertEqual(calls[0].query,{})
        self.assertEqual(calls[1].query,{"cursor":"c1"})
        self.assertEqual(calls[1].headers["if-none-match"],"W/\"e1\"")
        self.assertGreaterEqual(calls[1].at-calls[0].at,0.95)
        self.assertEqual(a.cursor,"c1")
    def lease_routes(self,lease_seconds):
        tid="0b4f6c1e-1111-4222-8333-944445555666"
        def ticket(state):
            now=time.time()
            return {"untrusted_content_notice":"n","ticket":{"id":tid,"ref":"SB-1","state":state,"claim":{"started_at":iso(now),"expires_at":iso(now+lease_seconds)}}}
        self.m.on("POST",f"/api/v1/tickets/{tid}/claim",lambda r:(200,{},ticket("claimed")))
        self.m.on("POST",f"/api/v1/tickets/{tid}/claim/renew",lambda r:(200,{},ticket("claimed")))
        self.m.on("POST",f"/api/v1/tickets/{tid}/release",lambda r:(200,{},ticket("open")))
        self.m.on("POST",f"/api/v1/tickets/{tid}/submit",lambda r:(200,{},ticket("blocker_pending_verification")))
        self.m.on("POST",f"/api/v1/tickets/{tid}/updates",lambda r:(201,{},{"id":"u",**ticket("working")}))
        return tid
    def test_claim_lease_renews_at_a_third_of_the_lease(self):
        tid=self.lease_routes(3)
        a=self.make().start()
        with a.claim(tid) as lease:
            self.assertAlmostEqual(lease.renew_every,1.0,delta=0.05)
            time.sleep(3.5)
            lease.update("still going")
        renews=self.m.calls("POST",f"/api/v1/tickets/{tid}/claim/renew")
        self.assertEqual(len(renews),3)
        gaps=[b.at-a_.at for a_,b in zip(renews,renews[1:])]
        for g in gaps:
            self.assertAlmostEqual(g,1.0,delta=0.25)
        self.assertEqual(self.m.calls("POST",f"/api/v1/tickets/{tid}/release"),[])
        time.sleep(1.2)
        self.assertEqual(len(self.m.calls("POST",f"/api/v1/tickets/{tid}/claim/renew")),3)
    def test_claim_releases_on_error_but_not_after_submit(self):
        tid=self.lease_routes(1800)
        a=self.make().start()
        with self.assertRaises(RuntimeError):
            with a.claim(tid):
                raise RuntimeError("solver crashed")
        rel=self.m.calls("POST",f"/api/v1/tickets/{tid}/release")
        self.assertEqual(len(rel),1)
        self.assertIn("RuntimeError",rel[0].json["note"])
        with self.assertRaises(RuntimeError):
            with a.claim(tid) as lease:
                lease.submit("blocker")
                raise RuntimeError("after submit")
        self.assertEqual(len(self.m.calls("POST",f"/api/v1/tickets/{tid}/release")),1)
    def test_lease_stops_when_claim_is_lost(self):
        tid=self.lease_routes(1800)
        self.m.on("POST",f"/api/v1/tickets/{tid}/claim/renew",err(409,"not_claim_holder"))
        a=self.make().start()
        with a.claim(WorkItem("claim",{"id":tid}),renew_every=0.2) as lease:
            time.sleep(0.7)
            self.assertTrue(lease.lost)
            self.assertEqual(lease.error.code,"not_claim_holder")
            self.assertFalse(lease.held)
        self.assertEqual(len(self.m.calls("POST",f"/api/v1/tickets/{tid}/claim/renew")),1)
    def test_claim_lost_raises_before_the_block(self):
        tid=self.lease_routes(1800)
        self.m.on("POST",f"/api/v1/tickets/{tid}/claim",err(409,"already_claimed"))
        a=self.make().start()
        ran=[]
        with self.assertRaises(ClaimLost):
            with a.claim(tid):
                ran.append(1)
        self.assertEqual(ran,[])
        self.assertEqual(a._leases,set())
        self.assertEqual(self.m.calls("POST",f"/api/v1/tickets/{tid}/release"),[])
    def test_retire_on_stop_retires_and_deletes_state_file(self):
        self.m.on("POST","/api/v1/agents/me/retire",lambda r:(200,{},{"retired":True,"released":["SB-1"]}) if r.token in self.f.valid else err(401,"token_revoked"))
        a=self.make(state_file=self.state).start()
        a.stop()
        self.assertTrue(os.path.exists(self.state))
        self.assertEqual(self.m.calls("POST","/api/v1/agents/me/retire"),[])
        b=self.make(state_file=self.state,retire_on_stop=True).start()
        tok=b.token
        b.stop()
        calls=self.m.calls("POST","/api/v1/agents/me/retire")
        self.assertEqual([c.token for c in calls],[tok])
        self.assertFalse(os.path.exists(self.state))
        self.assertIsNone(b.agent)
        b.stop()
        self.assertEqual(len(self.m.calls("POST","/api/v1/agents/me/retire")),1)
    def test_failed_retire_keeps_state_file_and_reports(self):
        self.m.on("POST","/api/v1/agents/me/retire",err(503,"unavailable"))
        seen=[]
        a=self.make(state_file=self.state,retire_on_stop=True,on_error=seen.append).start()
        a.stop()
        self.assertTrue(os.path.exists(self.state))
        self.assertEqual([e.code for e in seen],["unavailable"])
    def test_stop_ends_background_thread(self):
        a=self.make(heartbeat_interval=0.1).start()
        for _ in range(150):
            if len(self.m.calls("POST","/api/v1/agents/me/heartbeat"))>=3:
                break
            time.sleep(0.02)
        a.stop()
        n=len(self.m.calls("POST","/api/v1/agents/me/heartbeat"))
        self.assertGreaterEqual(n,3)
        self.assertFalse(a._thread.is_alive())
        time.sleep(0.3)
        self.assertEqual(len(self.m.calls("POST","/api/v1/agents/me/heartbeat")),n)
if __name__=="__main__":
    unittest.main()
