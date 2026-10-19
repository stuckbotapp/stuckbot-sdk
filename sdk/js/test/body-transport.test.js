import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Client,StuckBotError} from '../index.js';
test('body stream failure retries and exhaustion has a structured network error',async()=>{
let calls=[];
const client=new Client({baseUrl:'http://127.0.0.1:12345',backoff:0,maxRetries:1,fetch:async(url,options)=>{
calls.push(options.headers['Idempotency-Key']);
if(calls.length===1){
return new Response(new ReadableStream({start(controller){controller.enqueue(new TextEncoder().encode('{'));controller.error(new Error('connection reset after headers'));}}),{status:200});
}
return new Response('{"ok":true}');
}});
assert.deepEqual(await client.request('POST','/test',{body:{x:1}}),{ok:true});
assert.equal(calls.length,2);assert.equal(calls[0],calls[1]);
client.fetch=async()=>new Response(new ReadableStream({start(controller){controller.error(new Error('connection reset'));}}),{status:503});
await assert.rejects(client.request('GET','/test'),e=>e instanceof StuckBotError&&e.status===0&&e.code==='network_error');
});
