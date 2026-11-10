import json
import os
import re
import unittest
import urllib.parse
from stuckbot import OPERATIONS,Client
SPEC=os.environ.get("STUCKBOT_OPENAPI",os.path.join(os.path.dirname(__file__),"..","..","..","docs","openapi.json"))
T="0b4f6c1e-1111-4222-8333-944445555666"
CALLS={
"register_agent":lambda c:c.register_agent("sbe_x",labels={"host":"h"}),
"renew_agent_token":lambda c:c.renew_agent_token(apply=False),
"heartbeat":lambda c:c.heartbeat(status="ok"),
"get_me":lambda c:c.get_me(),
"list_my_signals":lambda c:c.list_my_signals(limit=5),
"list_inbox":lambda c:c.list_inbox(limit=5),
"ack_inbox":lambda c:c.ack_inbox([T]),
"retire_agent":lambda c:c.retire_agent(),
"create_ticket":lambda c:c.create_ticket({"title":"t"}),
"list_tickets":lambda c:c.list_tickets(mine=True),
"get_ticket":lambda c:c.get_ticket("SB-1"),
"list_ticket_events":lambda c:c.list_ticket_events(T,after=3),
"add_comment":lambda c:c.add_comment(T,"hi"),
"report_chat_reply":lambda c:c.report_chat_reply(T,"q1","yes",picks=[0]),
"upload_attachment":lambda c:c.upload_attachment(T,b"data",filename="a.txt"),
"download_attachment":lambda c:c.download_attachment(T),
"find_work":lambda c:c.find_work(),
"claim_ticket":lambda c:c.claim_ticket(T),
"renew_claim":lambda c:c.renew_claim(T),
"release_ticket":lambda c:c.release_ticket(T,"bye"),
"withdraw_ticket":lambda c:c.withdraw_ticket(T,"duplicate"),
"post_update":lambda c:c.post_update(T,"progress"),
"add_evidence":lambda c:c.add_evidence(T,"b1","command_run",command="true",exit_code=0),
"submit_stage":lambda c:c.submit_stage(T,"blocker"),
"continue_task":lambda c:c.continue_task(T),
"handoff_ticket":lambda c:c.handoff_ticket(T,{"title":"t"}),
"update_package":lambda c:c.update_package(T,{"title":"t"}),
"escalate_ticket":lambda c:c.escalate_ticket(T,"help"),
"list_verifications":lambda c:c.list_verifications(),
"claim_verification":lambda c:c.claim_verification(T),
"decide_verification":lambda c:c.decide_verification(T,"approve","checked"),
"request_access":lambda c:c.request_access(T,"the log"),
"get_context":lambda c:c.get_context(T),
"report_signal":lambda c:c.report_signal("watchdog",details={"reason":"loop"}),
"get_openapi":lambda c:c.get_openapi(),
"api_index":lambda c:c.api_index(),
"get_handoff_package_schema":lambda c:c.get_handoff_package_schema(),
"get_data_grants":lambda c:c.get_data_grants(T),
"list_data_stores":lambda c:c.list_data_stores(),
"get_upload_grant":lambda c:c.get_upload_grant("main"),
"get_grant_keys":lambda c:c.get_grant_keys(),
"ingest_external_signal":lambda c:c.ingest_external_signal("github",title="CI failure"),
"get_ops_summary":lambda c:c.get_ops_summary(),
"update_ticket_dependencies":lambda c:c.update_ticket_dependencies(T,"SB-2"),
"list_project_memories":lambda c:c.list_project_memories(),
"record_project_memory":lambda c:c.record_project_memory("Known fix",fix_summary="Restart service"),
"dispatch_ticket_run":lambda c:c.dispatch_ticket_run(T,tier="fast"),
"inspect_agent_run":lambda c:c.inspect_agent_run(T,tokens_used=100),
"request_approval":lambda c:c.request_approval(T,"drop table old","Old copy",scope={"commands":["psql -c 'drop table old'"]}),
"list_approvals":lambda c:c.list_approvals(status="pending",ticket="SB-2"),
"get_approval":lambda c:c.get_approval(T),
"use_approval":lambda c:c.use_approval(T,"drop table old",command="psql -c 'drop table old'"),
"report_approval_result":lambda c:c.report_approval_result(T,T,"succeeded",exit_code=0),
"withdraw_approval":lambda c:c.withdraw_approval(T,"not needed"),
}
class Recorder(Client):
    def __init__(self):
        super().__init__(token="sba_x",base_url="https://sdk.invalid",max_retries=0)
        self.sent=[]
    def _send(self,method,url,headers,payload):
        self.sent.append((method,urllib.parse.urlsplit(url).path))
        return 200,{},b"{}"
def spec_ops():
    with open(SPEC) as f:
        spec=json.load(f)
    out={}
    for path,item in spec["paths"].items():
        for method,op in item.items():
            if method in ("get","post","put","patch","delete"):
                out[(method.upper(),path)]=op.get("operationId")
    return out
def matches(template,path):
    return re.fullmatch(re.sub(r"\\\{\w+\\\}","[^/]+",re.escape(template)),path) is not None
class SyncTest(unittest.TestCase):
    def test_every_spec_operation_has_a_method_and_every_call_is_in_the_spec(self):
        ops=spec_ops()
        self.assertGreaterEqual(len(ops),29)
        table={(m,p) for m,p in OPERATIONS.values()}
        missing=[f"{m} {p} ({oid})" for (m,p),oid in ops.items() if (m,p) not in table]
        self.assertEqual(missing,[],"operations in docs/openapi.json without an SDK method")
        extra=[f"{m} {p}" for m,p in table if (m,p) not in ops]
        self.assertEqual(extra,[],"SDK operations that are not in docs/openapi.json")
        self.assertEqual(set(CALLS),set(OPERATIONS),"every SDK operation needs a call in this test")
        c=Recorder()
        for name,call in CALLS.items():
            self.assertTrue(callable(getattr(Client,name,None)),f"Client.{name} is missing")
            before=len(c.sent)
            call(c)
            sent=c.sent[before:]
            self.assertEqual(len(sent),1,name)
            method,path=sent[0]
            want=OPERATIONS[name]
            self.assertEqual(method,want[0],name)
            self.assertTrue(matches(want[1],path),f"{name} sent {path}, expected {want[1]}")
            self.assertTrue(any(m==method and matches(p,path) for m,p in ops),f"{name} called {method} {path}, which is not in the spec")
        for (m,p),oid in ops.items():
            self.assertIn(oid,OPERATIONS,f"operationId {oid} should be the SDK method name")
            self.assertEqual(OPERATIONS[oid],(m,p))
if __name__=="__main__":
    unittest.main()
