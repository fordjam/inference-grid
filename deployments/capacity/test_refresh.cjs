const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict'),path=require('node:path');
class El{constructor(){this.children=[];this.style={};this.value='all';this.textContent=''}append(...x){this.children.push(...x)}replaceChildren(...x){this.children=x}setAttribute(){} }
const els={},timers=new Map();let id=0,calls=0;
const sample={captured_at:new Date().toISOString(),accounts:[]};
let request=async()=>({ok:true,status:200,json:async()=>sample});
const context={document:{getElementById:k=>els[k]??=new El(),createElement:()=>new El()},window:{addEventListener(){}},navigator:{},location:{},fetch:(...args)=>{calls++;return request(...args)},setInterval(){},setTimeout:fn=>{timers.set(++id,fn);return id},clearTimeout:n=>timers.delete(n),AbortController,Date,Number,Math};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'web/app.js'),'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await tick();
 await els.refresh.onclick();assert.match(els.refreshStatus.textContent,/No newer readings/);assert.equal(els.refresh.disabled,false);
 request=async()=>({ok:true,status:200,json:async()=>({...sample,accounts:[{provider:'opencode',status:'ok',observed_at:new Date().toISOString(),windows:[]}]})});
 await els.refresh.onclick();assert.match(els.refreshStatus.textContent,/Latest readings loaded/);
 request=()=>Promise.reject(Error('network'));
 await els.refresh.onclick();assert.match(els.refreshStatus.textContent,/Refresh failed/);assert.equal(els.refresh.disabled,false);
 request=(_url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Object.assign(Error('timeout'),{name:'AbortError'}))));
 const pending=els.refresh.onclick(),count=calls;await els.refresh.onclick();assert.equal(calls,count);assert.equal(els.refresh.disabled,true);
 for(const fn of timers.values())fn();await pending;assert.match(els.refreshStatus.textContent,/timed out/);assert.equal(els.refresh.disabled,false);
 request=async()=>({status:401});await els.refresh.onclick();assert.equal(context.location.href,'/login');
 console.log('PASS: new/unchanged data, failure, timeout, duplicate suppression, expired session');
})().catch(e=>{console.error(e);process.exitCode=1});
