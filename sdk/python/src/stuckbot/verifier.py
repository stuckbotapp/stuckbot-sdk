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
import subprocess
import sys
import time
from typing import Any,Dict,List,Optional
from .attest import SigningKey,VerifyingKey,attestation,check_attestation
from .client import Client
from .errors import StuckBotError
log=logging.getLogger("stuckbot.verify")
EXCERPT=2000
_REF=re.compile(r"^SB-[0-9]{1,12}$")
_UUID=re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
def fill(argv:List[str],ticket:Dict[str,Any])->Optional[List[str]]:
    """argv with {ticket_ref} and {ticket_id} filled in, or None if the ticket's values do not look right."""
    ref=str(ticket.get("ref") or "")
    tid=str(ticket.get("id") or "")
    if not _REF.match(ref) or not _UUID.match(tid):
        return None
    return [a.replace("{ticket_ref}",ref).replace("{ticket_id}",tid) for a in argv]
class Config:
    def __init__(self,data:Dict[str,Any],base:str="."):
        self.key_file=os.path.expanduser(str(data.get("key_file","~/.config/stuckbot/verifier-key.pem")))
        self.workdir=os.path.abspath(os.path.join(base,os.path.expanduser(str(data.get("workdir",".")))))
        self.timeout=float(data.get("timeout_seconds",600))
        self.poll=float(data.get("poll_seconds",30))
        self.env={str(k):str(v) for k,v in dict(data.get("env",{"PATH":"/usr/local/bin:/usr/bin:/bin"})).items()}
        self.allow=[_argv(c) for c in data.get("allow",[])]
        self.prepare=[_argv(c) for c in data.get("prepare",[])]
        self.max_output=int(data.get("max_output_bytes",10*1024*1024))
        self.sandbox=data.get("sandbox")
        if self.sandbox not in (None,"bwrap"):
            raise ValueError("sandbox must be bwrap or left out")
        if not self.allow:
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
def run_command(argv:List[str],cfg:Config,isolate:bool=False)->Dict[str,Any]:
    """Run one command without a shell. With isolate and sandbox set to bwrap, it runs in a sandbox with no network that sees only the workdir (as /work) and read-only system folders."""
    started=time.time()
    tmp=None
    cmd=argv
    env=cfg.env
    if isolate and cfg.sandbox=="bwrap":
        from .runner.sandbox import Bwrap
        import tempfile
        tmp=tempfile.mkdtemp(prefix="stuckbot-verify-")
        box=Bwrap({})
        env=box.env({**cfg.env,"HOME":"/home/agent"},tmp)
        cmd=box.wrap(argv,env,tmp,[],work=cfg.workdir)
    try:
        p=subprocess.run(cmd,cwd=cfg.workdir,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=cfg.timeout,shell=False)
        out=p.stdout[:cfg.max_output]
        code=p.returncode
        note=""
    except subprocess.TimeoutExpired as e:
        out=(e.stdout or b"")[:cfg.max_output]
        code=124
        note="stopped after %d seconds\n"%cfg.timeout
    except OSError as e:
        out=b""
        code=127
        note="could not start: %s\n"%e.strerror
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp,ignore_errors=True)
    text=out.decode("utf-8","replace")
    return {
        "exit_code":code,
        "output_sha256":hashlib.sha256(out).hexdigest(),
        "output_excerpt":(note+text[-EXCERPT:]) if (note or text) else "",
        "ran_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(started)),
        "cwd":cfg.workdir,
    }
def plan(ticket:Dict[str,Any],stage:str,cfg:Config)->Optional[List[Dict[str,Any]]]:
    """The command criteria to run for this stage, or None if this verifier cannot check all of them."""
    pkg=(ticket.get("content") or {}).get("package") or {}
    out=[]
    for c in pkg.get(stage+"_criteria") or []:
        method=c.get("method")
        if method=="manual":
            continue
        if method!="command" or not c.get("command"):
            return None
        argv=_argv(c["command"])
        if argv not in cfg.allow:
            return None
        out.append({"key":c["key"],"command":c["command"],"argv":argv,"expected_exit_code":int(c.get("expected_exit_code",0))})
    return out or None
class Verifier:
    def __init__(self,client:Client,key:SigningKey,cfg:Config):
        self.client=client
        self.key=key
        self.cfg=cfg
    def check_one(self,job:Dict[str,Any])->Optional[Dict[str,Any]]:
        ticket=self.client.get_ticket(job["ticket_id"])["ticket"]
        todo=plan(ticket,job["stage"],self.cfg)
        if todo is None:
            log.info("%s: skipped, a command is not in the allow list or a check is not a command",job.get("ticket_ref"))
            return None
        try:
            self.client.claim_verification(job["id"])
        except StuckBotError as e:
            log.info("%s: not claimed (%s)",job.get("ticket_ref"),e.code)
            return None
        for raw in self.cfg.prepare:
            argv=fill(raw,ticket)
            if argv is None:
                log.warning("%s: ticket id or ref has an unexpected format; leaving the job",job.get("ticket_ref"))
                return None
            r=run_command(argv,self.cfg)
            if r["exit_code"]!=0:
                log.warning("%s: prepare step %s failed with exit %s; leaving the job",job.get("ticket_ref"),argv,r["exit_code"])
                return None
        evidence=[]
        failed=[]
        for c in todo:
            r=run_command(c["argv"],self.cfg,isolate=True)
            payload={"command":c["command"],"exit_code":r["exit_code"],"output_sha256":r["output_sha256"],"ran_at":r["ran_at"],"cwd":r["cwd"]}
            if r["output_excerpt"]:
                payload["output_excerpt"]=r["output_excerpt"]
            evidence.append({
                "criterion":c["key"],
                "type":"command_run",
                "attestation":attestation(self.key,job["id"],ticket["id"],c["key"],job["stage"],"command_run",payload),
            })
            if r["exit_code"]!=c["expected_exit_code"]:
                failed.append("%s exited %s, expected %s"%(c["key"],r["exit_code"],c["expected_exit_code"]))
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
    for e in t.get("evidence",[]):
        att=e.get("attestation")
        if e.get("trust_label")!="customer_verified" or not att:
            continue
        if not att.get("statement"):
            print("%s %s: signature was checked by StuckBot; statement not stored by StuckBot (it looked like it held a secret, or the project uses strict privacy; sha256 %s)"%(e["stage"],e["criterion_key"],att.get("statement_sha256")))
            continue
        st=check_attestation(public,att)
        ok=st is not None and st.get("ticket_id")==t["id"] and st.get("criterion_key")==e["criterion_key"] and st.get("stage")==e["stage"] and all(st["payload"].get(k)==e["content"].get(k) for k in ("command","exit_code","output_sha256"))
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
