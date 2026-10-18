from __future__ import annotations
import calendar
import contextlib
import json
import os
import re
import threading
import time
from typing import Any,Callable,Dict,Iterator,List,Optional
from .client import Client,_env
from .errors import StuckBotError
LEASED=("claimed","working","continuing")
_TS=re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?(Z|[+-]\d\d(?::?\d\d)?)?$")
def parse_time(value:Optional[str])->Optional[float]:
    """Parse a server timestamp into Unix seconds."""
    m=_TS.match(value or "")
    if not m:
        return None
    y,mo,d,h,mi,s=(int(x) for x in m.groups()[:6])
    t=calendar.timegm((y,mo,d,h,mi,s,0,0,0))+(float("0."+m.group(7)) if m.group(7) else 0.0)
    z=m.group(8)
    if z and z!="Z":
        sign=-1 if z[0]=="-" else 1
        digits=z[1:].replace(":","")
        t-=sign*(int(digits[:2])*3600+int(digits[2:4] or 0)*60)
    return t
class WorkItem:
    """Something FleetAgent.poll found: kind is claim or continue (data is a ticket summary) or update (data is an event on your own ticket)."""
    __slots__=("kind","data")
    def __init__(self,kind,data):
        self.kind=kind
        self.data=data
    @property
    def ticket_id(self)->str:
        """The ticket this item is about."""
        return self.data["ticket_id"] if self.kind=="update" else self.data["id"]
    def __repr__(self):
        return f"WorkItem(kind={self.kind!r}, ticket_id={self.ticket_id!r})"
class Lease:
    """A claim held inside FleetAgent.claim. It renews itself in the background until the block ends."""
    def __init__(self,client,ticket,renew_every):
        self.client=client
        self.ticket=ticket
        self.ticket_id=ticket["id"]
        self.renewals=0
        self.error=None
        self.lost=False
        self.submitted=False
        self.released=False
        claim=ticket.get("claim") or {}
        start=parse_time(claim.get("started_at"))
        end=parse_time(claim.get("expires_at"))
        length=end-start if start is not None and end is not None and end>start else 1800.0
        self.lease_seconds=length
        self.renew_every=renew_every if renew_every else max(1.0,length/3)
        self._stop=threading.Event()
        self._thread=None
    @property
    def state(self)->str:
        """The ticket state from the last answer."""
        return self.ticket.get("state","")
    @property
    def expires_at(self)->Optional[str]:
        """When the lease runs out unless renewed."""
        return (self.ticket.get("claim") or {}).get("expires_at")
    @property
    def held(self)->bool:
        """True while this lease still holds an active claim."""
        return not self.released and not self.lost and self.state in LEASED
    def _keep(self,r):
        if isinstance(r,dict) and isinstance(r.get("ticket"),dict):
            self.ticket=r["ticket"]
        return r
    def _start(self):
        self._thread=threading.Thread(target=self._run,name=f"stuckbot-lease-{self.ticket_id}",daemon=True)
        self._thread.start()
    def _run(self):
        while not self._stop.wait(self.renew_every):
            try:
                self.renew()
            except StuckBotError as e:
                self.error=e
                if e.status in (404,409):
                    self.lost=e.code!="state_changed"
                    if self.lost:
                        return
            except Exception as e:
                self.error=e
    def close(self):
        """Stop renewing. The claim itself stays until it is submitted, released or expires."""
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.client.timeout+1)
    def renew(self)->Dict[str,Any]:
        """Renew the lease now."""
        r=self._keep(self.client.renew_claim(self.ticket_id))
        self.renewals+=1
        return r
    def update(self,body:str,progress:Optional[Dict[str,Any]]=None)->Dict[str,Any]:
        """Post a progress note."""
        return self._keep(self.client.post_update(self.ticket_id,body,progress))
    def evidence(self,criterion:str,type:str,payload:Optional[Dict[str,Any]]=None,result:Optional[str]=None,**fields:Any)->Dict[str,Any]:
        """Add evidence for one criterion."""
        return self._keep(self.client.add_evidence(self.ticket_id,criterion,type,payload,result,**fields))
    def submit(self,stage:str="blocker")->Dict[str,Any]:
        """Submit a stage for verification. After this the lease is not released on error."""
        r=self._keep(self.client.submit_stage(self.ticket_id,stage))
        self.submitted=True
        return r
    def handoff(self,package:Dict[str,Any],reason:Optional[str]=None)->Dict[str,Any]:
        """Hand the ticket off to a new child ticket."""
        r=self._keep(self.client.handoff_ticket(self.ticket_id,package,reason))
        self.submitted=True
        return r
    def escalate(self,reason:str)->Dict[str,Any]:
        """Ask a person to step in."""
        r=self._keep(self.client.escalate_ticket(self.ticket_id,reason))
        self.submitted=True
        return r
    def release(self,note:Optional[str]=None)->Dict[str,Any]:
        """Give the ticket back."""
        r=self._keep(self.client.release_ticket(self.ticket_id,note))
        self.released=True
        return r
