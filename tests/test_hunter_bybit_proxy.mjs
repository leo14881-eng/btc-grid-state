import fs from 'node:fs';
import assert from 'node:assert/strict';
import './helpers/cloudflare-webcrypto.mjs';
const source=fs.readFileSync(new URL('../workers/hunter-bybit-proxy.js',import.meta.url),'utf8');
const {default:worker}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
let calls=[]; let active=0; let peak=0;
globalThis.fetch=async (url,options={})=>{
  calls.push({url:String(url),options});active++;peak=Math.max(peak,active);
  await new Promise(resolve=>setTimeout(resolve,1)); active--;
  const u=new URL(url);
  if(u.pathname.includes('/alpha/')) return Response.json({retCode:0,result:{list:[]}});
  const symbol=u.searchParams.get('symbol');const limit=Number(u.searchParams.get('limit'));
  return Response.json({retCode:0,time:Date.now(),result:{category:'spot',symbol,list:Array.from({length:limit||1},()=>['0','10','11','9','10','1','10'])}});
};
const send=(path,options={})=>worker.fetch(new Request('https://worker.example'+path,options),{HUNTER_PROXY_TOKEN:'test-token'});
assert.equal((await send('/bybit/spot')).status,200);
assert.equal(new URL(calls.at(-1).url).searchParams.has('symbol'),false);
assert.equal((await send('/bybit/spot?symbol=btcusdt')).status,200);
assert.equal(new URL(calls.at(-1).url).searchParams.get('symbol'),'BTCUSDT');
assert.equal((await send('/bybit/spot?symbol=')).status,400);
assert.equal((await send('/bybit/tickers')).status,200);
assert.equal(new URL(calls.at(-1).url).pathname,'/v5/market/tickers');
calls=[];
const end=Date.now();const symbols=Array.from({length:20},(_,i)=>'ALT'+i+'USDT');
const r=await send('/bybit/early-klines',{method:'POST',body:JSON.stringify({symbols,end})});
assert.equal(r.status,200);const data=await r.json();
assert.equal(calls.length,40);assert(peak<=4);assert.equal(Object.keys(data.result.klines).length,20);
assert(calls.every(c=>new URL(c.url).searchParams.get('end')===String(end)));
for(const body of [{symbols:[...symbols,'EXTRAUSDT'],end},{symbols:['BTCUSDT','BTCUSDT'],end},{symbols:['http://evil'],end},{symbols:['BTCUSDT'],end:0}]) {
  assert.equal((await send('/bybit/early-klines',{method:'POST',body:JSON.stringify(body)})).status,400);
}
assert.equal((await send('/bybit/early-klines')).status,405);
assert.equal((await send('/bybit/alpha/token-list',{method:'POST',body:'{"tokenTag":0}'})).status,401);
const signedBody='{"tokenTag": 0}';
assert.equal((await send('/bybit/alpha/token-list',{method:'POST',body:signedBody,headers:{'X-Hunter-Proxy-Token':'test-token','X-BAPI-API-KEY':'test-key','X-BAPI-TIMESTAMP':'1','X-BAPI-RECV-WINDOW':'5000','X-BAPI-SIGN':'test-sign'}})).status,200);
assert.equal(calls.at(-1).options.body,signedBody);
assert.equal((await send('/unknown')).status,404);
console.log('Worker routes, batching, concurrency, validation and Alpha byte preservation passed');
