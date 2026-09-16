const names={codex:'Codex',claude:'Claude',clinepass:'ClinePass','command-code':'Command Code GOAT',opencode:'OpenCode Go',zai:'Z.ai Coding Plan'};
const labels={five_hour:'5-hour allowance',weekly:'Weekly allowance',monthly:'Monthly allowance',weekly_opus:'Opus weekly',weekly_fable:'Model-specific weekly'};
let mode='used',snapshot=null,online=false,installEvent=null;
const $=id=>document.getElementById(id),node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e};
const pct=x=>Number.isFinite(x)&&x>=0&&x<=100;const stamp=x=>{const n=Date.parse(x);return Number.isFinite(n)?n:null};
const age=x=>{const t=stamp(x);return t===null?Infinity:(Date.now()-t)/1000};
const duration=s=>{if(!Number.isFinite(s))return 'Unknown';if(s<0)return 'Due · awaiting new reading';const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${Math.max(1,m)}m`};
const ZONE=(()=>{try{return Intl.DateTimeFormat().resolvedOptions().timeZone}catch(e){return 'local time'}})();
const clock=t=>new Date(t).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const ago=x=>{const a=age(x);return !Number.isFinite(a)||a<0?'No valid timestamp':a<60?'Observed just now':`Observed ${duration(a)} ago`};
function isFresh(a){return online&&(!snapshot?.captured_at||age(snapshot.captured_at)<=120)&&a.status==='ok'&&age(a.observed_at)>=0&&age(a.observed_at)<=900}
function draw(){if(!snapshot)return;const accounts=Object.keys(names).map(provider=>snapshot.accounts.find(a=>a.provider===provider)||{provider,windows:[],status:'unknown'});$('providers').textContent=accounts.length;$('fresh').textContent=accounts.filter(isFresh).length;$('attention').textContent=accounts.filter(a=>!isFresh(a)||a.windows.some(w=>pct(w.used_percent)&&w.used_percent>=80)).length;if($('needsyou'))$('needsyou').textContent=(snapshot.operator||[]).filter(r=>r&&r.kind&&r.id).length;
let future=accounts.filter(isFresh).flatMap(a=>a.windows.map(w=>stamp(w.resets_at))).filter(t=>t&&t>Date.now());$('next').textContent=future.length?duration((Math.min(...future)-Date.now())/1000):'Unknown';$('cards').replaceChildren();
for(const a of accounts){let fresh=isFresh(a);if($('filter').value==='attention'&&fresh&&!a.windows.some(w=>pct(w.used_percent)&&w.used_percent>=80))continue;const c=node('article',undefined,'card'),top=node('div',undefined,'cardtop');top.append(node('div',names[a.provider].slice(0,1),'monogram'));const info=node('div');info.append(node('div',names[a.provider],'provider'),node('span',fresh?'● Fresh observation':a.status==='ok'?'◷ Last known reading':a.status==='rate_limited'?'◷ Usage check rate-limited · retrying automatically':a.status==='auth_required'?'△ Provider sign-in needs renewal':'△ Reading unavailable','state '+(fresh?'fresh':'warn')));top.append(info);c.append(top);
const windows=[...a.windows];for(const id of ['five_hour','weekly'])if(!windows.some(w=>w.id===id))windows.push({id,used_percent:null});windows.sort((a,b)=>['five_hour','weekly','monthly'].indexOf(a.id)-['five_hour','weekly','monthly'].indexOf(b.id));
for(const w of windows){const reported=pct(w.used_percent),valid=reported&&fresh,v=valid?(mode==='remaining'?100-w.used_percent:w.used_percent):null;const row=node('div',undefined,'window'),head=node('div',undefined,'windowhead');head.append(node('span',labels[w.id]||w.id.replaceAll('_',' ')));let number=node('span',valid?(Math.round(v*10)/10).toLocaleString():'Unknown','number'+(valid?'':' unavailable'));if(valid)number.append(node('small','% '+mode));head.append(number);const bar=node('div',undefined,'bar '+(!valid?'unknown':w.used_percent>=80?'warn':''));if(valid){const fill=node('i');fill.style.width=v+'%';bar.append(fill)}let reset='Reset not supplied';if(stamp(w.resets_at))reset='Resets '+clock(stamp(w.resets_at))+' · in '+duration((stamp(w.resets_at)-Date.now())/1000);else if(w.reset_label)reset='At observation: '+w.reset_label;let detail=valid?(mode==='used'?(100-w.used_percent).toFixed(1)+'% remaining':w.used_percent.toFixed(1)+'% used')+' · '+reset:reported?'Last observed: '+w.used_percent.toFixed(1)+'% used · '+(100-w.used_percent).toFixed(1)+'% remaining · stale':'Usage not reported by this source';if(!valid&&stamp(w.resets_at))detail+=' · '+reset+(w.reset_label?' ('+w.reset_label+')':'');row.append(head,bar,node('div',detail,'reset'));c.append(row)}
let foot=ago(a.observed_at)+(fresh?' · refreshes automatically':' · not current availability');if(pct(a.monthly_remaining)||Number.isFinite(a.monthly_remaining))foot+=' · '+a.monthly_remaining.toFixed(2)+' monthly plan units reported';c.append(node('div',foot,'cardfoot'));$('cards').append(c)}
if($('resets')){$('resets').replaceChildren();const rows=accounts.flatMap(a=>a.windows.filter(w=>stamp(w.resets_at)).map(w=>({provider:names[a.provider]||a.provider,id:labels[w.id]||w.id,t:stamp(w.resets_at),fresh:isFresh(a)}))).sort((a,b)=>a.t-b.t);for(const r of rows){const row=node('div',undefined,'workrow');row.append(node('strong',r.provider+' · '+r.id),node('span',clock(r.t)+' '+ZONE),node('span',(r.t>Date.now()?'in '+duration((r.t-Date.now())/1000):'due')+(r.fresh?'':' · from stale reading')));$('resets').append(row)}if(!rows.length)$('resets').append(node('div','No reset times reported.','empty'))}
if($('zone'))$('zone').textContent='All times in '+ZONE;$('work').replaceChildren();let work=(snapshot.attempts||[]).slice().sort((a,b)=>(stamp(b.at)||0)-(stamp(a.at)||0)).slice(0,8);for(const a of work){const row=node('div',undefined,'workrow');row.append(node('strong',a.task||'Untitled work'),node('span',(names[a.provider]||a.provider||'Unknown provider')+' · '+(a.status||'Unknown status')),node('span',ago(a.at)));$('work').append(row)}if(!work.length)$('work').append(node('div','No recent activity reported by the connected feed.','empty'));if($('evidence')){$('evidence').replaceChildren();const ev=(snapshot.scorecard||[]).slice(0,12);for(const r of ev){const rate=(typeof r.attempts==='number'&&r.attempts>0)?Math.round(100*(r.accepted||0)/r.attempts)+'% acceptance':(r.attempts===0?'no attempts yet':'rate unknown');const row=node('div',undefined,'workrow');row.append(node('strong',(r.family||'?')+' / '+(r.model||'?')),node('span',(r.category||'unrecorded')+' · '+((typeof r.accepted==='number')?r.accepted:0)+' of '+((typeof r.attempts==='number')?r.attempts:0)+' accepted'),node('span',rate));$('evidence').append(row)}if(!ev.length)$('evidence').append(node('div','No model evidence reported yet.','empty'));}if($('needs')){$('needs').replaceChildren();const KINDS={blocked_task:'Board task',held_attempt:'Held attempt',alarm:'Watch alarm',decision:'Your decision',draft:'Drafted fix'};const ops=(snapshot.operator||[]).slice(0,50);for(const o of ops){const row=node('div',undefined,'workrow');row.append(node('strong',(KINDS[o.kind]||o.kind||'Unknown kind')+' · '+(o.id||'Unknown id')),node('span',o.reason||'No reason given'),node('span',stamp(o.since)!==null?'Waiting '+duration(age(o.since)):'Since unknown'));$('needs').append(row)}if(!ops.length)$('needs').append(node('div','Nothing is waiting on you right now.','empty'))}if($('acceptedwork')){$('acceptedwork').replaceChildren();const isoWeek=t=>{const d=new Date(t),dt=new Date(Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),d.getUTCDate())),day=dt.getUTCDay()||7;dt.setUTCDate(dt.getUTCDate()+4-day);return dt.getUTCFullYear()+'-W'+String(Math.ceil(((dt-Date.UTC(dt.getUTCFullYear(),0,1))/86400000+1)/7)).padStart(2,'0')};const weeks={now:isoWeek(Date.now()),last:isoWeek(Date.now()-7*86400000)},byAccount={};for(const r of (snapshot.accepted_work||[]).slice(0,50))if(r&&r.account){const e=byAccount[r.account]||(byAccount[r.account]={}),w=r.week||'?';e[w]={accepted:r.accepted,attempts:r.attempts}}const seen=Object.keys(byAccount).sort();for(const account of seen){const e=byAccount[account],cell=w=>{const c=e[w]||{},a=typeof c.accepted==='number'?c.accepted:0,t=typeof c.attempts==='number'?c.attempts:0;return a+' of '+t+' attempts accepted'};const row=node('div',undefined,'workrow');row.append(node('strong',account),node('span',weeks.now+' · '+cell(weeks.now)),node('span',weeks.last+' · '+cell(weeks.last)));$('acceptedwork').append(row)}if(!seen.length)$('acceptedwork').append(node('div','No accepted work reported yet.','empty'))}if($('reviewerrecall')){$('reviewerrecall').replaceChildren();const rate=n=>typeof n==='number'?Math.round(100*n)+'%':'unknown';const rr=(snapshot.reviewer_recall||[]).slice(0,50);for(const r of rr){const row=node('div',undefined,'workrow');row.append(node('strong',r.lane||'Unknown lane'),node('span','recall '+rate(r.recall)+' · precision '+rate(r.precision)),node('span',(r.run_id||'run unknown')+(r.scored_at?' · scored '+ago(r.scored_at):'')));$('reviewerrecall').append(row)}if(!rr.length)$('reviewerrecall').append(node('div','No reviewer recall reported yet.','empty'))}$('updated').textContent='View updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}
let refreshInFlight=false;
async function refresh(manual=false){
 if(refreshInFlight)return false;
 refreshInFlight=true;
 const controller=new AbortController();
 const timeout=setTimeout(()=>controller.abort(),10000);
 if(manual)$('refreshStatus').textContent='Downloading the latest upload…';
 try{
  const r=await fetch('/api/usage',{cache:'no-store',signal:controller.signal});
  if(r.status===401){$('refreshStatus').textContent='Session expired. Opening sign-in…';location.href='/login';return false}
  if(!r.ok)throw Error('http');
  const next=await r.json();if(!Array.isArray(next.accounts))throw Error('invalid');
  snapshot=next;online=true;
  $('connection').textContent=snapshot.captured_at?(age(snapshot.captured_at)>120?'◷ Mac has not uploaded for '+duration(age(snapshot.captured_at))+' · showing last readings':'● Connected · latest Mac upload '+new Date(snapshot.captured_at).toLocaleTimeString()):'● Connected to your local quota feed';
  draw();return true;
 }catch(error){
  online=false;$('connection').textContent='Could not reach the dashboard · showing last readings';
  if(!snapshot)snapshot={accounts:[],attempts:[]};draw();
  if(manual)$('refreshStatus').textContent=error.name==='AbortError'?'The download timed out after 10 seconds. Tap Refresh to retry.':'Could not reach the dashboard. Check your connection and try again.';
  return false;
 }finally{clearTimeout(timeout);refreshInFlight=false}
}
// Refresh asks the Mac to collect new provider readings through its outbound poll.
// It never launches inference, changes credentials or shortens a provider cooldown.
const REASONS={snapshot_only:'Your Mac uploaded its latest snapshot, but no collector is configured there, so provider readings were not re-collected.',mac_not_reporting:'Your Mac did not pick up the request within 2 minutes. It may be asleep or offline; showing last readings.',collection_timeout:'Collection on your Mac did not finish in time; showing last readings.',collector_error:'The collector on your Mac reported an error; showing last readings.',collector_timeout:'The collector on your Mac timed out; showing last readings.',upload_failed:'Your Mac collected readings but could not upload them; showing last readings.'};
const when=x=>{const t=stamp(x);return t===null?'an unknown time':new Date(t).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})};
function describe(request){
 const o=request.outcome||{},providers=o.providers||[];
 if(request.state==='queued')return 'Request queued · waiting for your Mac to pick it up…';
 if(request.state==='collecting')return 'Your Mac is collecting new provider readings…';
 const notes=[];
 for(const p of providers.filter(p=>p.status==='cooldown'))notes.push((names[p.provider]||p.provider)+' usage check on cooldown'+(p.next_eligible_at?' until '+when(p.next_eligible_at):''));
 for(const p of providers.filter(p=>p.status==='auth_required'))notes.push((names[p.provider]||p.provider)+' sign-in needs renewal on your Mac');
 for(const p of providers.filter(p=>p.status==='error'))notes.push((names[p.provider]||p.provider)+' reading failed');
 const done='Collected at '+when(request.completed_at)+'.';
 if(request.state==='cooldown')return done+' '+notes.join('; ')+'. Refresh cannot shorten a cooldown.';
 if(request.state==='completed')return o.reason==='collected'?done+(notes.length?' '+notes.join('; ')+'.':' New readings loaded.'):REASONS[o.reason]||done;
 return REASONS[o.reason]||'The refresh request failed; showing last readings.';
}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let collectionInFlight=false;
async function follow(request){
 const button=$('refresh'),started=Date.now();
 collectionInFlight=true;button.disabled=true;button.setAttribute('aria-busy','true');
 try{
  while(!['completed','cooldown','failed'].includes(request.state)){
   button.textContent=request.state==='collecting'?'Collecting…':'Queued…';$('refreshStatus').textContent=describe(request);
   if(Date.now()-started>330000){$('refreshStatus').textContent='Still waiting for your Mac. Readings will appear automatically when they arrive.';return}
   await sleep(2000);
   const r=await fetch('/api/refresh?id='+encodeURIComponent(request.id),{cache:'no-store'});
   if(r.status===401){location.href='/login';return}
   if(!r.ok)throw Error('http');
   request=await r.json();
  }
  await refresh(false);
  $('refreshStatus').textContent=describe(request);
 }catch(error){$('refreshStatus').textContent='Lost contact with the dashboard while waiting. Readings will appear automatically when they arrive.'}
 finally{collectionInFlight=false;button.disabled=false;button.textContent='↻ Refresh';button.setAttribute('aria-busy','false')}
}
async function requestCollection(){
 if(collectionInFlight)return;
 $('refreshStatus').textContent='Asking your Mac to collect new readings…';
 try{
  const r=await fetch('/api/refresh',{method:'POST',cache:'no-store'});
  if(r.status===401){$('refreshStatus').textContent='Session expired. Opening sign-in…';location.href='/login';return}
  if(!r.ok)throw Error('http');
  await follow(await r.json());
 }catch(error){$('refreshStatus').textContent='Could not reach the dashboard to request a refresh. Check your connection and try again.'}
}
async function resumeCollection(){
 try{const r=await fetch('/api/refresh',{cache:'no-store'});if(r.status!==200)return;const request=await r.json();if(['queued','collecting'].includes(request.state))await follow(request)}catch(error){}
}
$('refresh').onclick=requestCollection;$('filter').onchange=draw;for(const m of ['remaining','used'])$(m).onclick=()=>{mode=m;$('remaining').setAttribute('aria-pressed',m==='remaining');$('used').setAttribute('aria-pressed',m==='used');draw()};window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installEvent=e});$('install').onclick=async()=>{if(installEvent){await installEvent.prompt();installEvent=null}else $('installHelp').showModal()};$('closeHelp').onclick=()=>$('installHelp').close();window.addEventListener('offline',()=>{online=false;$('connection').textContent='Offline · live readings unavailable';draw()});if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});refresh().then(resumeCollection);setInterval(()=>refresh(),15000);
$('logout').onclick=async()=>{await fetch('/logout',{method:'POST'});location.href='/login'};
