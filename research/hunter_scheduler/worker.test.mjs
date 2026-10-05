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

test('completed current generation skips POST; prior, invalid and absent state dispatch',async()=>{
 const now=new Date('2026-10-05T23:42:00Z');
 const good={schema:'hunter_scheduler_health_v1',current_generation_id:'2026-10-05T23:40:00Z',
  last_successful_monitor_generation_id:'2026-10-05T23:40:00Z',health:'HEALTHY',last_failure:null,
  shadow_only:true,real_order_count:0,monitor_completed_at_utc:'2026-10-05T23:40:46Z'};
 const cases=[
  [good,'SKIPPED'],
  [{...good,last_successful_monitor_generation_id:'2026-10-05T23:35:00Z'},'DISPATCHED'],
  [{...good,health:'CRITICAL'},'DISPATCHED'],
  [{...good,last_failure:'FAIL'},'DISPATCHED'],
  [{...good,monitor_completed_at_utc:'2026-10-05T23:45:00Z'},'DISPATCHED'],
  [{...good,real_order_count:1},'DISPATCHED'],
  [{...good,shadow_only:false},'DISPATCHED'],
  [{...good,schema:'wrong'},'DISPATCHED'],
  [{},'DISPATCHED']
 ];
 for(const [doc,status] of cases){
  const calls=[];
  const f=async(url,opt)=>{calls.push({url,opt});return opt.method==='POST'
   ?new Response(null,{status:204}):new Response(JSON.stringify(doc),{status:200});};
  const result=await tick({GITHUB_ACTIONS_TOKEN:'x'},f,now);
  assert.equal(result.status,status);
  assert.equal(calls.filter(x=>x.opt.method==='POST').length,status==='SKIPPED'?0:1);
  assert.ok(calls[0].url.endsWith('hunter-scheduler-health.json?ref=main'));
  assert.equal(calls[0].opt.headers['Cache-Control'],'no-cache');
 }
});
test('missing, malformed or timed out health still activates backup once',async()=>{
 for(const failure of ['404','invalid','timeout']){
  let posts=0;
  const f=async(url,opt)=>{
   if(opt.method==='POST'){posts++;return new Response(null,{status:204});}
   if(failure==='404')return new Response('',{status:404});
   if(failure==='invalid')return new Response('not JSON',{status:200});
   throw new Error('secret timeout detail');
  };
  assert.equal((await tick({GITHUB_ACTIONS_TOKEN:'x'},f,new Date('2026-10-05T23:42:00Z'))).status,'DISPATCHED');
  assert.equal(posts,1);
 }
});
