const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
class El{constructor(){this.children=[];this.style={};this.value='all';this.textContent=''}append(...x){this.children.push(...x)}replaceChildren(...x){this.children=x}setAttribute(){} }
const els={};const context={document:{getElementById:id=>els[id]??=new El(),createElement:()=>new El()},window:{addEventListener(){}},navigator:{},fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){},clearTimeout(){},AbortController,Date,Number,Math};
vm.createContext(context);vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'web/app.js'),'utf8'),context);
vm.runInContext(`snapshot={captured_at:new Date().toISOString(),accounts:[{provider:'command-code',status:'ok',observed_at:new Date().toISOString(),windows:[{id:'weekly',used_percent:10.86}]},{provider:'opencode',status:'ok',observed_at:'2020-01-01',windows:[{id:'weekly',used_percent:0.1,resets_at:new Date(Date.now()+7200e3).toISOString()}]},{provider:'codex',status:'error',observed_at:'2020-01-01',windows:[{id:'weekly',used_percent:null,resets_at:new Date(Date.now()+3600e3).toISOString(),reset_label:'reset from seat1 reading'}]}]};online=true;draw()`,context);
const text=e=>e.textContent+e.children.map(text).join(' ');
let out=text(els.cards);assert(out.includes('10.9% used'));assert(out.includes('89.1% remaining'));assert(out.includes('Unknown'));assert(out.includes('Last observed: 0.1% used · 99.9% remaining · stale'));assert(!out.includes('99.9% used'));assert(out.includes('Usage not reported by this source · Resets'));assert(out.includes('reset from seat1 reading'));assert(text(els.resets).includes('Codex · Weekly allowance'));
vm.runInContext(`mode='remaining';draw()`,context);out=text(els.cards);assert(out.includes('89.1% remaining'));assert(out.includes('10.9% used'));

// 02-B5, 2026-09-17: the headline must be the tightest window (min remaining), not
// whichever window sorts first -- five-hour had room while weekly was exhausted.
vm.runInContext(`snapshot={captured_at:new Date().toISOString(),accounts:[{provider:'command-code',status:'ok',observed_at:new Date().toISOString(),windows:[{id:'five_hour',used_percent:12},{id:'weekly',used_percent:97}]}]};mode='used';draw()`,context);
out=text(els.cards);
assert(out.includes('3.0% remaining · Weekly allowance'),'headline must name the tight window');
assert(!out.includes('88.0% remaining · 5-hour allowance'),'headline must not be the roomy window');
vm.runInContext(`snapshot.accounts[0].windows=[{id:'five_hour',used_percent:97},{id:'weekly',used_percent:12}];draw()`,context);
out=text(els.cards);
assert(out.includes('3.0% remaining · 5-hour allowance'),'order reversed: five-hour is now the tight one');
vm.runInContext(`snapshot.accounts[0].observed_at='2020-01-01';draw()`,context);
out=text(els.cards);
// The per-window stale detail line also contains "% remaining · stale", so check for the
// headline's specific label suffix rather than the generic substring.
assert(!out.includes('% remaining · Weekly allowance')&&!out.includes('% remaining · 5-hour allowance'),
  'a stale (non-fresh) account must show no headline at all');
console.log('PASS: headline is the tightest window, not the first-sorted one');
vm.runInContext(`snapshot.scorecard=[{family:'glm',model:'glm-5.3-flash',category:'pure_function',attempts:7,completed:6,accepted:6,repairs:0,usage:{input_tokens:2641},secret:'DO_NOT_SHARE'},{family:'qwen',model:'qwen3.8-max',category:'tests_multi_file'},{family:'x',model:'y',category:'z',attempts:0}];draw()`,context);
const ev=text(els.evidence);
assert(ev.includes('glm / glm-5.3-flash'));assert(ev.includes('pure_function · 6 of 7 accepted'));assert(ev.includes('86% acceptance'));
assert(ev.includes('qwen / qwen3.8-max'));assert(ev.includes('rate unknown'));
assert(ev.includes('x / y'));assert(ev.includes('no attempts yet'));
assert(!ev.includes('DO_NOT_SHARE'));
console.log('PASS: evidence rows with acceptance rates');

console.log('PASS: used/remaining labels, stale unknown, historical values');
