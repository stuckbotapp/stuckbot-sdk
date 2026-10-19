import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {FleetAgent} from '../index.js';
const opts={baseUrl:'http://127.0.0.1:12345',fetch:async()=>new Response(JSON.stringify({agent:{id:'old-agent'}}),{status:200})};
async function original(extra={}){
const state=join(await mkdtemp(join(tmpdir(),'state-config-')),'state.json');
const a=new FleetAgent({...opts,enrollmentSecret:'sbe_original',stateFile:state,...extra});
a._stopped=true;
await a._grant({token:'sba_saved',agent:{id:'old-agent'},expires_at:new Date(Date.now()+3600000).toISOString()});
return state;
}
test('changed connector, scopes and projects do not reuse saved identity',async()=>{
for(const changes of [{enrollmentSecret:'sbe_another_org'},{scopes:['signals:report']},{projectIds:['new-project']}]){
const state=await original();
const b=new FleetAgent({...opts,enrollmentSecret:'sbe_original',stateFile:state,...changes});
assert.equal(await b._resume(),false);
}
});
test('same security config and reordered scope lists resume',async()=>{
const state=await original({scopes:['tickets:read_own','signals:report']});
const b=new FleetAgent({...opts,enrollmentSecret:'sbe_original',stateFile:state,scopes:['signals:report','tickets:read_own']});
assert.equal(await b._resume(),true);assert.equal(b.agentId,'old-agent');
});
test('corrupted saved state is ignored without raw TypeError',async()=>{
const state=await original();
for(const value of [null,[],42,'text',{expires_at:23}]){
await writeFile(state,JSON.stringify(value));
const b=new FleetAgent({...opts,enrollmentSecret:'sbe_original',stateFile:state});
assert.equal(await b._resume(),false);
}
});
