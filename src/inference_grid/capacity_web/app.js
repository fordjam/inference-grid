const names={codex:'Codex',claude:'Claude',clinepass:'ClinePass','command-code':'Command Code GOAT',opencode:'OpenCode Go',zai:'Z.ai Coding Plan'};
const labels={five_hour:'5-hour allowance',weekly:'Weekly allowance',monthly:'Monthly allowance',weekly_opus:'Opus weekly',weekly_fable:'Model-specific weekly'};
let mode='used',snapshot=null,online=false,installEvent=null;
const STALE_SECONDS=86400;// 02-A1: a held attempt or land request waiting this long shows red
const STALE_KINDS=new Set(['held_attempt','land_request','blocked_task','decision']);
const COLLECTOR_ALARM_SECONDS=1800;// 02-A1/C3: a collector observation older than this alarms
const $=id=>document.getElementById(id),node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e};
const pct=x=>Number.isFinite(x)&&x>=0&&x<=100;const stamp=x=>{const n=Date.parse(x);return Number.isFinite(n)?n:null};
const tightestWindow=(windows,fresh)=>{let worst=null;for(const w of windows||[]){if(!fresh||!pct(w.used_percent))continue;if(!worst||w.used_percent>worst.used_percent)worst=w;}return worst};
const age=x=>{const t=stamp(x);return t===null?Infinity:(Date.now()-t)/1000};
const duration=s=>{if(!Number.isFinite(s))return 'Unknown';if(s<0)return 'Due · awaiting new reading';const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${Math.max(1,m)}m`};
const ZONE=(()=>{try{return Intl.DateTimeFormat().resolvedOptions().timeZone}catch(e){return 'local time'}})();
const clock=t=>new Date(t).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const ago=x=>{const a=age(x);return !Number.isFinite(a)||a<0?'No valid timestamp':a<60?'Observed just now':`Observed ${duration(a)} ago`};
function isFresh(a){return online&&a.status==='ok'&&age(a.observed_at)>=0&&age(a.observed_at)<=900}
function draw(){if(!snapshot)return;const accounts=Object.keys(names).map(provider=>snapshot.accounts.find(a=>a.provider===provider)||{provider,windows:[],status:'unknown'});$('providers').textContent=accounts.length;$('fresh').textContent=accounts.filter(isFresh).length;$('attention').textContent=accounts.filter(a=>!isFresh(a)||a.windows.some(w=>pct(w.used_percent)&&w.used_percent>=80)).length;if($('needsyou'))$('needsyou').textContent=(snapshot.operator||[]).filter(r=>r&&r.kind&&r.id).length;
let future=accounts.filter(isFresh).flatMap(a=>a.windows.map(w=>stamp(w.resets_at))).filter(t=>t&&t>Date.now());$('next').textContent=future.length?duration((Math.min(...future)-Date.now())/1000):'Unknown';$('cards').replaceChildren();
for(const a of accounts){let fresh=isFresh(a);if($('filter').value==='attention'&&fresh&&!a.windows.some(w=>pct(w.used_percent)&&w.used_percent>=80))continue;const c=node('article',undefined,'card'),top=node('div',undefined,'cardtop');top.append(node('div',names[a.provider].slice(0,1),'monogram'));const info=node('div');info.append(node('div',names[a.provider],'provider'),node('span',fresh?'● Fresh observation':a.status==='ok'?'◷ Last known reading':a.status==='rate_limited'?'◷ Usage check rate-limited · retrying automatically':a.status==='auth_required'?'△ Provider sign-in needs renewal':'△ Reading unavailable','state '+(fresh?'fresh':'warn')));
// Headline is the tightest window (min remaining), not whichever window sorts first: a
// five-hour reading with room can otherwise hide an exhausted weekly window at a glance.
const tightest=tightestWindow(a.windows,fresh);if(tightest){const remaining=100-tightest.used_percent;info.append(node('div',remaining.toFixed(1)+'% remaining · '+(labels[tightest.id]||tightest.id.replaceAll('_',' ')),'headline'+(remaining<=20?' warn':'')));}
top.append(info);c.append(top);
const windows=[...a.windows];for(const id of ['five_hour','weekly'])if(!windows.some(w=>w.id===id))windows.push({id,used_percent:null});windows.sort((a,b)=>['five_hour','weekly','monthly'].indexOf(a.id)-['five_hour','weekly','monthly'].indexOf(b.id));
for(const w of windows){const reported=pct(w.used_percent),valid=reported&&fresh,v=valid?(mode==='remaining'?100-w.used_percent:w.used_percent):null;const row=node('div',undefined,'window'),head=node('div',undefined,'windowhead');head.append(node('span',labels[w.id]||w.id.replaceAll('_',' ')));let number=node('span',valid?(Math.round(v*10)/10).toLocaleString():'Unknown','number'+(valid?'':' unavailable'));if(valid)number.append(node('small','% '+mode));head.append(number);const bar=node('div',undefined,'bar '+(!valid?'unknown':w.used_percent>=80?'warn':''));if(valid){const fill=node('i');fill.style.width=v+'%';bar.append(fill)}let reset='Reset not supplied';if(stamp(w.resets_at))reset='Resets '+clock(stamp(w.resets_at))+' · in '+duration((stamp(w.resets_at)-Date.now())/1000);else if(w.reset_label)reset='At observation: '+w.reset_label;let detail=valid?(mode==='used'?(100-w.used_percent).toFixed(1)+'% remaining':w.used_percent.toFixed(1)+'% used')+' · '+reset:reported?'Last observed: '+w.used_percent.toFixed(1)+'% used · '+(100-w.used_percent).toFixed(1)+'% remaining · stale':'Usage not reported by this source';if(!valid&&stamp(w.resets_at))detail+=' · '+reset+(w.reset_label?' ('+w.reset_label+')':'');row.append(head,bar,node('div',detail,'reset'));c.append(row)}
let foot=ago(a.observed_at)+(fresh?' · refreshes automatically':' · not current availability');if(pct(a.monthly_remaining)||Number.isFinite(a.monthly_remaining))foot+=' · '+a.monthly_remaining.toFixed(2)+' monthly plan units reported';c.append(node('div',foot,'cardfoot'));$('cards').append(c)}
if($('resets')){$('resets').replaceChildren();const rows=accounts.flatMap(a=>a.windows.filter(w=>stamp(w.resets_at)).map(w=>({provider:names[a.provider]||a.provider,id:labels[w.id]||w.id,t:stamp(w.resets_at),fresh:isFresh(a)}))).sort((a,b)=>a.t-b.t);for(const r of rows){const row=node('div',undefined,'workrow');row.append(node('strong',r.provider+' · '+r.id),node('span',clock(r.t)+' '+ZONE),node('span',(r.t>Date.now()?'in '+duration((r.t-Date.now())/1000):'due')+(r.fresh?'':' · from stale reading')));$('resets').append(row)}if(!rows.length)$('resets').append(node('div','No reset times reported.','empty'))}
if($('zone'))$('zone').textContent='All times in '+ZONE;$('work').replaceChildren();let work=(snapshot.attempts||[]).slice().sort((a,b)=>(stamp(b.at)||0)-(stamp(a.at)||0)).slice(0,8);for(const a of work){const row=node('div',undefined,'workrow');row.append(node('strong',a.task||'Untitled work'),node('span',(names[a.provider]||a.provider||'Unknown provider')+' · '+(a.status||'Unknown status')),node('span',ago(a.at)));$('work').append(row)}if(!work.length)$('work').append(node('div','No recent activity reported by the connected feed.','empty'));if($('evidence')){$('evidence').replaceChildren();const ev=(snapshot.scorecard||[]).slice(0,12);for(const r of ev){const rate=(typeof r.attempts==='number'&&r.attempts>0)?Math.round(100*(r.accepted||0)/r.attempts)+'% acceptance':(r.attempts===0?'no attempts yet':'rate unknown');const row=node('div',undefined,'workrow');row.append(node('strong',(r.family||'?')+' / '+(r.model||'?')),node('span',(r.category||'unrecorded')+' · '+((typeof r.accepted==='number')?r.accepted:0)+' of '+((typeof r.attempts==='number')?r.attempts:0)+' accepted'),node('span',rate));$('evidence').append(row)}if(!ev.length)$('evidence').append(node('div','No model evidence reported yet.','empty'));}if($('needs')){$('needs').replaceChildren();const KINDS={blocked_task:'Board task',held_attempt:'Held attempt',alarm:'Watch alarm',decision:'Your decision',draft:'Drafted fix',land_request:'Land request',data_status:'Data status'};const ops=(snapshot.operator||[]).slice(0,50);for(const o of ops){const opAge=age(o.since);const stale=STALE_KINDS.has(o.kind)&&Number.isFinite(opAge)&&opAge>=STALE_SECONDS;const row=node('div',undefined,'workrow'+(stale?' stale':''));row.append(node('strong',(KINDS[o.kind]||o.kind||'Unknown kind')+' · '+(o.id||'Unknown id')),node('span',o.reason||'No reason given'),node('span',stamp(o.since)!==null?'Waiting '+duration(age(o.since)):'Since unknown'));$('needs').append(row)}if(!ops.length)$('needs').append(node('div','Nothing is waiting on you right now.','empty'))}
if($('heartbeats')){$('heartbeats').replaceChildren();const hb=(snapshot.heartbeats||[]).slice(0,50);for(const h of hb){const row=node('div',undefined,'workrow');row.append(node('strong',h.name||'Unknown'),node('span',Number.isFinite(h.age_seconds)?'Last beat '+duration(h.age_seconds)+' ago':'No reading yet'),node('span',h.since?clock(stamp(h.since)):''));$('heartbeats').append(row)}if(!hb.length)$('heartbeats').append(node('div','No heartbeats configured.','empty'))}
if($('diskusage')){$('diskusage').replaceChildren();const d=snapshot.disk_usage;if(d&&d.exists){const gb=b=>(b/1073741824).toFixed(2)+' GB';const row=node('div',undefined,'workrow'+(d.over_cap?' stale':''));row.append(node('strong','~/.grid-workspaces'),node('span',gb(d.total_bytes)+' of '+gb(d.cap_bytes)+' cap'),node('span',d.over_cap?'Over cap':'Within cap'));$('diskusage').append(row)}else $('diskusage').append(node('div','No workspace directory found.','empty'))}
if($('diskfree')){$('diskfree').replaceChildren();const d=snapshot.disk_free;if(d){const gb=b=>Number.isFinite(b)?(b/1073741824).toFixed(1)+' GB free':'Unknown';const cls=d.state==='red'?' stale':d.state==='alarm'?' warn':'';const row=node('div',undefined,'workrow'+cls);row.append(node('strong','/System/Volumes/Data'),node('span',gb(d.free_bytes)),node('span',d.reason||(d.state==='ok'?'Healthy':d.state==='red'?'Critically low':'Low')));$('diskfree').append(row)}else $('diskfree').append(node('div','No disk reading yet.','empty'))}
// Age is computed here from `since`, live, on every redraw -- never from a baked
// `age_seconds` -- so a collector row keeps counting up even while the overlay that
// last wrote it goes unrefreshed (02-A1/C3 review finding).
if($('collectorages')){$('collectorages').replaceChildren();const ca=(snapshot.collector_ages||[]).slice(0,50);for(const c of ca){const a=age(c.since);const alarmed=!Number.isFinite(a)||a>COLLECTOR_ALARM_SECONDS;const row=node('div',undefined,'workrow'+(alarmed?' stale':''));row.append(node('strong',c.name||'Unknown'),node('span',Number.isFinite(a)?'Last observed '+duration(a)+' ago':'No reading yet'),node('span',c.since?clock(stamp(c.since)):''));$('collectorages').append(row)}if(!ca.length)$('collectorages').append(node('div','No collectors configured.','empty'))}
if($('memory')){$('memory').replaceChildren();const m=snapshot.memory;if(m){const cls=m.state==='red'?' stale':m.state==='amber'?' warn':'';const swap=Number.isFinite(m.swap_gb)?m.swap_gb.toFixed(1)+' GB swap':'Unknown swap reading';const top=(m.top_processes||[]).map(p=>(p.comm||'?')+' '+(Number.isFinite(p.rss_gb)?p.rss_gb.toFixed(1)+' GB':'?')).join(', ');const row=node('div',undefined,'workrow'+cls);row.append(node('strong','Memory'),node('span',swap),node('span',m.reason||top||(m.state==='ok'?'Healthy':'')));$('memory').append(row)}else $('memory').append(node('div','No memory reading yet.','empty'))}
// 02-C2/C4/C5: the same numbers `inference-grid report --week` prints, never
// recomputed here -- a bare percentage is never shown without its numerator/denominator.
if($('thisweek')){$('thisweek').replaceChildren();const tw=snapshot.this_week;const frac=x=>Number.isFinite(x)?Math.round(x*100)+'%':'unrecorded';
// One decimal place for the failure-rate-vs-baseline comparison specifically: rounding
// both to a whole percent (frac) can make two visibly different rates read identically,
// defeating the comparison the row exists for (the fixed 2026-09-17 baseline is 17.6%).
const frac1=x=>Number.isFinite(x)?(x*100).toFixed(1)+'%':'unrecorded';const pct100=x=>Number.isFinite(x)?Math.round(x)+'%':'unrecorded';const usd=x=>Number.isFinite(x)?'$'+x.toFixed(2):'unrecorded';if(tw){const rows=[];const ulRow=node('div',undefined,'workrow');ulRow.append(node('strong','Unattended-land rate'),node('span',(Number.isFinite(tw.unattended_land_count)?tw.unattended_land_count:0)+' of '+tw.packets_landed+' packets'),node('span',frac(tw.unattended_land_rate)));rows.push(ulRow);const fRate=tw.failure_rate||{};const overBaseline=Number.isFinite(fRate.rate)&&Number.isFinite(fRate.baseline_2026_09_17)&&fRate.rate>fRate.baseline_2026_09_17;const fRow=node('div',undefined,'workrow'+(overBaseline?' warn':''));fRow.append(node('strong','Failure rate'),node('span',(fRate.failed_or_abandoned||0)+' of '+(fRate.attempts||0)+' attempts'),node('span',frac1(fRate.rate)+' (2026-09-17 baseline '+frac1(fRate.baseline_2026_09_17)+')'));rows.push(fRow);for(const q of (tw.quota_use||[]).slice(0,10)){const used=Number.isFinite(q.numerator)&&Number.isFinite(q.denominator)?q.numerator+' of '+q.denominator:'provider-reported, raw units unrecorded';const row=node('div',undefined,'workrow');row.append(node('strong',(q.subscription||'Unknown')+' quota'),node('span',used),node('span',pct100(q.used_percent)));rows.push(row)}for(const c of (tw.subscription_costs||[]).slice(0,10)){const row=node('div',undefined,'workrow');row.append(node('strong',(c.subscription||'Unknown')+' cost'),node('span',usd(c.weekly_cost_usd)+' ÷ '+c.landed_packets+' landed'),node('span',usd(c.cost_per_landed_packet_usd)+' per packet'));rows.push(row)}for(const row of rows)$('thisweek').append(row)}else $('thisweek').append(node('div','No weekly reading yet.','empty'))}
if($('failurerows')){$('failurerows').replaceChildren();const FKINDS={failed_attempt:'Failed attempt',held_attempt:'Held attempt'};const fr=(snapshot.failures||[]).slice(0,50);for(const f of fr){const row=node('div',undefined,'workrow failurerow');row.append(node('strong',(FKINDS[f.kind]||f.kind||'Unknown')+' · '+(f.task||f.id||'Unknown')),node('span',f.reason||'No reason given'),node('span',stamp(f.since)!==null?'Since '+duration(age(f.since))+' ago':'Since unknown'));if(f.suggestion)row.append(node('span','Suggested: '+f.suggestion));if(f.log_tail)row.append(node('pre',f.log_tail.slice(-2000),'sectiontail'));$('failurerows').append(row)}if(!fr.length)$('failurerows').append(node('div','No failed or held attempts right now.','empty'))}if($('acceptedwork')){$('acceptedwork').replaceChildren();const isoWeek=t=>{const d=new Date(t),dt=new Date(Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),d.getUTCDate())),day=dt.getUTCDay()||7;dt.setUTCDate(dt.getUTCDate()+4-day);return dt.getUTCFullYear()+'-W'+String(Math.ceil(((dt-Date.UTC(dt.getUTCFullYear(),0,1))/86400000+1)/7)).padStart(2,'0')};const weeks={now:isoWeek(Date.now()),last:isoWeek(Date.now()-7*86400000)},byAccount={};for(const r of (snapshot.accepted_work||[]).slice(0,50))if(r&&r.account){const e=byAccount[r.account]||(byAccount[r.account]={}),w=r.week||'?';e[w]={accepted:r.accepted,attempts:r.attempts}}const seen=Object.keys(byAccount).sort();for(const account of seen){const e=byAccount[account],cell=w=>{const c=e[w]||{},a=typeof c.accepted==='number'?c.accepted:0,t=typeof c.attempts==='number'?c.attempts:0;return a+' of '+t+' attempts accepted'};const row=node('div',undefined,'workrow');row.append(node('strong',account),node('span',weeks.now+' · '+cell(weeks.now)),node('span',weeks.last+' · '+cell(weeks.last)));$('acceptedwork').append(row)}if(!seen.length)$('acceptedwork').append(node('div','No accepted work reported yet.','empty'))}if($('reviewerrecall')){$('reviewerrecall').replaceChildren();const rate=n=>typeof n==='number'?Math.round(100*n)+'%':'unknown';const rr=(snapshot.reviewer_recall||[]).slice(0,50);for(const r of rr){const row=node('div',undefined,'workrow');row.append(node('strong',r.lane||'Unknown lane'),node('span','recall '+rate(r.recall)+' · precision '+rate(r.precision)),node('span',(r.run_id||'run unknown')+(r.scored_at?' · scored '+ago(r.scored_at):'')));$('reviewerrecall').append(row)}if(!rr.length)$('reviewerrecall').append(node('div','No reviewer recall reported yet.','empty'))}$('updated').textContent='View updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}
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
$('refresh').onclick=()=>refresh(true);$('filter').onchange=draw;for(const m of ['remaining','used'])$(m).onclick=()=>{mode=m;$('remaining').setAttribute('aria-pressed',m==='remaining');$('used').setAttribute('aria-pressed',m==='used');draw()};window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installEvent=e});$('install').onclick=async()=>{if(installEvent){await installEvent.prompt();installEvent=null}else $('installHelp').showModal()};$('closeHelp').onclick=()=>$('installHelp').close();window.addEventListener('offline',()=>{online=false;$('connection').textContent='Offline · live readings unavailable';draw()});if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});refresh();setInterval(refresh,15000);// --- Boards: planned, running, blocked and landed work, local only (never uploaded) ---
const BOARDLISTS=[['active','Running'],['planned','Planned'],['blocked','Blocked'],['landed_today','Landed today']];
const BOARDSTATES={active:'active',planned:'planned',blocked:'blocked',landed_today:'landed'};
let boards=[],boardProject='',boardState='',boardSort={key:'project',dir:1};
const esc=v=>v==null?'':String(v);
function allBoardRows(){const rows=[];for(const b of boards){for(const [key] of BOARDLISTS){for(const r of (b[key]||[]))rows.push(Object.assign({},r,{board:b.name,state:BOARDSTATES[key]}))}}return rows;}
function boardAge(r){const s=Number(r.age);return Number.isFinite(s)?duration(s)+' ago':'—';}
function boardMeta(r,state){
 if(state==='active'){const gate=r.gate?('gate '+esc(r.gate.name)):'gates pending';return 'round '+(r.round||1)+' of '+(r.max_rounds||1)+' · '+gate+' · '+(esc(r.lane)||'lane unknown')+(r.model?' · '+esc(r.model):'')+' · '+duration((Number(r.minutes)||0)*60);}
 if(state==='planned'){const why=r.reason?esc(r.reason):(r.lane?('ready for '+esc(r.lane)):'cannot dispatch');return (r.lane?('lane '+esc(r.lane)+' · '):'')+why+' · waiting '+boardAge(r);}
 if(state==='blocked')return esc(r.reason||'blocked')+' · '+(r.operator_owed?'needs you':'no action needed')+' · '+boardAge(r);
 return esc(r.state||'landed')+' · '+boardAge(r);
}
function boardRow(r,state){const row=node('div',undefined,'workrow boardrow');row.tabIndex=0;row.setAttribute('role','button');row.append(node('strong',r.title||r.id),node('span',esc(r.id),'badge'),node('span',boardMeta(r,state),'boardmeta'));row.onclick=()=>openDrawer(r,state);row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openDrawer(r,state);}};return row;}
function drawBoards(){
 const cards=$('boardcards');if(!cards)return;
 const projectSel=$('boardproject');
 if(projectSel){const current=projectSel.value;projectSel.replaceChildren();const all=node('option','All projects');all.value='';projectSel.append(all);for(const name of boards.map(b=>b.name).filter(Boolean).slice().sort()){const o=node('option',name);o.value=name;projectSel.append(o);}projectSel.value=boards.some(b=>b.name===current)?current:'';boardProject=projectSel.value;}
 cards.replaceChildren();
 if(!boards.length)cards.append(node('div','No boards reported by the local overlay yet.','empty'));
 for(const b of boards){const card=node('article',undefined,'card boardcard');const top=node('div',undefined,'cardtop');top.append(node('div',(b.name||'?').slice(0,1).toUpperCase(),'monogram'));const info=node('div');const counts=BOARDLISTS.map(([k])=>((b[k]||[]).length)).join(' / ');info.append(node('div',b.name||'board','provider'),node('span',counts+' · running / planned / blocked / landed','state'));top.append(info);card.append(top);
  for(const [key,label] of BOARDLISTS){const list=b[key]||[];if(!list.length)continue;card.append(node('h3',label+' ('+list.length+')','boardlisthead'));for(const r of list)card.append(boardRow(r,BOARDSTATES[key]));}
  if(!BOARDLISTS.some(([k])=>(b[k]||[]).length))card.append(node('div','Nothing planned, running, blocked or landed today.','empty'));
  cards.append(card);}
 drawAllWork();
}
function drawAllWork(){
 const host=$('allwork');if(!host)return;
 const rows=allBoardRows().filter(r=>(!boardProject||r.board===boardProject)&&(!boardState||r.state===boardState));
 const cols=[['project','Project'],['title','Title'],['state','State'],['lane','Lane'],['age','Age']];
 const value=r=>{const v=r[boardSort.key];if(boardSort.key==='age'){const n=Number(v);return Number.isFinite(n)?n:Infinity;}return (v==null?'':String(v)).toLowerCase();};
 rows.sort((a,b)=>{const x=value(a),y=value(b);return x<y?-boardSort.dir:x>y?boardSort.dir:0;});
 const table=node('table',undefined,'boardtable');const head=node('tr');
 for(const [key,label] of cols){const th=node('th',label+(boardSort.key===key?(boardSort.dir>0?' ▲':' ▼'):''));th.onclick=()=>{boardSort=boardSort.key===key?{key,dir:-boardSort.dir}:{key,dir:1};drawAllWork();};head.append(th);}
 const thead=node('thead');thead.append(head);table.append(thead);
 const body=node('tbody');
 for(const r of rows){const tr=node('tr');tr.tabIndex=0;tr.onclick=()=>openDrawer(r,r.state);tr.append(node('td',r.project||''),node('td',r.title||r.id),node('td',r.state||''),node('td',r.lane||'—'),node('td',boardAge(r)));body.append(tr);}
 table.append(body);
 host.replaceChildren();host.append(node('h3','All work across boards','boardlisthead'),rows.length?table:node('div','No rows match the filter.','empty'));
}
function openDrawer(r,state){
 const title=$('drawertitle'),meta=$('drawermeta'),body=$('drawerbody');
 if(!title||!body)return;
 title.textContent=r.title||r.id||'Task';
 if(meta)meta.textContent=[r.project,r.id,state,r.lane,r.model,(r.round?('round '+r.round+' of '+(r.max_rounds||1)):'')].filter(Boolean).join(' · ');
 body.replaceChildren();
 if(r.focus)body.append(node('p',r.focus,'caption'));
 const links=r.links||{},linkList=[];
 for(const [key,label] of [['brief','Brief'],['report','Report'],['attempt','Attempt']])if(links[key])linkList.push(label+': '+links[key]);
 if(linkList.length)body.append(node('pre',linkList.join('\n'),'drawerpaths'));
 if(r.gate&&r.gate.tail){body.append(node('h3','Last gate · '+esc(r.gate.name)));body.append(node('pre',r.gate.tail,'gate'));}
 if(r.section){body.append(node('h3','Packet section'));body.append(node('pre',r.section,'sectiontail'));}
 const dialog=$('boarddrawer');if(dialog&&dialog.showModal)dialog.showModal();
}
async function loadBoards(){try{const r=await fetch('/api/boards',{cache:'no-store'});if(!r.ok)throw Error('http');const data=await r.json();boards=Array.isArray(data.boards)?data.boards:[];}catch(error){boards=[];}drawBoards();}
if($('boardstate'))$('boardstate').onchange=()=>{boardState=$('boardstate').value;drawAllWork();};
if($('closedrawer'))$('closedrawer').onclick=()=>$('boarddrawer').close();
if($('doneclick'))$('doneclick').onclick=()=>$('boarddrawer').close();
loadBoards();setInterval(loadBoards,30000);
