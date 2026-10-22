import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from mock_server import Mock,err,iso
from stuckbot.runner import Config,Runner
from stuckbot.runner import adapters,sandbox
from stuckbot.runner.state import State
from stuckbot.verifier import fill
HAS_BWRAP=shutil.which("bwrap") is not None and subprocess.run(["bwrap","--unshare-all","--ro-bind","/","/","true"],capture_output=True).returncode==0
AGENT=r'''
import json,os,sys,time
mode=sys.argv[1]
print(json.dumps({"id":"m1","usage":{"input":100000,"output":20000}}),flush=True)
if mode=="spend":
    for i in range(100):
        print(json.dumps({"id":"m%d"%(i+2),"usage":{"input":200000,"output":20000}}),flush=True)
        time.sleep(0.05)
if mode=="sleep":
    time.sleep(300)
if mode=="crash":
    sys.exit(3)
if mode=="private":
    out=os.environ["OUT"]
    text=open(os.path.join(out,"prompt.md")).read()
    files=sorted(os.listdir(os.path.join(out,"ticket-files")))
    log=open(os.path.join(out,"ticket-files",files[0])).read()
    if "PRIVATE-CANARY" not in text or "LOG-CANARY" not in log:
        sys.exit(4)
    p=os.path.join(os.getcwd(),"calc.py")
    src=open(p).read()
    open(p,"w").write(src.replace("a - b","a + b"))
    json.dump({"outcome":"solved","summary":"saw the private details in "+files[0]},open(os.path.join(out,"result.json"),"w"))
if mode=="handoff":
    json.dump({"outcome":"handoff","summary":"needs a db person","handoff":{"title":"db migration stuck","objective":"migrate SECRET-PLAN","stuck_reason":"lock","completed":[],"remaining":["migrate"],"attempts":[],"next_steps":["ask dba"],"context":{"logs":["deadlock"],"workspace":{"repo":"github.com/acme/demo"}},"blocker_criteria":[{"description":"migrated","method":"manual","expected":"done"}],"mode":"blocker_only"}},open(os.path.join(os.environ["OUT"],"result.json"),"w"))
if mode in ("fix","spend"):
    p=os.path.join(os.getcwd(),"calc.py")
    src=open(p).read()
    open(p,"w").write(src.replace("a - b","a + b"))
out=os.environ["OUT"]
if mode in ("fix","lie","spend"):
    json.dump({"outcome":"solved","summary":"fixed add"},open(os.path.join(out,"result.json"),"w"))
if mode=="stuck":
    json.dump({"outcome":"stuck","summary":"tried","stuck_reason":"no idea"},open(os.path.join(out,"result.json"),"w"))
'''
class FakeStuckBot:
    """Just enough of the StuckBot API for the runner: atomic claims, leases, evidence, submit, release, escalate."""
    def __init__(self):
        self.m=Mock()
        self.m.answer=self.answer
        self.lock=threading.Lock()
        self.agents={}
        self.tickets={}
        self.n=0
        self.claims=0
        self.stop=set()
        self.comments=[]
        self.claim_delay=0.0
        self.node=""
    @property
    def url(self):
        return self.m.url
    def close(self):
        self.m.close()
    def post(self,repo="github.com/acme/demo",**kw):
        self.n+=1
        tid=str(uuid.uuid4())
        pkg={"title":"calc bug","objective":"tests pass","stuck_reason":"test_add fails","blocker_criteria":[{"key":"tests","description":"tests pass","method":"command","command":"python3 -m unittest -q test_calc","expected":"exit 0"}],"context":{"workspace":{"repo":repo}}}
        t={"id":tid,"ref":f"SB-{self.n}","state":"open","paused":False,"root_id":None,"depth":0,"urgency":5,"labels":[],"required_skills":[],"holder":None,"evidence":[],"updates":[],"package":pkg,"events":[]}
        t.update(kw)
        self.tickets[tid]=t
        return t
    def view(self,t,agent=None):
        return {"private_data":t.get("private_data",{"strict_privacy":False}),"id":t["id"],"ref":t["ref"],"state":t["state"],"paused":t["paused"],"root_id":t["root_id"],"depth":t["depth"],"urgency":t["urgency"],"labels":t["labels"],"required_skills":t["required_skills"],
                "claim":{"started_at":iso(time.time()),"expires_at":iso(time.time()+1800),"execution_target":t.get("target")} if t["holder"] else None,
                "you":{"holds_claim":bool(agent and t["holder"]==agent)},
                "criteria":[{"key":"tests","stage":"blocker","method":"command","evidence_type":"command_run"}],
                "content":{"title":t["package"]["title"],"package":t["package"]}}
    def agent_of(self,req):
        return self.agents.get(req.token)
    def answer(self,req):
        p=req.path
        if p=="/api/v1/agents/register":
            with self.lock:
                tok=f"sba_{len(self.agents)+1}"
                a={"id":str(uuid.uuid4()),"name":"fleet/"+((req.json or {}).get("name") or "agent")}
                self.agents[tok]=a
            return 201,{},{"token":tok,"agent":a,"expires_at":iso(time.time()+86400)}
        a=self.agent_of(req)
        if not a:
            return err(401,"invalid_token")
        if p=="/api/v1/agents/me":
            return 200,{},{"agent":a}
        if p=="/api/v1/agents/me/heartbeat":
            return 200,{},{"ok":True,"handoff_requested":None,"stop_requested":{"reason":"person"} if a["id"] in self.stop else None,"retry_after_seconds":60,"token_expires_soon":False}
        if p=="/api/v1/agents/me/retire":
            return 200,{},{"released":[],"waiting":[]}
        if p=="/api/v1/data-stores":
            return 200,{},{"data_stores":[{"name":"main","url":self.node}],"projects":[],"grant_keys":""}
        if p=="/api/v1/data-stores/main/upload-grants":
            return 200,{},{"store":"main","url":self.node+"/objects/","token":"upload-grant","expires_at":iso(time.time()+600),"max_bytes":1000000,"note":""}
        if p=="/api/v1/work":
            return 200,{},{"claimable":[dict(self.view(t),work="claim") for t in self.tickets.values() if t["state"]=="open"],"updates":[],"retry_after_seconds":1}
        m=re.match(r"^/api/v1/tickets/([^/]+)(/.*)?$",p)
        if not m or m.group(1) not in self.tickets:
            return err(404,"not_found")
        t=self.tickets[m.group(1)]
        rest=m.group(2) or ""
        t["events"].append((req.method,rest,a["id"]))
        if req.method=="GET" and rest=="":
            return 200,{},{"ticket":self.view(t,a["id"])}
        if rest=="/claim":
            time.sleep(self.claim_delay)
            with self.lock:
                if t["state"]!="open":
                    return err(409,"already_claimed")
                t["state"]="claimed"
                t["holder"]=a["id"]
                t["target"]=(req.json or {}).get("execution_target")
                self.claims+=1
            return 200,{},{"ticket":self.view(t,a["id"])}
        if rest=="/comments":
            self.comments.append((t["ref"],req.json["body"]))
            return 201,{},{"id":"c"}
        if t["holder"]!=a["id"]:
            return err(409,"not_claim_holder")
        if rest=="/data-grants":
            return 200,{},{"expires_at":iso(time.time()+300),"note":"","grants":[dict(g,token="grant-"+t["ref"]) for g in t.get("grants",[])]}
        if rest=="/handoff":
            t["state"]="handed_off"
            t["handoff"]=req.json
            return 201,{},{"ticket":self.view(t,a["id"]),"child":self.view(t,a["id"])}
        if rest=="/claim/renew":
            return 200,{},{"ticket":self.view(t,a["id"])}
        if rest=="/updates":
            t["updates"].append(req.json)
            return 201,{},{"id":"u","ticket":self.view(t,a["id"])}
        if rest=="/evidence":
            t["evidence"].append(req.json)
            return 201,{},{"evidence":{"id":"e"},"ticket":self.view(t,a["id"])}
        if rest=="/submit":
            t["state"]="blocker_pending_verification"
            return 200,{},{"ticket":self.view(t,a["id"])}
        if rest=="/release":
            t["state"]="open"
            t["holder"]=None
            t["release_note"]=(req.json or {}).get("note")
            return 200,{},{"ticket":self.view(t)}
        if rest=="/escalate":
            t["state"]="needs_human"
            t["escalation"]=req.json["reason"]
            return 200,{},{"ticket":self.view(t,a["id"])}
        return err(404,"not_found")
