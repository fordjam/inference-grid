const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict'),path=require('node:path');
class El{constructor(){this.children=[];this.style={};this.value='all';this.textContent=''}append(...x){this.children.push(...x)}replaceChildren(...x){this.children=x}setAttribute(){} }
const els={},timers=new Map();let id=0,calls=[];
const now=()=>new Date().toISOString();
let snapshot={captured_at:now(),accounts:[]};
// Scripted server: a queue of refresh-request states returned on successive polls.
let states=[],requests=0,usage=async()=>({ok:true,status:200,json:async()=>snapshot}),refreshPost=null;
const request=async(url,opts={})=>{
 calls.push((opts.method||'GET')+' '+url);
 if(url==='/api/usage')return usage();
 if(url==='/api/refresh'&&opts.method==='POST'){if(refreshPost)return refreshPost();requests++;return {ok:true,status:202,json:async()=>states.shift()}}
 if(url.startsWith('/api/refresh?id=')||url==='/api/refresh')return {ok:true,status:200,json:async()=>states.length>1?states.shift():states[0]};
 throw Error('unexpected '+url);
};
const context={document:{getElementById:k=>els[k]??=new El(),createElement:()=>new El()},window:{addEventListener(){}},navigator:{},location:{},fetch:(...a)=>request(...a),setInterval(){},setTimeout:(fn,ms)=>{timers.set(++id,{fn,ms});return id},clearTimeout:n=>timers.delete(n),AbortController,Date,Number,Math,encodeURIComponent};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'web/app.js'),'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
// Drive the page: fire poll sleeps (short timers) but leave the 10s abort timers alone.
async function settle(until,limit=60){for(let i=0;i<limit;i++){await tick();await tick();if(until())return;for(const [k,t] of [...timers]){if(t.ms<10000){timers.delete(k);t.fn()}}}throw Error('did not settle: '+els.refreshStatus.textContent)}
const drive=async(start)=>{let done=false;const p=start().then(()=>{done=true});await settle(()=>done);await p};
const tap=async(pattern)=>{await drive(()=>els.refresh.onclick());assert.match(els.refreshStatus.textContent,pattern)};
const seq=(rid,...tail)=>tail.map(([state,extra])=>({id:rid,state,requested_at:1,claimed_at:null,completed_at:extra?now():null,outcome:extra||null}));
(async()=>{
 await settle(()=>calls.length>=2);// initial snapshot download + resume check
 assert.deepEqual(calls,['GET /api/usage','GET /api/refresh']);
 // 1. Full lifecycle: queued -> collecting -> completed with new readings.
 states=seq('a'.repeat(16),['queued'],['collecting'],['completed',{state:'completed',reason:'collected',collected:true,providers:[{provider:'claude',status:'ok',next_eligible_at:null}]}]);
 snapshot={captured_at:now(),accounts:[{provider:'claude',status:'ok',observed_at:now(),windows:[]}]};
 const click=els.refresh.onclick();
 await settle(()=>els.refreshStatus.textContent.includes('waiting for your Mac'));assert.equal(els.refresh.disabled,true);assert.equal(els.refresh.textContent,'Queued…');
 await settle(()=>els.refreshStatus.textContent.includes('collecting new provider readings'));assert.equal(els.refresh.textContent,'Collecting…');
 await settle(()=>els.refreshStatus.textContent.includes('New readings loaded'));await click;
 assert.equal(els.refresh.disabled,false);assert.equal(els.refresh.textContent,'↻ Refresh');assert.equal(requests,1);
 assert.equal(calls.filter(c=>c==='GET /api/usage').length,2,'snapshot re-downloaded once after completion');
 // 2. Concurrent taps while a request is open do not create another request.
 states=seq('b'.repeat(16),['queued'],['queued'],['completed',{state:'completed',reason:'collected',collected:true,providers:[]}]);
 const first=els.refresh.onclick();await tick();await els.refresh.onclick();await settle(()=>els.refreshStatus.textContent.includes('Collected at'));await first;assert.equal(requests,2);
 // 3. Provider cooldown is shown with its next eligible time and cannot be bypassed.
 const until=new Date(Date.now()+3600e3).toISOString();
 states=seq('c'.repeat(16),['collecting'],['cooldown',{state:'cooldown',reason:'collected',collected:true,providers:[{provider:'claude',status:'cooldown',next_eligible_at:until},{provider:'codex',status:'auth_required',next_eligible_at:null}]}]);
 await tap(/Claude usage check on cooldown until \d/);assert.match(els.refreshStatus.textContent,/Codex sign-in needs renewal/);assert.match(els.refreshStatus.textContent,/cannot shorten a cooldown/);
 // 4. Offline Mac: the server expires the queued request.
 states=seq('d'.repeat(16),['queued'],['failed',{state:'failed',reason:'mac_not_reporting',collected:false,providers:[]}]);
 await tap(/did not pick up the request within 2 minutes/);assert.equal(els.refresh.disabled,false);
 // 5. Snapshot-only completion is distinguished from real collection.
 states=seq('e'.repeat(16),['completed',{state:'completed',reason:'snapshot_only',collected:false,providers:[]}]);
 await tap(/no collector is configured/);
 // 6. Request refused / network failure / expired session.
 refreshPost=async()=>({ok:false,status:403});await els.refresh.onclick();assert.match(els.refreshStatus.textContent,/Could not reach the dashboard to request/);
 refreshPost=()=>Promise.reject(Error('network'));await els.refresh.onclick();assert.match(els.refreshStatus.textContent,/Could not reach the dashboard to request/);assert.equal(els.refresh.disabled,false);
 refreshPost=async()=>({status:401});await els.refresh.onclick();assert.equal(context.location.href,'/login');
 // 7. Reload while a request is open resumes following it.
 context.location.href=undefined;refreshPost=null;states=seq('f'.repeat(16),['collecting'],['completed',{state:'completed',reason:'collected',collected:true,providers:[]}]);
 await drive(()=>vm.runInContext('resumeCollection()',context));assert.match(els.refreshStatus.textContent,/Collected at/);
 // 8. Background snapshot download timeout does not touch the refresh status.
 const before=els.refreshStatus.textContent;usage=(_u,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Object.assign(Error('timeout'),{name:'AbortError'}))));
 const pending=vm.runInContext('refresh()',context);for(const [k,t] of [...timers]){timers.delete(k);t.fn()}await pending;assert.equal(els.refreshStatus.textContent,before);assert.match(els.connection.textContent,/Could not reach the dashboard/);
 console.log('PASS: lifecycle states, coalesced taps, cooldown display, offline Mac, snapshot-only, refusal/network/session, resume after reload, background timeout');
})().catch(e=>{console.error(e);process.exitCode=1});
