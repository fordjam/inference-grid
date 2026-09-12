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
function isFresh(a){return online&&a.status==='ok'&&age(a.observed_at)>=0&&age(a.observed_at)<=900}
function draw(){if(!snapshot)return;const accounts=Object.keys(names).map(provider=>snapshot.accounts.find(a=>a.provider===provider)||{provider,windows:[],status:'unknown'});$('providers').textContent=accounts.length;$('fresh').textContent=accounts.filter(isFresh).length;$('attention').textContent=accounts.filter(a=>!isFresh(a)||a.windows.some(w=>pct(w.used_percent)&&w.used_percent>=80)).length;
let future=accounts.filter(isFresh).flatMap(a=>a.windows.map(w=>stamp(w.resets_at))).filter(t=>t&&t>Date.now());$('next').textContent=future.length?duration((Math.min(...future)-Date.now())/1000):'Unknown';$('cards').replaceChildren();
for(const a of accounts){let fresh=isFresh(a);if($('filter').value==='attention'&&fresh&&!a.windows.some(w=>pct(w.used_percent)&&w.used_percent>=80))continue;const c=node('article',undefined,'card'),top=node('div',undefined,'cardtop');top.append(node('div',names[a.provider].slice(0,1),'monogram'));const info=node('div');info.append(node('div',names[a.provider],'provider'),node('span',fresh?'● Fresh observation':a.status==='ok'?'◷ Last known reading':a.status==='rate_limited'?'◷ Usage check rate-limited · retrying automatically':a.status==='auth_required'?'△ Provider sign-in needs renewal':'△ Reading unavailable','state '+(fresh?'fresh':'warn')));top.append(info);c.append(top);
const windows=[...a.windows];for(const id of ['five_hour','weekly'])if(!windows.some(w=>w.id===id))windows.push({id,used_percent:null});windows.sort((a,b)=>['five_hour','weekly','monthly'].indexOf(a.id)-['five_hour','weekly','monthly'].indexOf(b.id));
for(const w of windows){const reported=pct(w.used_percent),valid=reported&&fresh,v=valid?(mode==='remaining'?100-w.used_percent:w.used_percent):null;const row=node('div',undefined,'window'),head=node('div',undefined,'windowhead');head.append(node('span',labels[w.id]||w.id.replaceAll('_',' ')));let number=node('span',valid?(Math.round(v*10)/10).toLocaleString():'Unknown','number'+(valid?'':' unavailable'));if(valid)number.append(node('small','% '+mode));head.append(number);const bar=node('div',undefined,'bar '+(!valid?'unknown':w.used_percent>=80?'warn':''));if(valid){const fill=node('i');fill.style.width=v+'%';bar.append(fill)}let reset='Reset not supplied';if(stamp(w.resets_at))reset='Resets '+clock(stamp(w.resets_at))+' · in '+duration((stamp(w.resets_at)-Date.now())/1000);else if(w.reset_label)reset='At observation: '+w.reset_label;const detail=valid?(mode==='used'?(100-w.used_percent).toFixed(1)+'% remaining':w.used_percent.toFixed(1)+'% used')+' · '+reset:reported?'Last observed: '+w.used_percent.toFixed(1)+'% used · '+(100-w.used_percent).toFixed(1)+'% remaining · stale':'Not reported by this source';row.append(head,bar,node('div',detail,'reset'));c.append(row)}
let foot=ago(a.observed_at)+(fresh?' · refreshes automatically':' · not current availability');if(pct(a.monthly_remaining)||Number.isFinite(a.monthly_remaining))foot+=' · '+a.monthly_remaining.toFixed(2)+' monthly plan units reported';c.append(node('div',foot,'cardfoot'));$('cards').append(c)}
if($('resets')){$('resets').replaceChildren();const rows=accounts.flatMap(a=>a.windows.filter(w=>stamp(w.resets_at)).map(w=>({provider:names[a.provider]||a.provider,id:labels[w.id]||w.id,t:stamp(w.resets_at),fresh:isFresh(a)}))).sort((a,b)=>a.t-b.t);for(const r of rows){const row=node('div',undefined,'workrow');row.append(node('strong',r.provider+' · '+r.id),node('span',clock(r.t)+' '+ZONE),node('span',(r.t>Date.now()?'in '+duration((r.t-Date.now())/1000):'due')+(r.fresh?'':' · from stale reading')));$('resets').append(row)}if(!rows.length)$('resets').append(node('div','No reset times reported.','empty'))}
if($('zone'))$('zone').textContent='All times in '+ZONE;$('work').replaceChildren();let work=(snapshot.attempts||[]).slice().sort((a,b)=>(stamp(b.at)||0)-(stamp(a.at)||0)).slice(0,8);for(const a of work){const row=node('div',undefined,'workrow');row.append(node('strong',a.task||'Untitled work'),node('span',(names[a.provider]||a.provider||'Unknown provider')+' · '+(a.status||'Unknown status')),node('span',ago(a.at)));$('work').append(row)}if(!work.length)$('work').append(node('div','No recent activity reported by the connected feed.','empty'));$('updated').textContent='View updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}
let refreshInFlight=false;
async function refresh(manual=false){
 if(refreshInFlight)return;
 refreshInFlight=true;
 const button=$('refresh'),controller=new AbortController();
 const timeout=setTimeout(()=>controller.abort(),10000);
 button.disabled=true;button.textContent='Checking…';button.setAttribute('aria-busy','true');
 if(manual)$('refreshStatus').textContent='Checking for a newer upload…';
 try{
  const previous=JSON.stringify(snapshot?.accounts||null),previousCapture=snapshot?.captured_at;
  const r=await fetch('/api/usage',{cache:'no-store',signal:controller.signal});
  if(r.status===401){$('refreshStatus').textContent='Session expired. Opening sign-in…';location.href='/login';return}
  if(!r.ok)throw Error('http');
  const next=await r.json();if(!Array.isArray(next.accounts))throw Error('invalid');
  snapshot=next;online=true;
  $('connection').textContent=snapshot.captured_at?(age(snapshot.captured_at)>120?'◷ Mac has not uploaded for '+duration(age(snapshot.captured_at))+' · showing last readings':'● Connected · latest Mac upload '+new Date(snapshot.captured_at).toLocaleTimeString()):'● Connected to your local quota feed';
  draw();
  if(manual){
   const changed=previous!==JSON.stringify(snapshot.accounts);
   const uploaded=previousCapture!==snapshot.captured_at;
   $('refreshStatus').textContent=(changed?'Latest readings loaded.':uploaded?'New upload received; provider readings are unchanged.':'No newer readings yet.')+' Checked '+new Date().toLocaleTimeString()+'. Provider checks run separately; rate-limit cooldowns still apply.';
  }
 }catch(error){
  online=false;$('connection').textContent='Could not reach the dashboard · showing last readings';
  if(!snapshot)snapshot={accounts:[],attempts:[]};draw();
  $('refreshStatus').textContent=error.name==='AbortError'?'The check timed out after 10 seconds. Tap Refresh to retry.':'Refresh failed. Check your connection and try again.';
 }finally{
  clearTimeout(timeout);refreshInFlight=false;button.disabled=false;button.textContent='↻ Refresh';button.setAttribute('aria-busy','false');
 }
}
$('refresh').onclick=()=>refresh(true);$('filter').onchange=draw;for(const m of ['remaining','used'])$(m).onclick=()=>{mode=m;$('remaining').setAttribute('aria-pressed',m==='remaining');$('used').setAttribute('aria-pressed',m==='used');draw()};window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installEvent=e});$('install').onclick=async()=>{if(installEvent){await installEvent.prompt();installEvent=null}else $('installHelp').showModal()};$('closeHelp').onclick=()=>$('installHelp').close();window.addEventListener('offline',()=>{online=false;$('connection').textContent='Offline · live readings unavailable';draw()});if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});refresh();setInterval(refresh,15000);