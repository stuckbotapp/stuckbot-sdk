"""stuckbot-verify: a verifier you run on your own machine.

It takes StuckBot verification jobs, runs each criterion's command itself, and reports the
results signed with your key, so StuckBot shows them as "Signed by your verifier".

Safety rules, in order of importance:
- It only runs commands listed word for word in your config ("allow"). Ticket text never adds or
  changes a command, and jobs whose commands are not listed are left for someone else.
- Commands run without a shell, in your configured folder, with only the environment you list,
  under a time limit.
- Only a hash and a short excerpt of the output leave your machine.
- "prepare" commands may contain {ticket_ref} and {ticket_id}, for example to check out the branch a
  StuckBot Runner delivered (stuckbot/{ticket_ref}). They are filled in only with values that match
  SB-<number> and a UUID, so ticket text cannot change what runs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any,Dict,List,Optional,Tuple
from .attest import SigningKey,VerifyingKey,attestation,check_attestation
from .client import Client
from .errors import StuckBotError
log=logging.getLogger("stuckbot.verify")
EXCERPT=2000
_REF=re.compile(r"^SB-[0-9]{1,12}$")
_UUID=re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_BRANCH=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SHA=re.compile(r"^[0-9a-f]{40}$")
def ticket_candidate(ticket:Dict[str,Any],job:Optional[Dict[str,Any]]=None)->Dict[str,str]:
    cand=dict((job or {}).get("candidate") or ticket.get("candidate") or {})
    commit=str(cand.get("commit") or (job or {}).get("candidate_commit_sha") or ticket.get("candidate_commit_sha") or "")
    tree=str(cand.get("tree") or (job or {}).get("candidate_tree_sha") or ticket.get("candidate_tree_sha") or "")
    branch=str(cand.get("branch") or "")
    for u in reversed(ticket.get("updates") or []):
        prog=u.get("progress") if isinstance(u,dict) else None
        if isinstance(prog,dict):
            if not commit and prog.get("commit"):
                commit=str(prog["commit"])
            if not tree and prog.get("tree"):
                tree=str(prog["tree"])
            if not branch and prog.get("branch"):
                branch=str(prog["branch"])
    ws=(((ticket.get("content") or {}).get("package") or {}).get("context") or {}).get("workspace") or {}
    if not commit and ws.get("commit"):
        commit=str(ws["commit"])
    if not branch and ws.get("branch"):
        branch=str(ws["branch"])
    ref=str(ticket.get("ref") or (job or {}).get("ticket_ref") or "")
    if not branch and _REF.match(ref):
        branch=f"stuckbot/{ref}"
    return {"branch":branch,"commit":commit,"tree":tree}
def criteria_hash(ticket:Dict[str,Any],stage:str,job:Optional[Dict[str,Any]]=None)->str:
    if job and job.get("criteria_sha256"):
        return str(job["criteria_sha256"])
    if ticket.get("criteria_sha256"):
        return str(ticket["criteria_sha256"])
    pkg=(ticket.get("content") or {}).get("package") or {}
    spec=pkg.get(stage+"_criteria") or []
    norm=[{"key":c.get("key") or f"{stage}_{i+1}","method":c.get("method","command"),"command":c.get("command",""),"expected":c.get("expected",""),"expected_exit_code":int(c.get("expected_exit_code",0)),"pattern":c.get("pattern",""),"url":c.get("url","")} for i,c in enumerate(spec) if isinstance(c,dict)]
    return hashlib.sha256(json.dumps(norm,separators=(",",":"),sort_keys=True).encode()).hexdigest()
def fill(argv:List[str],ticket:Dict[str,Any],job:Optional[Dict[str,Any]]=None)->Optional[List[str]]:
    """argv with {ticket_ref}, {ticket_id}, {candidate_branch} and {candidate_commit} filled in, or None if invalid."""
    ref=str(ticket.get("ref") or (job or {}).get("ticket_ref") or "")
    tid=str(ticket.get("id") or (job or {}).get("ticket_id") or "")
    if not _REF.match(ref) or not _UUID.match(tid):
        return None
    cand=ticket_candidate(ticket,job)
    branch=cand["branch"]
    commit=cand["commit"]
    need_branch=any("{candidate_branch}" in a for a in argv)
    need_commit=any("{candidate_commit}" in a for a in argv)
    if need_branch and (not _BRANCH.match(branch) or ".." in branch):
        return None
    if need_commit and not _SHA.match(commit):
        return None
    out=[]
    for a in argv:
        r=a.replace("{ticket_ref}",ref).replace("{ticket_id}",tid).replace("{candidate_branch}",branch).replace("{candidate_commit}",commit)
        if a.startswith("{") and r.startswith("-"):
            return None
        out.append(r)
    return out
class Config:
    def __init__(self,data:Dict[str,Any],base:str="."):
        self.key_file=os.path.expanduser(str(data.get("key_file","~/.config/stuckbot/verifier-key.pem")))
        self.workdir=os.path.abspath(os.path.join(base,os.path.expanduser(str(data.get("workdir",".")))))
        self.timeout=float(data.get("timeout_seconds",600))
        self.poll=float(data.get("poll_seconds",30))
        self.env={str(k):str(v) for k,v in dict(data.get("env",{"PATH":"/usr/local/bin:/usr/bin:/bin"})).items()}
        self.allow=[_argv(c) for c in data.get("allow",[])]
        self.allow_urls=[str(u) for u in data.get("allow_urls",[])]
        self.prepare=[_argv(c) for c in data.get("prepare",[])]
        self.max_output=int(data.get("max_output_bytes",10*1024*1024))
        self.sandbox=data.get("sandbox")
        if self.sandbox not in (None,"bwrap"):
            raise ValueError("sandbox must be bwrap or left out")
        if not self.allow and not self.allow_urls:
            raise ValueError("the config has no allow list, so the verifier would never run anything")
    @classmethod
    def load(cls,path:str)->"Config":
        with open(path) as f:
            return cls(json.load(f),os.path.dirname(os.path.abspath(path)))
def _argv(cmd:Any)->List[str]:
    out=list(cmd) if isinstance(cmd,list) else shlex.split(str(cmd))
    if not out or not all(isinstance(x,str) for x in out):
        raise ValueError("bad command in config: %r"%(cmd,))
    return out
def workdir_git_head(workdir:str)->Tuple[str,str]:
    env={"PATH":"/usr/local/bin:/usr/bin:/bin","HOME":"/nonexistent","GIT_CONFIG_NOSYSTEM":"1","GIT_TERMINAL_PROMPT":"0"}
    c=subprocess.run(["git","-c","core.hooksPath=/dev/null","rev-parse","--verify","--quiet","HEAD"],cwd=workdir,env=env,capture_output=True,text=True,timeout=10)
    if c.returncode!=0:
        return "",""
    t=subprocess.run(["git","-c","core.hooksPath=/dev/null","rev-parse","--verify","--quiet","HEAD^{tree}"],cwd=workdir,env=env,capture_output=True,text=True,timeout=10)
    return c.stdout.strip(),(t.stdout.strip() if t.returncode==0 else "")
def run_command(argv:List[str],cfg:Config,isolate:bool=False)->Dict[str,Any]:
    """Run one command without a shell, streaming and capping output at cfg.max_output bytes."""
    started=time.time()
    tmp=None
    cmd=argv
    env={**cfg.env,"GIT_CONFIG_COUNT":"1","GIT_CONFIG_KEY_0":"core.hooksPath","GIT_CONFIG_VALUE_0":"/dev/null"}
    if isolate and cfg.sandbox=="bwrap":
        from .runner.sandbox import Bwrap
        import tempfile
        tmp=tempfile.mkdtemp(prefix="stuckbot-verify-")
        box=Bwrap({})
        env=box.env({**env,"HOME":"/home/agent"},tmp,proxy=False)
        cmd=box.wrap(argv,env,tmp,[],work=cfg.workdir)
    chunks:List[bytes]=[]
    kept=0
    code=0
    note=""
    try:
        p=subprocess.Popen(cmd,cwd=cfg.workdir,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,shell=False,start_new_session=True)
        def _drain():
            nonlocal kept
            try:
                while True:
                    d=p.stdout.read(65536)
                    if not d:
                        break
                    if kept<cfg.max_output:
                        take=d[:cfg.max_output-kept]
                        chunks.append(take)
                        kept+=len(take)
            finally:
                try:
                    p.stdout.close()
                except OSError:
                    pass
        th=threading.Thread(target=_drain,daemon=True)
        th.start()
        try:
            code=p.wait(timeout=cfg.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid,signal.SIGKILL)
            except OSError:
                p.kill()
            p.wait()
            code=124
            note="stopped after %d seconds\n"%cfg.timeout
        th.join(timeout=5)
    except OSError as e:
        code=127
        note="could not start: %s\n"%e.strerror
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp,ignore_errors=True)
    out=b"".join(chunks)
    text=out.decode("utf-8","replace")
    return {
        "exit_code":code,
        "output_sha256":hashlib.sha256(out).hexdigest(),
        "output_excerpt":(note+text[-EXCERPT:]) if (note or text) else "",
        "ran_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(started)),
        "cwd":cfg.workdir,
    }
def _safe_workdir_file(workdir:str,rel:str)->Optional[str]:
    if not rel or os.path.isabs(rel):
        return None
    full=os.path.realpath(os.path.join(workdir,rel))
    root=os.path.realpath(workdir)
    if full!=root and not full.startswith(root+os.sep):
        return None
    if os.path.islink(os.path.join(workdir,rel)) or not os.path.isfile(full):
        return None
    return full
def plan(ticket:Dict[str,Any],stage:str,cfg:Config)->Optional[List[Dict[str,Any]]]:
    """The criteria this verifier can run for this stage, or None if a command is disallowed or nothing can be checked."""
    pkg=(ticket.get("content") or {}).get("package") or {}
    out=[]
    for c in pkg.get(stage+"_criteria") or []:
        method=c.get("method")
        if method=="manual":
            continue
        if method in ("command","test_suite"):
            if not c.get("command"):
                return None
            argv=_argv(c["command"])
            if argv not in cfg.allow:
                return None
            out.append({"key":c["key"],"method":method,"command":c["command"],"argv":argv,"expected_exit_code":int(c.get("expected_exit_code",0))})
        elif method in ("log_match","log_check"):
            pat=str(c.get("pattern") or c.get("expected") or "")
            if not pat:
                return None
            cmd=c.get("command")
            argv=None
            if cmd:
                argv=_argv(cmd)
                if argv not in cfg.allow:
                    return None
            lfile=str(c.get("log_file") or c.get("file") or "")
            if not argv and not lfile:
                return None
            out.append({"key":c["key"],"method":"log_match","pattern":pat,"log_file":lfile,"command":cmd,"argv":argv})
        elif method=="external_check":
            url=str(c.get("url") or "")
            if url and url in cfg.allow_urls:
                out.append({"key":c["key"],"method":"external_check","url":url,"expected":str(c.get("expected") or "200")})
            continue
        else:
            return None
    return out or None
class Verifier:
    def __init__(self,client:Client,key:SigningKey,cfg:Config):
        self.client=client
        self.key=key
        self.cfg=cfg
    def _run_criterion(self,c:Dict[str,Any])->Tuple[str,Dict[str,Any],Optional[str]]:
        m=c["method"]
        if m in ("command","test_suite"):
            r=run_command(c["argv"],self.cfg,isolate=True)
            ok=r["exit_code"]==c["expected_exit_code"]
            if m=="command":
                payload={"command":c["command"],"exit_code":r["exit_code"],"output_sha256":r["output_sha256"],"ran_at":r["ran_at"],"cwd":r["cwd"]}
                if r["output_excerpt"]:
                    payload["output_excerpt"]=r["output_excerpt"]
                return "command_run",payload,(None if ok else "%s exited %s, expected %s"%(c["key"],r["exit_code"],c["expected_exit_code"]))
            summary=("exit code %d\n"%r["exit_code"])+(r["output_excerpt"][-5000:] if r["output_excerpt"] else "")
            payload={"runner":c["command"],"passed":1 if ok else 0,"failed":0 if ok else 1,"exit_code":r["exit_code"],"output_sha256":r["output_sha256"],"ran_at":r["ran_at"],"cwd":r["cwd"],"summary":summary}
            return "test_report",payload,(None if ok else "%s test suite exited %s, expected %s"%(c["key"],r["exit_code"],c["expected_exit_code"]))
        if m=="log_match":
            if c.get("argv"):
                r=run_command(c["argv"],self.cfg,isolate=True)
                text=r["output_excerpt"]
                sha=r["output_sha256"]
            else:
                fp=_safe_workdir_file(self.cfg.workdir,c["log_file"])
                if not fp:
                    return "log_match",{"pattern":c["pattern"],"matched":False,"excerpt":""},"%s log file %r not found in workdir"%(c["key"],c["log_file"])
                with open(fp,"rb") as f:
                    raw=f.read(self.cfg.max_output)
                sha=hashlib.sha256(raw).hexdigest()
                text=raw.decode("utf-8","replace")
            pat=c["pattern"]
            try:
                hit=re.search(pat,text)
            except re.error:
                hit=None
            matched=bool(hit) or (pat in text)
            excerpt=hit.group(0)[:500] if hit else (text[-EXCERPT:] if matched else "")
            payload={"pattern":pat,"matched":matched,"excerpt":excerpt or pat[:200],"log_sha256":sha}
            return "log_match",payload,(None if matched else "%s pattern %r did not match"%(c["key"],pat))
        url=c["url"]
        want=c["expected"]
        status=0
        body_text=""
        try:
            req=urllib.request.Request(url,headers={"User"+"-Agent":"stuckbot-verify/1.0"})
            with urllib.request.urlopen(req,timeout=min(self.cfg.timeout,30.0)) as resp:
                status=int(resp.status)
                body_text=resp.read(65536).decode("utf-8","replace")
        except urllib.error.HTTPError as e:
            status=int(e.code)
        except OSError as e:
            return "external_check",{"url":url,"status":0,"passed":False,"error":str(e)[:200]},"%s request to %s failed: %s"%(c["key"],url,e)
        ok=(str(status)==want) if want.isdigit() else (200<=status<300 and want in body_text)
        payload={"url":url,"status":status,"passed":ok,"checked_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
        return "external_check",payload,(None if ok else "%s check of %s returned %s, expected %s"%(c["key"],url,status,want))
    def check_one(self,job:Dict[str,Any])->Optional[Dict[str,Any]]:
        ticket=self.client.get_ticket(job["ticket_id"])["ticket"]
        todo=plan(ticket,job["stage"],self.cfg)
        if todo is None:
            log.info("%s: skipped, a command is not in the allow list or a check is not runnable",job.get("ticket_ref"))
            return None
        try:
            self.client.claim_verification(job["id"])
        except StuckBotError as e:
            log.info("%s: not claimed (%s)",job.get("ticket_ref"),e.code)
            return None
        for raw in self.cfg.prepare:
            argv=fill(raw,ticket,job)
            if argv is None:
                log.warning("%s: ticket id, ref or candidate ref has an unexpected format; leaving the job",job.get("ticket_ref"))
                return None
            r=run_command(argv,self.cfg,isolate=(self.cfg.sandbox=="bwrap"))
            if r["exit_code"]!=0:
                log.warning("%s: prepare step %s failed with exit %s; leaving the job",job.get("ticket_ref"),argv,r["exit_code"])
                return None
        cand=ticket_candidate(ticket,job)
        head_commit,head_tree=workdir_git_head(self.cfg.workdir)
        if cand["commit"] and head_commit and head_commit!=cand["commit"]:
            log.warning("%s: workdir HEAD %s does not match candidate commit %s; leaving the job",job.get("ticket_ref"),head_commit,cand["commit"])
            return None
        commit_sha=head_commit or cand["commit"]
        tree_sha=head_tree or cand["tree"]
        c_sha=criteria_hash(ticket,job["stage"],job)
        ctx={"workdir":self.cfg.workdir,"sandbox":self.cfg.sandbox or "none"}
        evidence=[]
        failed=[]
        for c in todo:
            etype,payload,err_msg=self._run_criterion(c)
            if commit_sha:
                payload.setdefault("candidate_commit_sha",commit_sha)
            if tree_sha:
                payload.setdefault("candidate_tree_sha",tree_sha)
            evidence.append({
                "criterion":c["key"],
                "type":etype,
                "payload":payload,
                "attestation":attestation(self.key,job["id"],ticket["id"],c["key"],job["stage"],etype,payload,candidate_commit_sha=commit_sha,candidate_tree_sha=tree_sha,criteria_sha256=c_sha,context=ctx),
            })
            if err_msg:
                failed.append(err_msg)
        decision="reject" if failed else "approve"
        reason=("Failed on the verifier's machine: "+"; ".join(failed)) if failed else "Every check passed when run on the verifier's own machine: "+", ".join(c["key"] for c in todo)+"."
        out=self.client.decide_verification(job["id"],decision,reason,evidence=evidence)
        log.info("%s: %s",job.get("ticket_ref"),decision)
        return out
    def run(self,once:bool=False)->int:
        done=0
        while True:
            try:
                jobs=self.client.list_verifications().get("verifications",[])
            except StuckBotError as e:
                log.warning("could not list verifications: %s",e)
                jobs=[]
            for job in jobs:
                try:
                    if self.check_one(job):
                        done+=1
                except StuckBotError as e:
                    log.warning("%s: %s",job.get("ticket_ref"),e)
            if once:
                return done
            time.sleep(self.cfg.poll)
def audit(client:Client,ticket:str,public:VerifyingKey)->int:
    """Re-check every signed result on a ticket with your public key. Returns the number that fail."""
    bad=0
    t=client.get_ticket(ticket)["ticket"]
    cand=ticket_candidate(t)
    for e in t.get("evidence",[]):
        att=e.get("attestation")
        if e.get("trust_label")!="customer_verified" or not att:
            continue
        if not att.get("statement"):
            print("%s %s: signature was checked by StuckBot; statement not stored by StuckBot (it looked like it held a secret, or the project uses strict privacy; sha256 %s)"%(e["stage"],e["criterion_key"],att.get("statement_sha256")))
            continue
        st=check_attestation(public,att)
        etype=st.get("type") if st else ""
        keys=("command","exit_code","output_sha256") if etype=="command_run" else (("runner","passed","failed") if etype=="test_report" else (("pattern","matched") if etype=="log_match" else ("url","status","passed")))
        commit_ok=not (cand["commit"] and st and st.get("candidate_commit_sha") and st.get("candidate_commit_sha")!=cand["commit"])
        ok=st is not None and commit_ok and st.get("ticket_id")==t["id"] and st.get("criterion_key")==e["criterion_key"] and st.get("stage")==e["stage"] and all(st["payload"].get(k)==e["content"].get(k) for k in keys if k in e.get("content",{}))
        print("%s %s: %s"%(e["stage"],e["criterion_key"],"signature valid and matches the stored result" if ok else "DOES NOT MATCH"))
        bad+=0 if ok else 1
    return bad
def main(argv:Optional[List[str]]=None)->int:
    ap=argparse.ArgumentParser(prog="stuckbot-verify",description="Check StuckBot fixes on your own machine and sign the results.")
    sub=ap.add_subparsers(dest="cmd",required=True)
    k=sub.add_parser("keygen",help="make a signing key and print the public key to add under Policy, Your verifiers")
    k.add_argument("--out",default="~/.config/stuckbot/verifier-key.pem")
    r=sub.add_parser("run",help="take verification jobs and check them (token in STUCKBOT_TOKEN)")
    r.add_argument("--config",required=True)
    r.add_argument("--once",action="store_true",help="check what is waiting now, then exit")
    a=sub.add_parser("audit",help="re-check the signed results on a ticket with your public key")
    a.add_argument("ticket")
    a.add_argument("--public-key",required=True)
    args=ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    if args.cmd=="keygen":
        path=os.path.expanduser(args.out)
        os.makedirs(os.path.dirname(path) or ".",mode=0o700,exist_ok=True)
        key=SigningKey.generate()
        key.save(path)
        vk=key.verifying_key()
        print("Private key saved to %s (keep it on this machine only).\n"%path)
        print(vk.to_pem())
        print("Fingerprint: %s"%vk.fingerprint())
        return 0
    if args.cmd=="run":
        cfg=Config.load(args.config)
        v=Verifier(Client(),SigningKey.load(cfg.key_file),cfg)
        n=v.run(once=args.once)
        if args.once:
            print("checked %d verification jobs"%n)
        return 0
    with open(args.public_key) as f:
        public=VerifyingKey.from_pem(f.read())
    return 1 if audit(Client(),args.ticket,public) else 0
if __name__=="__main__":
    sys.exit(main())
