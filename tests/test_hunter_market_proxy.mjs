import fs from 'node:fs';
import assert from 'node:assert/strict';
const source=fs.readFileSync(new URL('../workers/hunter-bybit-proxy.js',import.meta.url),'utf8');
const {default:worker}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const calls=[];let active=0;let peak=0;
globalThis.fetch=async (url,options)=>{
 calls.push(String(url));active++;peak=Math.max(peak,active);
 await new Promise(r=>setTimeout(r,1));active--;
 const u=new URL(url);const symbol=u.searchParams.get('symbol');
 const result=u.pathname.endsWith('/orderbook')?{s:symbol,ts:Date.now(),b:[['99','100']],a:[['101','100']]}:{category:'spot',symbol,list:[['0','100','101','99','100','1','100']]};
 return Response.json({retCode:0,time:Date.now(),result});
};
const send=(path,options={})=>worker.fetch(new Request('https://worker.example'+path,options),{});
const health=await (await send('/health')).json();assert.equal(health.version,3);
assert(health.capabilities.includes('orderbooks'));assert(health.capabilities.includes('klines'));
const symbols=Array.from({length:20},(_,i)=>'ALT'+i+'USDT');
const batch=await (await send('/bybit/orderbooks',{method:'POST',body:JSON.stringify({symbols})})).json();
assert.equal(Object.keys(batch.result.books).length,20);assert.equal(calls.length,20);assert(peak<=4);
assert.equal(batch.result.books.ALT0USDT.result.category,'spot');
assert(calls.every(url=>url.startsWith('https://api.bybit.com/v5/market/orderbook?')));
for(const body of [{symbols:[...symbols,'EXTRAUSDT']},{symbols:['BTCUSDT','BTCUSDT']},{symbols:['http://evil']},{symbols:['BTCUSDT'],url:'https://evil'}]) {
 assert.equal((await send('/bybit/orderbooks',{method:'POST',body:JSON.stringify(body)})).status,400);
}
assert.equal((await send('/bybit/orderbooks')).status,405);
assert.equal((await send('/bybit/klines?symbol=BTCUSDT&interval=5&limit=1000&start=1000&end=2000')).status,200);
for(const path of ['/bybit/klines?symbol=BTCUSDT&interval=5&limit=1001','/bybit/klines?symbol=BTCUSDT&interval=1','/bybit/klines?symbol=BTCUSDT&interval=5&start=2000&end=1000','/bybit/klines?symbol=BTCUSDT&interval=5&end='+String(Date.now()+100000)])assert.equal((await send(path)).status,400);
globalThis.fetch=async()=>{throw new Error('connection failed');};
const partial=await (await send('/bybit/orderbooks',{method:'POST',body:JSON.stringify({symbols:['BTCUSDT']})})).json();
assert.equal(Object.keys(partial.result.books).length,0);assert(partial.result.failures.BTCUSDT);
console.log('New Worker books/history: validation, bounded concurrency, fixed upstream and connection failure passed');
