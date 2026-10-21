"""stuckbot-runner: start fresh AI agent sessions on StuckBot tickets, on your own machine.

It claims a ticket before it starts anything, runs each session in a sandbox with its own copy
of an authorized repository, enforces spending, time, concurrency and retry limits itself, reruns
the checks, and submits the result. Your AI provider login stays on this machine.
"""
from __future__ import annotations
import argparse
import getpass
import json
import logging
import os
import signal
import sys
import time
from typing import List,Optional
from .config import Config,EXAMPLE
from .core import Runner,Session,prompt
from .state import State
__all__=["Config","Runner","Session","State","main"]
def _state(cfg:Config)->State:
    return State(os.path.join(cfg.state_dir,"runner.db"))
def check(cfg:Config)->int:
    from . import adapters,sandbox
    bad=0
    sbx=sandbox.make(cfg.sandbox)
    problem=sbx.available()
    print(f"sandbox: {sbx.kind}"+(f" - NOT READY: {problem}" if problem else " - ready"))
    bad+=1 if problem else 0
    if sbx.kind=="none":
        print("  WARNING: sessions run with this user's full access to files and network")
    for p in cfg.providers:
        a=adapters.make(p)
        try:
            prog=a.program()
            creds=a.credentials()
            print(f"provider {p['name']}: {a.name}, program {prog}, credentials {', '.join(sorted(creds)) or 'none set'}, up to ${float(p['max_usd_per_session']):g} a session")
            print(f"  egress: {', '.join(a.egress()+cfg.egress) or 'none'}")
            print(f"  spending: {a.usage_note}")
        except (OSError,ValueError) as e:
            print(f"provider {p['name']}: NOT READY - {e}")
            bad+=1
    for w in cfg.workspaces.values():
        print(f"workspace {w['name']}: {w.get('repo') or 'scratch'}"+(" (needs approval)" if w.get("approval") else ""))
    lim=cfg.limits
    print(f"limits: {lim['max_concurrent']} at a time, {lim['max_runtime_seconds']}s per session, ${lim['hourly_usd']:g}/hour, ${lim['daily_usd']:g}/day, {lim['max_attempts_per_ticket']} attempts per problem, handoff depth {lim['max_handoff_depth']}")
    if not cfg.enrollment_secret():
        print("enrollment secret: NOT SET (enrollment_secret_file or STUCKBOT_ENROLL)")
        bad+=1
    return 1 if bad else 0
def status(cfg:Config)->int:
    st=_state(cfg)
    now=time.time()
    print(f"spent: ${st.spent(now-3600):.2f} in the last hour, ${st.spent(now-86400):.2f} in the last day; ${st.reserved():.2f} reserved by running sessions")
    for p in cfg.providers:
        b=st.breaker(p["name"])
        if b["open_until"]>now:
            print(f"{p['name']}: paused for {int(b['open_until']-now)}s after {b['failures']} failed sessions")
    for r in st.recent(20):
        when=time.strftime("%Y-%m-%d %H:%M",time.localtime(r["created_at"]))
        print(f"{when} {r['ticket_ref'] or r['ticket_id']:<10} {r['profile']:<12} {r['state']:<12} {r['outcome'] or '':<12} ${r['spent_usd']:.2f} {r['note'] or ''}")
    return 0
def main(argv:Optional[List[str]]=None)->int:
    ap=argparse.ArgumentParser(prog="stuckbot-runner",description="Start fresh AI agent sessions on StuckBot tickets, on your own machine, within limits you set.")
    sub=ap.add_subparsers(dest="cmd",required=True)
    for name,help in (("run","take tickets and start sessions until stopped"),("check","check the config, sandbox, programs and credentials"),("status","show recent runs, spending and paused providers"),("pending","list tickets waiting for your approval")):
        s=sub.add_parser(name,help=help)
        s.add_argument("--config",required=True)
        if name=="run":
            s.add_argument("--once",action="store_true",help="look for work once, finish what started, then exit")
    a=sub.add_parser("approve",help="approve starting a session on a ticket")
    a.add_argument("ticket")
    a.add_argument("--config",required=True)
    sub.add_parser("example-config",help="print an example config")
    args=ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd=="example-config":
        print(json.dumps(EXAMPLE,indent=2))
        return 0
    try:
        cfg=Config.load(args.config)
    except (OSError,ValueError) as e:
        print(f"stuckbot-runner: {e}",file=sys.stderr)
        return 2
    if args.cmd=="check":
        return check(cfg)
    if args.cmd=="status":
        return status(cfg)
    if args.cmd=="pending":
        for r in _state(cfg).pending_approvals():
            print(f"{r['ticket_ref']}: {r['reason']}")
        return 0
    if args.cmd=="approve":
        n=_state(cfg).approve(args.ticket,getpass.getuser())
        print(f"approved {args.ticket}" if n else f"{args.ticket} is not waiting for approval")
        if n:
            _state(cfg).audit("approved",None,ticket_ref=args.ticket,by=getpass.getuser())
        return 0 if n else 1
    try:
        runner=Runner(cfg)
    except RuntimeError as e:
        print(f"stuckbot-runner: {e}",file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM,runner.stop)
    signal.signal(signal.SIGINT,runner.stop)
    runner.run(once=args.once)
    return 0
if __name__=="__main__":
    sys.exit(main())
