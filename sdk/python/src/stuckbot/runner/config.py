"""Runner configuration (JSON). Everything not set falls back to a conservative default."""
from __future__ import annotations
import json
import os
import re
from typing import Any,Dict,List,Optional
from . import adapters
from ..verifier import _argv
LIMITS={
    "max_concurrent":1,
    "max_runtime_seconds":1800,
    "max_attempts_per_ticket":2,
    "max_handoff_depth":2,
    "hourly_usd":5.0,
    "daily_usd":20.0,
    "breaker_failures":3,
    "breaker_cooldown_seconds":900,
    "max_workspace_mb":2048,
    "drain_seconds":60,
    "check_timeout_seconds":600,
}
PROFILE_LIMITS=("max_usd_per_session","hourly_usd","daily_usd","max_concurrent")
def _norm_repo(s:str)->str:
    s=s.strip().lower()
    s=re.sub(r"^[a-z+]+://","",s)
    s=re.sub(r"^[^@/]+@","",s)
    s=s.replace(":","/")
    return s.rstrip("/").removesuffix(".git")
class Config:
    def __init__(self,data:Dict[str,Any],base:str="."):
        self.raw=data
        rel=lambda p:os.path.abspath(os.path.join(base,os.path.expanduser(str(p))))
        self.base_url=data.get("base_url")
        self.enrollment_secret_file=rel(data["enrollment_secret_file"]) if data.get("enrollment_secret_file") else None
        self.state_dir=rel(data.get("state_dir","./runner-state"))
        self.name=str(data.get("name") or "runner")
        self.poll_seconds=float(data.get("poll_seconds",30))
        self.projects=list(data.get("projects",[]))
        self.labels=list(data.get("labels",[]))
        self.min_urgency=data.get("min_urgency")
        self.limits={**LIMITS,**(data.get("limits") or {})}
        self.sandbox=dict(data.get("sandbox") or {"kind":"bwrap"})
        self.sandbox.setdefault("kind","bwrap")
        self.egress=list(self.sandbox.get("egress",[]))
        self.allow_commands=[_argv(c) for c in data.get("allow_commands",[])]
        self.on_give_up=data.get("on_give_up","escalate")
        self.approval=dict(data.get("approval") or {})
        self.default_workspace=data.get("default_workspace")
        self.workspaces={}
        for name,w in (data.get("workspaces") or {}).items():
            w=dict(w)
            if w.get("repo"):
                w["repo"]=rel(w["repo"])
            w["name"]=name
            w["match"]={_norm_repo(m) for m in [name]+list(w.get("match",[]))}
            self.workspaces[name]=w
        self.providers=[dict(p) for p in data.get("providers",[])]
        self.validate()
    def validate(self)->None:
        problems=[]
        if not self.providers:
            problems.append("providers is empty: add at least one provider profile")
        names=set()
        for p in self.providers:
            n=p.get("name")
            if not n or not re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$",str(n)):
                problems.append(f"provider name {n!r} must be 1 to 32 lowercase letters, digits, - or _")
            if n in names:
                problems.append(f"provider name {n!r} is used twice")
            names.add(n)
            if p.get("adapter","command") not in adapters.ADAPTERS:
                problems.append(f"provider {n}: unknown adapter {p.get('adapter')!r}")
            if not float(p.get("max_usd_per_session",0) or 0)>0:
                problems.append(f"provider {n}: set max_usd_per_session (the most one session may spend)")
            if p.get("adapter","command")!="command" and not p.get("price_per_mtok"):
                problems.append(f"provider {n}: set price_per_mtok (input, output, cache_write, cache_read USD per million tokens) so spending can be measured")
            if p.get("adapter","command")=="command" and not p.get("args") and not p.get("program"):
                problems.append(f"provider {n}: the command adapter needs program and args")
        for k in ("hourly_usd","daily_usd","max_runtime_seconds","max_concurrent"):
            if not float(self.limits.get(k) or 0)>0:
                problems.append(f"limits.{k} must be more than 0")
        if self.sandbox["kind"]=="none" and not self.sandbox.get("i_understand_there_is_no_isolation"):
            problems.append("sandbox.kind none runs agents with this user's full access; also set sandbox.i_understand_there_is_no_isolation to true, and only on a throwaway machine")
        if self.sandbox["kind"] not in ("bwrap","none"):
            problems.append("sandbox.kind must be bwrap or none")
        if self.on_give_up not in ("escalate","release"):
            problems.append("on_give_up must be escalate or release")
        if self.default_workspace and self.default_workspace!="scratch" and self.default_workspace not in self.workspaces:
            problems.append(f"default_workspace {self.default_workspace!r} is not in workspaces")
        for w in self.workspaces.values():
            if w.get("repo") and not os.path.isdir(w["repo"]):
                problems.append(f"workspace {w['name']}: repo {w['repo']} does not exist")
        if problems:
            raise ValueError("config problems:\n- "+"\n- ".join(problems))
    @classmethod
    def load(cls,path:str)->"Config":
        with open(path) as f:
            return cls(json.load(f),os.path.dirname(os.path.abspath(path)))
    def enrollment_secret(self)->Optional[str]:
        if self.enrollment_secret_file:
            with open(self.enrollment_secret_file) as f:
                return f.read().strip()
        return os.environ.get("STUCKBOT_ENROLL")
    def workspace_for(self,ticket:Dict[str,Any])->Optional[Dict[str,Any]]:
        """The authorized workspace this ticket points at, {"name":"scratch"} for an empty folder, or None."""
        ws=(((ticket.get("content") or {}).get("package") or {}).get("context") or {}).get("workspace") or {}
        repo=ws.get("repo")
        if repo:
            key=_norm_repo(str(repo))
            for w in self.workspaces.values():
                if key in w["match"]:
                    return w
            return None
        if self.default_workspace=="scratch":
            return {"name":"scratch","match":set()}
        if self.default_workspace:
            return self.workspaces[self.default_workspace]
        return None
    def profile_limit(self,p:Dict[str,Any],key:str)->float:
        v=p.get(key)
        return float(v) if v is not None else float("inf")
EXAMPLE={
    "base_url":"https://www.stuckbot.com",
    "enrollment_secret_file":"/etc/stuckbot-runner/enrollment-secret",
    "state_dir":"/var/lib/stuckbot-runner",
    "name":"build-box",
    "poll_seconds":30,
    "limits":{"max_concurrent":1,"max_runtime_seconds":1800,"max_attempts_per_ticket":2,"max_handoff_depth":2,"hourly_usd":5,"daily_usd":20},
    "providers":[{
        "name":"claude",
        "adapter":"claude-code",
        "model":"claude-sonnet-5-5",
        "max_usd_per_session":2,
        "price_per_mtok":{"input":2,"output":10,"cache_write":4,"cache_read":0.2},
        "env_files":{"ANTHROPIC_API_KEY":"/etc/stuckbot-runner/anthropic-key"},
    }],
    "workspaces":{"demo":{"repo":"/srv/git/demo","match":["github.com/acme/demo"]}},
    "sandbox":{"kind":"bwrap"},
}