class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.mkdtemp(prefix="runner-test-")
        self.api=FakeStuckBot()
        repo=os.path.join(self.dir,"demo")
        os.makedirs(repo)
        open(os.path.join(repo,"calc.py"),"w").write("def add(a, b):\n    return a - b\n")
        open(os.path.join(repo,"test_calc.py"),"w").write("import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        env={"PATH":"/usr/bin:/bin","HOME":self.dir,"GIT_CONFIG_NOSYSTEM":"1"}
        for cmd in (["git","init","-q","-b","main"],["git","add","-A"],["git","-c","user.name=t","-c","user.email=t@t","commit","-qm","init"]):
            subprocess.run(cmd,cwd=repo,env=env,check=True)
        self.repo=repo
        self.agent=os.path.join(self.dir,"agent.py")
        open(self.agent,"w").write(AGENT)
        self.secret=os.path.join(self.dir,"enroll")
        open(self.secret,"w").write("sbe_test")
    def tearDown(self):
        self.api.close()
        shutil.rmtree(self.dir,ignore_errors=True)
    def config(self,mode="fix",state="state",**over):
        c={"base_url":self.api.url,"enrollment_secret_file":self.secret,"state_dir":os.path.join(self.dir,state),"name":"test","poll_seconds":1,
           "limits":{"max_concurrent":2,"max_runtime_seconds":30,"max_attempts_per_ticket":2,"hourly_usd":100,"daily_usd":200,"drain_seconds":2},
           "providers":[{"name":"fake","adapter":"command","program":"/usr/bin/python3","args":["-I",self.agent,mode],"max_usd_per_session":3,"price_per_mtok":{"input":3,"output":15},"env":{"OUT":"/out" if HAS_BWRAP else os.path.join(self.dir,state,"OUT")}}],
           "workspaces":{"demo":{"repo":self.repo,"match":["github.com/acme/demo"]}},
           "sandbox":{"kind":"bwrap","ro_binds":[self.agent]} if HAS_BWRAP else {"kind":"none","i_understand_there_is_no_isolation":True}}
        for k,v in over.items():
            tgt=c
            ks=k.split("__")
            for x in ks[:-1]:
                tgt=tgt[x]
            tgt[ks[-1]]=v
        return Config(c,self.dir)
    def run_once(self,cfg):
        r=Runner(cfg)
        r.run(once=True)
        r.state.close()
        return r
    def audit(self,cfg):
        s=State(os.path.join(cfg.state_dir,"runner.db"))
        kinds=[a["kind"] for a in s.log(500)]
        s.close()
        return kinds
    def branch(self,name):
        return subprocess.run(["git","rev-parse","--verify","--quiet","refs/heads/"+name],cwd=self.repo,capture_output=True,text=True).returncode==0
class FakeNode:
    """A data store that serves and stores objects for any grant, and records what it saw."""
    def __init__(self):
        self.m=Mock()
        self.m.answer=self.answer
        self.objects={}
        self.seen=[]
    def answer(self,req):
        self.seen.append((req.method,req.path,req.token))
        o=req.path.rsplit("/",1)[-1]
        if req.method=="PUT":
            if "sha256:"+hashlib.sha256(req.body).hexdigest()!=o:
                return err(400,"digest_mismatch")
            self.objects[o]=req.body
            return 201,{},{"object":o,"size":len(req.body),"created":True}
        if o not in self.objects:
            return err(404,"not_found")
        return 200,{"Content-Type":"application/octet-stream"},self.objects[o]
    def put(self,data):
        o="sha256:"+hashlib.sha256(data).hexdigest()
        self.objects[o]=data
        return o
@unittest.skipUnless(HAS_BWRAP,"needs bubblewrap")
class RunnerFlowTest(RunnerTest):
    def private_ticket(self,node_url,**kw):
        node=FakeNode()
        self.addCleanup(node.m.close)
        body=json.dumps({"objective":"tests pass","stuck_reason":"PRIVATE-CANARY add() subtracts"}).encode()
        b=node.put(body)
        f=node.put(b"LOG-CANARY full log\n")
        url=node_url or node.m.url
        t=self.api.post(**kw)
        t["package"]={"title":"calc bug","mode":"blocker_only","blocker_criteria":t["package"]["blocker_criteria"],"context":{"workspace":{"repo":"github.com/acme/demo"}},"data_refs":[{"store":"main","object":b,"kind":"body"},{"store":"main","object":f,"kind":"file","name":"../build log.txt"}]}
        t["grants"]=[{"store":"main","object":b,"kind":"body","available":True,"url":url+"/objects/"+b},{"store":"main","object":f,"kind":"file","name":"../build log.txt","available":True,"url":url+"/objects/"+f}]
        return t,node
    def test_private_details_are_fetched_after_the_claim_and_given_to_the_session(self):
        t,node=self.private_ticket(None)
        cfg=self.config("private")
        self.run_once(cfg)
        self.assertEqual(t["state"],"blocker_pending_verification",t.get("release_note"))
        self.assertIn("saw the private details in 02-build_log.txt",t["updates"][0]["body"])
        rests=[r for _,r,_ in t["events"]]
        self.assertLess(rests.index("/claim"),rests.index("/data-grants"))
        self.assertTrue(all(tok=="grant-SB-1" for _,_,tok in node.seen))
        kinds=self.audit(cfg)
        self.assertLess(kinds.index("private_data_fetched"),kinds.index("session_started"))
    def test_an_unreachable_data_store_gives_the_ticket_back_without_a_session(self):
        port=socket.socket()
        port.bind(("127.0.0.1",0))
        dead="http://127.0.0.1:%d"%port.getsockname()[1]
        port.close()
        t,_=self.private_ticket(dead)
        cfg=self.config("fix")
        self.run_once(cfg)
        self.assertEqual(t["state"],"open")
        self.assertIn("could not fetch this ticket's details from its data store",t["release_note"])
        kinds=self.audit(cfg)
        self.assertIn("private_data_failed",kinds)
        self.assertNotIn("session_started",kinds)
    def test_a_strict_handoff_keeps_its_details_on_the_data_store(self):
        t,node=self.private_ticket(None)
        self.api.node=node.m.url
        t["private_data"]={"strict_privacy":True}
        cfg=self.config("handoff")
        self.run_once(cfg)
        self.assertIn("handoff",t,t.get("release_note"))
        h=t["handoff"]["package"]
        self.assertNotIn("objective",h)
        self.assertEqual(h["context"],{"workspace":{"repo":"github.com/acme/demo"}})
        ref=h["data_refs"][0]
        self.assertEqual((ref["kind"],ref["store"]),("body","main"))
        sealed=json.loads(node.objects[ref["object"]])
        self.assertEqual(sealed["objective"],"migrate SECRET-PLAN")
        self.assertEqual(sealed["context"],{"logs":["deadlock"]})
        self.assertNotIn("SECRET-PLAN",json.dumps(t["handoff"]))
    def test_claims_before_spawning_and_submits_runner_checked_evidence(self):
        t=self.api.post()
        cfg=self.config("fix")
        self.run_once(cfg)
        kinds=self.audit(cfg)
        self.assertLess(kinds.index("claimed"),kinds.index("session_started"))
        self.assertEqual(t["state"],"blocker_pending_verification")
        self.assertEqual(t["target"],"runner")
        ev=t["evidence"][0]
        self.assertEqual((ev["criterion"],ev["type"],ev["payload"]["exit_code"],ev["payload"]["cwd"]),("tests","command_run",0,"/work"))
        self.assertTrue(self.branch("stuckbot/SB-1"))
        diff=subprocess.run(["git","diff","--stat","main","stuckbot/SB-1"],cwd=self.repo,capture_output=True,text=True).stdout
        self.assertIn("calc.py",diff)
        self.assertNotIn("pycache",diff)
        self.assertIn("stuckbot/SB-1",t["updates"][0]["body"])
    def test_a_lost_claim_race_never_starts_a_session(self):
        t=self.api.post()
        self.api.claim_delay=0.3
        cfgs=[self.config("fix",state=f"s{i}") for i in range(3)]
        threads=[threading.Thread(target=self.run_once,args=(c,)) for c in cfgs]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        started=[c for c in cfgs if "session_started" in self.audit(c)]
        lost=[c for c in cfgs if "claim_lost" in self.audit(c)]
        self.assertEqual((len(started),len(lost),self.api.claims),(1,2,1))
        for c in lost:
            self.assertEqual(os.listdir(os.path.join(c.state_dir,"runs")),[])
    def test_the_runner_reruns_checks_so_a_false_success_is_not_submitted(self):
        t=self.api.post()
        cfg=self.config("lie")
        self.run_once(cfg)
        self.assertEqual(t["state"],"needs_human")
        self.assertIn("exited 1, expected 0",t["escalation"])
        self.assertEqual(t["evidence"],[])
        self.assertEqual(self.audit(cfg).count("session_started"),2)
    def test_spending_limit_stops_the_session_outside_the_model(self):
        t=self.api.post()
        cfg=self.config("spend",limits__max_attempts_per_ticket=1)
        start=time.monotonic()
        self.run_once(cfg)
        self.assertLess(time.monotonic()-start,20)
        self.assertIn("spending limit",t["escalation"])
        s=State(os.path.join(cfg.state_dir,"runner.db"))
        run=s.recent(1)[0]
        self.assertLess(run["spent_usd"],3+2*0.9+0.01)
        self.assertGreater(run["spent_usd"],3)
        s.close()
    def test_runtime_limit(self):
        t=self.api.post()
        cfg=self.config("sleep",limits__max_attempts_per_ticket=1,limits__max_runtime_seconds=2)
        start=time.monotonic()
        self.run_once(cfg)
        self.assertLess(time.monotonic()-start,25)
        self.assertIn("ran out of time",t["escalation"])
    def test_hourly_limit_is_checked_before_claiming(self):
        t=self.api.post()
        cfg=self.config("fix",limits__hourly_usd=2)
        self.run_once(cfg)
        self.assertEqual((t["state"],self.api.claims),("open",0))
        self.assertIn("skipped",self.audit(cfg))
    def test_reserved_and_spent_money_count_toward_the_daily_limit(self):
        cfg=self.config("fix",limits__daily_usd=5)
        r=Runner(cfg)
        self.assertIsNone(r.budget_problem("fake"))
        run=r.state.new_run({"id":"x","ref":"SB-9"},"fake",None,3.0)
        self.assertIn("daily limit",r.budget_problem("fake"))
        r.state.update(run["id"],state="done")
        r.state.settle(run["id"],1.5)
        self.assertIsNone(r.budget_problem("fake"))
        r.state.settle(run["id"],2.5)
        self.assertIn("daily limit",r.budget_problem("fake"))
    def test_crashing_provider_opens_the_circuit_breaker(self):
        t1=self.api.post()
        cfg=self.config("crash",limits__breaker_failures=2)
        self.run_once(cfg)
        self.assertEqual(t1["state"],"needs_human")
        t2=self.api.post()
        self.run_once(cfg)
        self.assertEqual(t2["state"],"open")
        s=State(os.path.join(cfg.state_dir,"runner.db"))
        self.assertGreater(s.breaker("fake")["open_until"],time.time()+800)
        s.close()
    def test_unauthorized_workspace_and_deep_handoffs_are_skipped(self):
        a=self.api.post(repo="github.com/acme/secret")
        b=self.api.post(depth=5,root_id=str(uuid.uuid4()))
        cfg=self.config("fix")
        self.run_once(cfg)
        self.assertEqual((a["state"],b["state"],self.api.claims),("open","open",0))
    def test_approval_gate(self):
        t=self.api.post(labels=["production"])
        cfg=self.config("fix",approval={"labels":["production"]})
        self.run_once(cfg)
        self.assertEqual((t["state"],self.api.claims),("open",0))
        self.assertIn("stuckbot-runner approve SB-1",self.api.comments[0][1])
        s=State(os.path.join(cfg.state_dir,"runner.db"))
        self.assertEqual(s.approve("SB-1","tester"),1)
        s.close()
        self.run_once(cfg)
        self.assertEqual(t["state"],"blocker_pending_verification")
    def test_recovery_after_the_runner_is_killed(self):
        t=self.api.post()
        cfg=self.config("sleep",limits__max_runtime_seconds=120)
        code=f"import sys;sys.path[:0]={sys.path!r};from stuckbot.runner import Config,Runner;import json;Runner(Config(json.loads({json.dumps(cfg.raw)!r}),{self.dir!r})).run()"
        p=subprocess.Popen([sys.executable,"-c",code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(100):
            if t["state"]=="claimed" and "session_started" in self.audit(cfg):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        p.kill()
        p.wait()
        time.sleep(1)
        self.assertEqual(subprocess.run(["pgrep","-f",self.agent+" sleep"],capture_output=True).stdout,b"")
        self.assertEqual(t["state"],"claimed",self.audit(cfg))
        cfg2=self.config("fix")
        self.run_once(cfg2)
        kinds=self.audit(cfg2)
        self.assertIn("recovered",kinds)
        self.assertIn("runner restarted",t["release_note"] or "")
        self.assertEqual(t["state"],"blocker_pending_verification")
    def test_two_runners_cannot_share_a_state_folder(self):
        cfg=self.config("fix")
        r=Runner(cfg)
        with self.assertRaises(RuntimeError):
            Runner(cfg)
        r.state.close()
    def test_graceful_shutdown_gives_the_ticket_back(self):
        t=self.api.post()
        cfg=self.config("sleep",limits__max_runtime_seconds=120,limits__drain_seconds=1)
        r=Runner(cfg)
        th=threading.Thread(target=r.run)
        th.start()
        for _ in range(100):
            if "session_started" in self.audit(cfg):
                break
            time.sleep(0.1)
        r.stop()
        th.join(60)
        self.assertFalse(th.is_alive())
        self.assertEqual(t["state"],"open")
        self.assertIn("shutting down",t["release_note"])
        r.state.close()
    def test_stop_requested_by_stuckbot_stops_the_session(self):
        t=self.api.post()
        cfg=self.config("sleep",limits__max_runtime_seconds=120,limits__max_attempts_per_ticket=1)
        r=Runner(cfg)
        th=threading.Thread(target=r.run,kwargs={"once":True})
        th.start()
        for _ in range(100):
            if t["holder"] and r.sessions and all(x.agent for x in r.sessions.values()):
                break
            time.sleep(0.1)
        self.api.stop.add(t["holder"])
        for s in list(r.sessions.values()):
            s.agent.heartbeat()
        th.join(60)
        self.assertIn("asked it to stop",t.get("escalation") or "")
        r.state.close()
@unittest.skipUnless(HAS_BWRAP,"needs bubblewrap")
class SandboxTest(unittest.TestCase):
    """What a session can and cannot do inside the bwrap sandbox."""
    def setUp(self):
        self.dir=tempfile.mkdtemp(prefix="sandbox-test-")
        for d in ("work","out","home","sock"):
            os.makedirs(os.path.join(self.dir,d))
        self.secret=os.path.join(self.dir,"secret.txt")
        open(self.secret,"w").write("top secret")
        self.log=[]
        self.upstream=socket.socket()
        self.upstream.bind(("127.0.0.1",0))
        self.upstream.listen(4)
        port=self.upstream.getsockname()[1]
        threading.Thread(target=self.serve_upstream,daemon=True).start()
        self.proxy=sandbox.EgressProxy(os.path.join(self.dir,"sock","egress.sock"),["allowed.test"],lambda k,**kw:self.log.append((k,kw)),ports=(port,),allow_private=True,resolver=lambda h,p:["127.0.0.1"])
        self.port=port
    def serve_upstream(self):
        while True:
            try:
                c,_=self.upstream.accept()
            except OSError:
                return
            c.sendall(b"hello from upstream")
            c.close()
    def tearDown(self):
        self.upstream.close()
        self.proxy.close()
        shutil.rmtree(self.dir,ignore_errors=True)
    def run_in(self,code,env=None):
        box=sandbox.Bwrap({})
        e=box.env({"PATH":"/usr/bin:/bin",**(env or {})},self.dir)
        cmd=box.wrap(["/usr/bin/python3","-I","-c",code],e,self.dir,[])
        p=subprocess.run(cmd,env=e,capture_output=True,text=True,timeout=60)
        return p.stdout.strip()
    def test_no_direct_network(self):
        out=self.run_in("import socket\ntry:\n socket.create_connection(('1.1.1.1',443),3);print('connected')\nexcept OSError as e:print('blocked',e.errno)")
        self.assertTrue(out.startswith("blocked"),out)
    def test_only_listed_hosts_through_the_proxy(self):
        code="import socket,time\nfor h in ('denied.test','allowed.test'):\n s=socket.create_connection(('127.0.0.1',3128),3);s.sendall(('CONNECT %s:"+str(self.port)+" HTTP/1.1\\r\\n\\r\\n'%h).encode());time.sleep(0.5);print(h,s.recv(200).decode().replace('\\r\\n','|'))"
        out=self.run_in(code)
        self.assertIn("denied.test HTTP/1.1 403",out)
        self.assertIn("allowed.test HTTP/1.1 200 Connection established||hello from upstream",out)
        self.assertIn("egress_denied",[k for k,_ in self.log])
    def test_cannot_read_host_files_or_see_host_processes(self):
        out=self.run_in(f"import os\nprint(os.path.exists({self.secret!r}),os.listdir('/home')!=['agent'],os.path.exists('/root'),os.path.exists('/etc/shadow'),len([p for p in os.listdir('/proc') if p.isdigit()])<10)")
        self.assertEqual(out,"False False False False True")
    def test_cannot_write_outside_its_folders(self):
        out=self.run_in("import os\nr=[]\nfor p in ('/usr/x','/etc/x','/x'):\n try:\n  open(p,'w');r.append('wrote '+p)\n except OSError:r.append('no')\nopen('/work/ok','w').write('1');print(' '.join(r))")
        self.assertEqual(out,"no no no")
        self.assertTrue(os.path.exists(os.path.join(self.dir,"work","ok")))
        self.assertFalse(os.path.exists("/x"))
    def test_environment_holds_only_what_the_runner_gives(self):
        os.environ["RUNNER_TEST_SECRET"]="leak"
        try:
            out=self.run_in("import os,json;print(json.dumps(sorted(os.environ)))",env={"GIVEN":"1"})
        finally:
            del os.environ["RUNNER_TEST_SECRET"]
        keys=json.loads(out)
        self.assertIn("GIVEN",keys)
        self.assertNotIn("RUNNER_TEST_SECRET",keys)
    def test_credentials_are_not_on_the_command_line(self):
        box=sandbox.Bwrap({})
        cmd=box.wrap(["true"],{"ANTHROPIC_API_KEY":"sk-ant-secret"},self.dir,[])
        self.assertNotIn("sk-ant-secret"," ".join(cmd))
class ProxyRulesTest(unittest.TestCase):
    def test_host_rules(self):
        allow=["api.anthropic.com","*.example.org"]
        self.assertTrue(sandbox.host_allowed("API.anthropic.com.",allow))
        self.assertTrue(sandbox.host_allowed("a.b.example.org",allow))
        self.assertFalse(sandbox.host_allowed("example.org",allow))
        self.assertFalse(sandbox.host_allowed("api.anthropic.com.evil.test",allow))
        self.assertFalse(sandbox.host_allowed("evilapi.anthropic.com",allow))
    def test_private_addresses_and_other_ports_are_refused(self):
        d=tempfile.mkdtemp()
        try:
            for ip in ("10.0.0.5","127.0.0.1","169.254.169.254","100.100.1.1","::1","::ffff:10.0.0.1","fd00::1"):
                p=sandbox.EgressProxy(os.path.join(d,"s"),["h.test"],lambda *a,**k:None,resolver=lambda h,port,ip=ip:[ip])
                self.assertIsNone(p.decide("h.test",443),ip)
                p.close()
            p=sandbox.EgressProxy(os.path.join(d,"s"),["h.test"],lambda *a,**k:None,resolver=lambda h,port:["93.184.215.14"])
            self.assertEqual(p.decide("h.test",443),"93.184.215.14")
            self.assertIsNone(p.decide("h.test",22))
            p.close()
        finally:
            shutil.rmtree(d)
class AdapterTest(unittest.TestCase):
    def test_claude_counts_final_output_tokens_once_per_response(self):
        a=adapters.make({"adapter":"claude-code","price_per_mtok":{"input":2,"output":10,"cache_write":4,"cache_read":0.2}})
        u=adapters.Usage()
        lines=[
            {"type":"stream_event","event":{"type":"message_start","message":{"id":"m1","usage":{"input_tokens":2,"cache_creation_input_tokens":1000,"cache_read_input_tokens":5000,"output_tokens":5}}}},
            {"type":"stream_event","event":{"type":"content_block_delta","delta":{"text":"hi"}}},
            {"type":"assistant","message":{"id":"m1","usage":{"input_tokens":2,"cache_creation_input_tokens":1000,"cache_read_input_tokens":5000,"output_tokens":5}}},
            {"type":"stream_event","event":{"type":"message_delta","usage":{"input_tokens":2,"cache_creation_input_tokens":1000,"cache_read_input_tokens":5000,"output_tokens":400}}},
            {"type":"assistant","message":{"id":"m1","usage":{"input_tokens":2,"cache_creation_input_tokens":1000,"cache_read_input_tokens":5000,"output_tokens":5}}},
            {"type":"stream_event","event":{"type":"message_start","message":{"id":"m2","usage":{"input_tokens":3,"cache_read_input_tokens":6000,"output_tokens":1}}}},
            {"type":"stream_event","event":{"type":"message_delta","usage":{"input_tokens":3,"cache_read_input_tokens":6000,"output_tokens":50}}},
            {"type":"result","total_cost_usd":0.0123,"is_error":False,"result":"done"},
        ]
        for l in lines:
            a.feed(json.dumps(l),u)
        self.assertEqual((u.tokens_in,u.tokens_out),(12005,450))
        self.assertAlmostEqual(u.cost,(5*2+450*10+1000*4+11000*0.2)/1e6)
        self.assertEqual((u.reported_cost,u.done,u.final_text),(0.0123,True,"done"))
        self.assertFalse(a.keep(json.dumps(lines[1])))
    def test_codex_turn_usage(self):
        a=adapters.make({"adapter":"codex","price_per_mtok":{"input":1,"output":8,"cache_read":0.1}})
        u=adapters.Usage()
        a.feed(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"ok"}}),u)
        a.feed(json.dumps({"type":"turn.completed","usage":{"input_tokens":14144,"cached_input_tokens":12288,"output_tokens":5,"reasoning_output_tokens":10}}),u)
        self.assertEqual((u.tokens_in,u.tokens_out,u.final_text),(14144,15,"ok"))
        self.assertAlmostEqual(u.cost,(1856*1+12288*0.1+15*8)/1e6)
    def test_claude_command_line(self):
        a=adapters.make({"adapter":"claude-code","program":sys.executable,"model":"claude-sonnet-5-5","price_per_mtok":{"input":1}})
        argv=a.command("x",{"work":"/work","out":"/out","home":"/home/agent"},True,1.5)
        self.assertIn("--max-budget-usd",argv)
        self.assertEqual(argv[argv.index("--max-budget-usd")+1],"1.50")
        self.assertIn("Task,Agent,WebFetch,WebSearch",argv)
        self.assertEqual(argv[argv.index("--permission-mode")+1],"bypassPermissions")
        argv=a.command("x",{"work":"/w","out":"/o","home":"/h"},False,1.5)
        self.assertEqual(argv[argv.index("--permission-mode")+1],"dontAsk")
        self.assertNotIn("Bash",argv[argv.index("--tools")+1])