class FleetAgent:
    """An agent that registers with a connector enrollment secret, keeps its token fresh, sends heartbeats and polls for work."""
    def __init__(self,enrollment_secret:Optional[str]=None,base_url:Optional[str]=None,state_file:Optional[str]=None,labels:Optional[Dict[str,Any]]=None,runtime:Optional[str]=None,provider:Optional[str]=None,model:Optional[str]=None,capabilities:Optional[Dict[str,Any]]=None,supports_stop:Optional[bool]=None,scopes:Optional[List[str]]=None,project_ids:Optional[List[str]]=None,parent_agent_id:Optional[str]=None,name:Optional[str]=None,heartbeat_interval:float=60.0,heartbeat_payload:Optional[Callable[[],Optional[Dict[str,Any]]]]=None,renew_before:Optional[float]=None,on_handoff_requested:Optional[Callable[[Dict[str,Any]],None]]=None,on_stop_requested:Optional[Callable[[Dict[str,Any]],None]]=None,on_error:Optional[Callable[[BaseException],None]]=None,timeout:float=30.0,max_retries:int=3,retire_on_stop:bool=False):
        """enrollment_secret defaults to STUCKBOT_ENROLL. state_file keeps the agent id and token (mode 0600) so a restart reuses the agent. retire_on_stop retires the agent in stop()."""
        self.enrollment_secret=enrollment_secret or _env("STUCKBOT_ENROLL")
        if not self.enrollment_secret:
            raise ValueError("Pass the connector enrollment secret or set STUCKBOT_ENROLL.")
        self.client=Client(token="",base_url=base_url,timeout=timeout,max_retries=max_retries,on_auth_failure=self._auth_failure)
        self.state_file=state_file
        self.registration={k:v for k,v in {"name":name,"labels":labels,"runtime":runtime,"provider":provider,"model":model,"capabilities":capabilities,"supports_stop":supports_stop,"scopes":scopes,"project_ids":project_ids,"parent_agent_id":parent_agent_id}.items() if v is not None}
        self.heartbeat_interval=heartbeat_interval
        self.heartbeat_payload=heartbeat_payload
        self.renew_before=renew_before
        self.on_handoff_requested=on_handoff_requested
        self.on_stop_requested=on_stop_requested
        self.on_error=on_error
        self.retire_on_stop=retire_on_stop
        self.agent=None
        self.expires_at=None
        self.handoff_requested=None
        self.stop_requested=None
        self.cursor=None
        self.last_error=None
        self.registrations=0
        self.renewals=0
        self.heartbeats=0
        self._renew_at=None
        self._lock=threading.RLock()
        self._stop=threading.Event()
        self._wake=threading.Event()
        self._thread=None
        self._leases=set()
    @property
    def agent_id(self)->Optional[str]:
        """This agent's id."""
        return self.agent["id"] if self.agent else None
    @property
    def token(self)->Optional[str]:
        """The current agent token."""
        return self.client.token or None
    def __enter__(self):
        return self.start()
    def __exit__(self,*exc):
        self.stop()
    def start(self)->"FleetAgent":
        """Resume from the state file or register, then start heartbeats and token renewal in a background thread."""
        with self._lock:
            self._stop.clear()
            if not self.agent and not self._resume():
                self._register()
            if not self._thread or not self._thread.is_alive():
                self._thread=threading.Thread(target=self._run,name="stuckbot-fleet",daemon=True)
                self._thread.start()
        return self
    def stop(self)->None:
        """Stop heartbeats, renewal, lease renewal and polling. The token stays valid until it expires, unless retire_on_stop is set: then the agent is retired (its tickets go back to open) and the state file is deleted. A failed retire goes to on_error."""
        self._stop.set()
        self._wake.set()
        for lease in list(self._leases):
            lease.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.client.timeout+1)
        if self.retire_on_stop and self.agent:
            with self._lock:
                try:
                    self.client.retire_agent()
                    self.agent=None
                    self.client.token=""
                    self.expires_at=None
                    self._renew_at=None
                    if self.state_file and os.path.exists(self.state_file):
                        os.remove(self.state_file)
                except (StuckBotError,OSError) as e:
                    self._report(e)
    def _ensure(self):
        if not self.agent:
            self.start()
    def _grant(self,r):
        self.client.token=r["token"]
        self.agent=r.get("agent") or self.agent
        self.expires_at=r.get("expires_at")
        self._schedule()
        self._save()
    def _schedule(self):
        end=parse_time(self.expires_at)
        if end is None:
            self._renew_at=None
            return
        left=end-time.time()
        before=self.renew_before if self.renew_before is not None else max(30.0,left/2)
        self._renew_at=time.monotonic()+max(0.0,left-before)
        self._wake.set()
    def _resume(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file) as f:
                s=json.load(f)
        except (OSError,ValueError):
            return False
        end=parse_time(s.get("expires_at"))
        if not s.get("token") or s.get("base_url")!=self.client.base_url or end is None or end<time.time()+30:
            return False
        self.client.token=s["token"]
        try:
            me=self.client.request("GET","/api/v1/agents/me",refresh=False)
        except StuckBotError as e:
            if e.status in (401,404):
                self.client.token=""
                return False
            raise
        self.agent=me["agent"]
        self.expires_at=s["expires_at"]
        self._schedule()
        return True
    def _register(self):
        r=self.client.register_agent(self.enrollment_secret,**self.registration)
        self.registrations+=1
        self.handoff_requested=None
        self.stop_requested=None
        self.cursor=None
        self._grant(r)
        return r
    def _save(self):
        if not self.state_file:
            return
        data=json.dumps({"base_url":self.client.base_url,"agent_id":self.agent_id,"agent_name":(self.agent or {}).get("name"),"token":self.client.token,"expires_at":self.expires_at})
        tmp=f"{self.state_file}.{os.getpid()}.tmp"
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,"w") as f:
            f.write(data)
        os.chmod(tmp,0o600)
        os.replace(tmp,self.state_file)
    def renew(self)->Dict[str,Any]:
        """Renew the token now, or register again when renewal is no longer possible (revoked, expired or lifetime_exceeded)."""
        with self._lock:
            try:
                r=self.client.renew_agent_token(apply=False)
            except StuckBotError as e:
                if e.status!=401:
                    raise
                return self._register()
            self.renewals+=1
            self._grant(r)
            return r
    def _auth_failure(self,err,used):
        with self._lock:
            if used!=self.client.token:
                return True
            try:
                self.renew()
            except StuckBotError:
                return False
            return True
    def _flags(self,d):
        if not isinstance(d,dict):
            return
        for key,cb in (("handoff_requested",self.on_handoff_requested),("stop_requested",self.on_stop_requested)):
            if key not in d:
                continue
            new=d.get(key)
            old=getattr(self,key)
            setattr(self,key,new)
            if new and not old and cb:
                try:
                    cb(new)
                except Exception as e:
                    self._report(e)
    def _report(self,e):
        self.last_error=e
        if self.on_error:
            try:
                self.on_error(e)
            except Exception:
                pass
    def heartbeat(self,status:Optional[str]=None,usage:Optional[Dict[str,float]]=None)->Dict[str,Any]:
        """Send one heartbeat now. Without arguments it asks heartbeat_payload for usage and status."""
        self._ensure()
        if status is None and usage is None and self.heartbeat_payload:
            p=self.heartbeat_payload() or {}
            status=p.get("status")
            usage=p.get("usage")
        r=self.client.heartbeat(status=status,usage=usage)
        self.heartbeats+=1
        self._flags(r)
        if r.get("token_expires_soon"):
            self._renew_at=time.monotonic()
            self._wake.set()
        return r
    def check_signals(self,limit:Optional[int]=None)->Dict[str,Any]:
        """Read this agent's signals and update handoff_requested and stop_requested."""
        self._ensure()
        r=self.client.list_my_signals(limit)
        self._flags(r)
        return r
    def _run(self):
        next_hb=time.monotonic()
        while not self._stop.is_set():
            now=time.monotonic()
            if self._renew_at is not None and now>=self._renew_at:
                try:
                    self.renew()
                except Exception as e:
                    self._report(e)
                    self._renew_at=now+30
            if now>=next_hb:
                try:
                    self.heartbeat()
                except Exception as e:
                    self._report(e)
                next_hb=time.monotonic()+self.heartbeat_interval
            due=min(x for x in (next_hb,self._renew_at) if x is not None)
            self._wake.wait(max(0.01,due-time.monotonic()))
            self._wake.clear()
    def poll(self,max_wait:Optional[float]=None,stop_on_stop_requested:bool=True,**filters:Any)->Iterator[WorkItem]:
        """Yield claimable tickets and updates from /work until stop() is called, waiting retry_after_seconds between polls."""
        self._ensure()
        while not self._stop.is_set():
            try:
                r=self.client.work(cursor=self.cursor,**filters)
            except StuckBotError as e:
                if e.status and e.status<500 and e.status!=429:
                    raise
                self._report(e)
                if self._stop.wait(min(max_wait or 30.0,e.retry_after or 30.0)):
                    return
                continue
            self._flags(r)
            if not r.unchanged:
                for t in r.get("claimable") or []:
                    yield WorkItem(t.get("work","claim"),t)
                for u in r.get("updates") or []:
                    yield WorkItem("update",u)
                self.cursor=r.get("next_cursor") or self.cursor
            if stop_on_stop_requested and self.stop_requested:
                return
            wait=r.retry_after_seconds if r.retry_after_seconds is not None else 60
            if max_wait is not None:
                wait=min(wait,max_wait)
            if self._stop.wait(wait):
                return
    @contextlib.contextmanager
    def claim(self,ticket:Any,continue_task:Optional[bool]=None,renew_every:Optional[float]=None,release_on_error:bool=True)->Iterator[Lease]:
        """Claim a ticket (or continue it), renew the lease in the background while the block runs, and release it if the block raises before submitting. A lost race raises ClaimLost before the block starts."""
        self._ensure()
        if isinstance(ticket,WorkItem):
            continue_task=ticket.kind=="continue" if continue_task is None else continue_task
            ticket=ticket.data
        if continue_task is None and isinstance(ticket,dict):
            continue_task=ticket.get("work")=="continue"
        r=self.client.continue_task(ticket) if continue_task else self.client.claim_ticket(ticket)
        lease=Lease(self.client,r["ticket"],renew_every)
        self._leases.add(lease)
        lease._start()
        try:
            yield lease
        except BaseException as e:
            lease.close()
            if release_on_error and lease.held and not lease.submitted:
                try:
                    lease.release(f"Released after an error in the solver: {type(e).__name__}")
                except StuckBotError as re_err:
                    self._report(re_err)
            raise
        finally:
            lease.close()
            self._leases.discard(lease)
