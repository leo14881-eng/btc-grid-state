export const REPO='leo14881-eng/btc-grid-state';
export const WORKFLOW='stock-shadow-position-monitor.yml';
export const API='https://api.github.com';
export class GitHub {
 constructor(token,fetcher=fetch){this.token=token;this.fetcher=fetcher;}
 async dispatch(){
  if(!this.token) throw new Error('GITHUB_TOKEN_MISSING');
  const url=`${API}/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const r=await this.fetcher(url,{method:'POST',redirect:'error',signal:AbortSignal.timeout(10000),
   headers:{Accept:'application/vnd.github+json',Authorization:`Bearer ${this.token}`,'User-Agent':'stock-shadow-independent-heartbeat','X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json'},
   body:JSON.stringify({ref:'main',inputs:{trigger_source:'cloudflare'}})});
  if(!r.ok) throw new Error(`GITHUB_HTTP_${r.status}`);
  return {status:'DISPATCHED',trigger_source:'cloudflare',simulation_only:true};
 }
}
export async function tick(env,fetcher=fetch){
 try{return await new GitHub(env.GITHUB_ACTIONS_TOKEN,fetcher).dispatch();}
 catch(e){return {status:'ERROR',error:/^[A-Z0-9_]+$/.test(e.message)?e.message:'HEARTBEAT_FAILED',simulation_only:true};}
}
export default {
 async scheduled(controller,env,ctx){ctx.waitUntil(tick(env).then(r=>{console.log(JSON.stringify(r));if(r.status==='ERROR')throw new Error('STOCK_HEARTBEAT_FAILED');}));},
 async fetch(){return new Response('Not found',{status:404});}
};