class ConfigTest(unittest.TestCase):
    def base(self,**kw):
        c={"providers":[{"name":"c","adapter":"claude-code","max_usd_per_session":1,"price_per_mtok":{"input":1}}],"state_dir":"/tmp/x"}
        c.update(kw)
        return c
    def test_refuses_unsafe_or_unmeasurable_setups(self):
        for bad,needle in (({"providers":[]},"providers is empty"),
                           ({"providers":[{"name":"c","adapter":"claude-code","max_usd_per_session":1}]},"price_per_mtok"),
                           ({"providers":[{"name":"c","adapter":"claude-code","price_per_mtok":{"input":1}}]},"max_usd_per_session"),
                           ({"sandbox":{"kind":"none"}},"full access"),
                           ({"limits":{"hourly_usd":0}},"hourly_usd"),
                           ({"default_workspace":"nope"},"default_workspace")):
            with self.assertRaises(ValueError) as e:
                Config(self.base(**bad))
            self.assertIn(needle,str(e.exception))
    def test_workspace_matching(self):
        d=tempfile.mkdtemp()
        try:
            cfg=Config(self.base(workspaces={"demo":{"repo":d,"match":["git@github.com:acme/demo.git"]}}))
            t=lambda repo:{"content":{"package":{"context":{"workspace":{"repo":repo}}}}}
            self.assertEqual(cfg.workspace_for(t("https://github.com/acme/demo"))["name"],"demo")
            self.assertEqual(cfg.workspace_for(t("demo"))["name"],"demo")
            self.assertIsNone(cfg.workspace_for(t("github.com/acme/other")))
            self.assertIsNone(cfg.workspace_for({"content":{"package":{}}}))
        finally:
            shutil.rmtree(d)
class VerifierFillTest(unittest.TestCase):
    def test_only_well_formed_ticket_values_are_filled_in(self):
        good={"ref":"SB-12","id":"6b358737-4b07-4a8f-b954-3536dff84eb9"}
        self.assertEqual(fill(["git","fetch","origin","stuckbot/{ticket_ref}"],good),["git","fetch","origin","stuckbot/SB-12"])
        for bad in ({"ref":"SB-1; rm -rf /","id":good["id"]},{"ref":"SB-1","id":"x"},{"ref":"--upload-pack=x","id":good["id"]}):
            self.assertIsNone(fill(["{ticket_ref}"],bad))
if __name__=="__main__":
    unittest.main()
