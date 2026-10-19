import json
import os
import tempfile
import time
import unittest
from mock_server import Mock,iso
from stuckbot import FleetAgent
from stuckbot.fleet import parse_time
class StateIdentityTest(unittest.TestCase):
    def setUp(self):
        self.m=Mock()
        self.state=os.path.join(tempfile.mkdtemp(prefix='state-identity-'),'agent.json')
        self.m.on('GET','/api/v1/agents/me',(200,{}, {'agent':{'id':'old-agent'}}))
    def tearDown(self):
        self.m.close()
    def original(self,**kw):
        a=FleetAgent('sbe_original',base_url=self.m.url,state_file=self.state,**kw)
        a._grant({'token':'sba_saved','agent':{'id':'old-agent'},'expires_at':iso(time.time()+3600)})
        return a
    def test_changed_connector_does_not_resume_old_identity(self):
        self.original()
        b=FleetAgent('sbe_another_org',base_url=self.m.url,state_file=self.state)
        self.assertFalse(b._resume())
        self.assertEqual(self.m.calls('GET','/api/v1/agents/me'),[])
    def test_narrowed_scopes_do_not_reuse_broad_saved_identity(self):
        self.original()
        b=FleetAgent('sbe_original',base_url=self.m.url,state_file=self.state,scopes=['signals:report'])
        self.assertFalse(b._resume())
    def test_changed_projects_do_not_reuse_saved_identity(self):
        self.original(project_ids=['old-project'])
        b=FleetAgent('sbe_original',base_url=self.m.url,state_file=self.state,project_ids=['new-project'])
        self.assertFalse(b._resume())
    def test_same_config_and_reordered_scopes_resume(self):
        self.original(scopes=['tickets:read_own','signals:report'])
        b=FleetAgent('sbe_original',base_url=self.m.url,state_file=self.state,scopes=['signals:report','tickets:read_own'])
        self.assertTrue(b._resume())
        self.assertEqual(b.agent_id,'old-agent')
    def test_corrupted_shapes_and_dates_do_not_crash(self):
        self.original()
        with open(self.state) as f:
            base=json.load(f)
        self.assertIsNone(parse_time('2026-99-99T00:00:00Z'))
        self.assertIsNone(parse_time(23))
        for value in [None,[],42,'text',{'expires_at':23},{'expires_at':'2026-99-99T00:00:00Z'}]:
            with open(self.state,'w') as f:
                json.dump({**base,**value} if isinstance(value,dict) else value,f)
            a=FleetAgent('sbe_original',base_url=self.m.url,state_file=self.state)
            self.assertFalse(a._resume())
if __name__=='__main__':
    unittest.main()
