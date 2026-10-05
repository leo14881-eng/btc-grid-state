import test from 'node:test';
import assert from 'node:assert/strict';
import worker,{GitHub,tick,REPO,WORKFLOW} from './worker.mjs';

test('dispatch is fixed to Hunter monitor on main with cloudflare source',async()=>{
 const calls=[];const f=async(url,opt)=>{calls.push({url,opt});return new Response(null,{status:204});};
 const r=await new GitHub('secret',f).dispatch();
 assert.equal(r.status,'DISPATCHED');assert.equal(calls.length,1);
 assert.equal(calls[0].url,`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`);
 assert.deepEqual(JSON.parse(calls[0].opt.body),{ref:'main',inputs:{trigger_source:'cloudflare'}});
 assert.equal(calls[0].opt.headers.Authorization,'Bearer secret');
});
test('missing token fails closed without network',async()=>{
 let calls=0;const r=await tick({},async()=>{calls++;});
 assert.equal(r.status,'ERROR');assert.equal(r.error,'GITHUB_TOKEN_MISSING');assert.equal(calls,0);
});
test('HTTP errors are redacted and never treated as success',async()=>{
 const r=await tick({GITHUB_ACTIONS_TOKEN:'x'},async()=>new Response('private',{status:403}));
 assert.deepEqual(r,{status:'ERROR',error:'GITHUB_HTTP_403',shadow_only:true});
});
test('unexpected errors do not leak secrets',async()=>{
 const r=await tick({GITHUB_ACTIONS_TOKEN:'x'},async()=>{throw new Error('https://secret.invalid/token=x');});
 assert.equal(r.error,'HEARTBEAT_FAILED');
});
test('public HTTP cannot trigger heartbeat',async()=>{
 const r=await worker.fetch(new Request('https://worker.invalid/tick',{method:'POST'}),{});
 assert.equal(r.status,404);
});
