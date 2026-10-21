"""Durable local state for stuckbot-runner: runs, spend, circuit breakers, approvals and the audit log.

Everything the runner decides is written here before it acts, so a crash or restart can find
every claim it holds and every session it started.
"""
from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any,Dict,List,Optional
ACTIVE=("claiming","claimed","running","finishing")
SCHEMA="""
create table if not exists runs(
id text primary key,
ticket_id text not null,
ticket_ref text,
root_id text,
depth integer default 0,
profile text not null,
agent_id text,
attempt integer not null default 1,
state text not null,
pid integer,
pid_start text,
run_dir text,
reserved_usd real not null default 0,
spent_usd real not null default 0,
tokens_in integer not null default 0,
tokens_out integer not null default 0,
exit_code integer,
outcome text,
note text,
created_at real not null,
started_at real,
ended_at real
);
create index if not exists runs_state on runs(state);
create index if not exists runs_root on runs(root_id);
create table if not exists spend(
id integer primary key,
at real not null,
profile text not null,
run_id text not null,
usd real not null
);
create index if not exists spend_at on spend(at);
create table if not exists breakers(
profile text primary key,
failures integer not null default 0,
open_until real not null default 0
);
create table if not exists approvals(
ticket_id text primary key,
ticket_ref text,
reason text,
asked_at real not null,
approved_at real,
approved_by text
);
create table if not exists audit(
id integer primary key,
at real not null,
run_id text,
ticket_ref text,
kind text not null,
detail text
);
"""
class State:
    def __init__(self,path:str,audit_file:Optional[str]=None):
        os.makedirs(os.path.dirname(os.path.abspath(path)),mode=0o700,exist_ok=True)
        self.path=path
        self.audit_file=audit_file
        self._lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False,isolation_level=None,timeout=30)
        self.db.row_factory=sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=full")
        self.db.executescript(SCHEMA)
        os.chmod(path,0o600)
    def close(self)->None:
        with self._lock:
            self.db.close()
    def _x(self,sql:str,args:tuple=())->sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql,args)
    def audit(self,kind:str,run:Optional[Dict[str,Any]]=None,**detail:Any)->None:
        now=time.time()
        rid=run.get("id") if run else None
        ref=(run or {}).get("ticket_ref") or detail.pop("ticket_ref",None)
        body=json.dumps(detail,sort_keys=True,default=str)
        self._x("insert into audit(at,run_id,ticket_ref,kind,detail) values(?,?,?,?,?)",(now,rid,ref,kind,body))
        if self.audit_file:
            line=json.dumps({"at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(now)),"kind":kind,"run":rid,"ticket":ref,**detail},sort_keys=True,default=str)
            fd=os.open(self.audit_file,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
            with os.fdopen(fd,"a") as f:
                f.write(line+"\n")
    def new_run(self,ticket:Dict[str,Any],profile:str,agent_id:Optional[str],reserved:float)->Dict[str,Any]:
        rid=uuid.uuid4().hex[:16]
        attempt=self.attempts(ticket.get("root_id") or ticket["id"])+1
        self._x("insert into runs(id,ticket_id,ticket_ref,root_id,depth,profile,agent_id,attempt,state,reserved_usd,created_at) values(?,?,?,?,?,?,?,?,?,?,?)",(rid,ticket["id"],ticket.get("ref"),ticket.get("root_id") or ticket["id"],int(ticket.get("depth") or 0),profile,agent_id,attempt,"claiming",reserved,time.time()))
        return self.run(rid)
    def run(self,rid:str)->Dict[str,Any]:
        r=self._x("select * from runs where id=?",(rid,)).fetchone()
        return dict(r) if r else {}
    def update(self,rid:str,**fields:Any)->Dict[str,Any]:
        if fields:
            cols=",".join(f"{k}=?" for k in fields)
            self._x(f"update runs set {cols} where id=?",(*fields.values(),rid))
        return self.run(rid)
    def active(self)->List[Dict[str,Any]]:
        return [dict(r) for r in self._x(f"select * from runs where state in ({','.join('?'*len(ACTIVE))}) order by created_at",ACTIVE)]
    def active_roots(self)->set:
        return {r["root_id"] for r in self.active()}
    def attempts(self,root_id:str)->int:
        return int(self._x("select count(*) from runs where root_id=? and state<>'lost'",(root_id,)).fetchone()[0])
    def tried(self,root_id:str)->List[str]:
        return [r[0] for r in self._x("select distinct profile from runs where root_id=? and state<>'lost'",(root_id,))]
    def spent(self,since:float,profile:Optional[str]=None)->float:
        if profile:
            r=self._x("select coalesce(sum(usd),0) from spend where at>=? and profile=?",(since,profile)).fetchone()
        else:
            r=self._x("select coalesce(sum(usd),0) from spend where at>=?",(since,)).fetchone()
        return float(r[0])
    def reserved(self,profile:Optional[str]=None)->float:
        rows=[r for r in self.active() if profile is None or r["profile"]==profile]
        return float(sum(max(r["reserved_usd"],r["spent_usd"]) for r in rows))
    def settle(self,rid:str,usd:float)->None:
        run=self.run(rid)
        if not run:
            return
        self._x("delete from spend where run_id=?",(rid,))
        self._x("insert into spend(at,profile,run_id,usd) values(?,?,?,?)",(run.get("started_at") or run["created_at"],run["profile"],rid,float(usd)))
        self._x("update runs set spent_usd=? where id=?",(float(usd),rid))
    def breaker(self,profile:str)->Dict[str,Any]:
        r=self._x("select * from breakers where profile=?",(profile,)).fetchone()
        return dict(r) if r else {"profile":profile,"failures":0,"open_until":0.0}
    def breaker_result(self,profile:str,ok:bool,threshold:int,cooldown:float)->Dict[str,Any]:
        b=self.breaker(profile)
        failures=0 if ok else b["failures"]+1
        open_until=time.time()+cooldown if failures>=threshold else (0.0 if ok else b["open_until"])
        self._x("insert into breakers(profile,failures,open_until) values(?,?,?) on conflict(profile) do update set failures=excluded.failures,open_until=excluded.open_until",(profile,failures,open_until))
        return self.breaker(profile)
    def ask_approval(self,ticket:Dict[str,Any],reason:str)->bool:
        """Record that a ticket waits for a person. Returns True the first time."""
        cur=self._x("insert or ignore into approvals(ticket_id,ticket_ref,reason,asked_at) values(?,?,?,?)",(ticket["id"],ticket.get("ref"),reason,time.time()))
        return cur.rowcount==1
    def approved(self,ticket_id:str)->bool:
        r=self._x("select approved_at from approvals where ticket_id=?",(ticket_id,)).fetchone()
        return bool(r and r[0])
    def approve(self,ref:str,by:str)->int:
        return self._x("update approvals set approved_at=?,approved_by=? where (ticket_ref=? or ticket_id=?) and approved_at is null",(time.time(),by,ref,ref)).rowcount
    def pending_approvals(self)->List[Dict[str,Any]]:
        return [dict(r) for r in self._x("select * from approvals where approved_at is null order by asked_at")]
    def recent(self,limit:int=20)->List[Dict[str,Any]]:
        return [dict(r) for r in self._x("select * from runs order by created_at desc limit ?",(limit,))]
    def log(self,limit:int=50)->List[Dict[str,Any]]:
        return [dict(r) for r in self._x("select * from audit order by id desc limit ?",(limit,))][::-1]
