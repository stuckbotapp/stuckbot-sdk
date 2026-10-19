import {Client, FleetAgent} from '../index.js';
const client=new Client({fetch:globalThis.fetch});
const fleet=new FleetAgent({enrollmentSecret:'test'});
void client.request('POST','/api/v1/heartbeat',{data:new Uint8Array([1,2,3]),contentType:'application/octet-stream'});
void fleet;
