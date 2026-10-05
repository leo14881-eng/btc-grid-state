export const REPO='leo14881-eng/btc-grid-state';
export const WORKFLOW='hunter-position-monitor.yml';
export const API='https://api.github.com';
export const HEALTH='research/results/hunter-scheduler-health.json';
export function expectedGeneration(now){
 return new Date(Math.floor(now.getTime()/300000)*300000).toISOString().replace('.000Z','Z');
}
export function completedGeneration(health,now){
 const expected=expectedGeneration(now);
 const end=Date.parse(health?.monitor_completed_at_utc);
 const start=Math.floor(now.getTime()/300000)*300000;
 return health?.schema==='hunter_scheduler_health_v1' &&
  health?.last_successful_monitor_generation_id===expected &&
  health?.current_generation_id===expected &&
  health?.health==='HEALTHY' && health?.last_failure==null &&
  health?.shadow_only===true && health?.real_order_count===0 &&
  Number.isFinite(end) && end>=start && end<=now.getTime();
}
export class GitHub {
 constructor(token,fetcher=fetch){this.token=token;this.fetcher=fetcher;}
 headers(){return {Accept:'application/vnd.github+json',Authorization:`Bearer ${this.token}`,'User-Agent':'hunter-independent-heartbeat','X-GitHub-Api-Version':'2022-11-28'};}
 async health(){
  const r=await this.fetcher(`${API}/repos/${REPO}/contents/${HEALTH}?ref=main`,{
   redirect:'error',signal:AbortSignal.timeout(10000),
   headers:{...this.headers(),Accept:'application/vnd.github.raw+json','Cache-Control':'no-cache'}});
  if(!r.ok)throw new Error(`HEALTH_HTTP_${r.status}`);
  return await r.json();
 }
 async dispatch(){
  if(!this.token)throw new Error('GITHUB_TOKEN_MISSING');
  const r=await this.fetcher(`${API}/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,{
   method:'POST',redirect:'error',signal:AbortSignal.timeout(10000),
   headers:{...this.headers(),'Content-Type':'application/json'},
   body:JSON.stringify({ref:'main',inputs:{trigger_source:'cloudflare'}})});
  if(!r.ok)throw new Error(`GITHUB_HTTP_${r.status}`);
  return {status:'DISPATCHED',trigger_source:'cloudflare',shadow_only:true};
 }
}
export async function tick(env,fetcher=fetch,now=new Date()){
 try{
  if(!env.GITHUB_ACTIONS_TOKEN)throw new Error('GITHUB_TOKEN_MISSING');
  const github=new GitHub(env.GITHUB_ACTIONS_TOKEN,fetcher);
  let health;
  try{health=await github.health();}catch{/* Missing/unreadable health activates backup. */}
  if(completedGeneration(health,now))return {
   status:'SKIPPED',reason:'GENERATION_ALREADY_COMPLETED',generation_id:expectedGeneration(now),shadow_only:true};
  return await github.dispatch();
 }catch(e){return {status:'ERROR',error:/^[A-Z0-9_]+$/.test(e.message)?e.message:'HEARTBEAT_FAILED',shadow_only:true};}
}
export default {
 async scheduled(controller,env,ctx){
  ctx.waitUntil(tick(env).then(r=>{console.log(JSON.stringify(r));if(r.status==='ERROR')throw new Error('HUNTER_HEARTBEAT_FAILED');}));
 },
 async fetch(){return new Response('Not found',{status:404});}
};
