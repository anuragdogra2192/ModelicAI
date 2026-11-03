// Imperative workbench engine: diagram canvas, CodeMirror editor, Plotly results,
// parameter panel and Modi chat. Mounted once by App.jsx.
import CodeMirror from 'codemirror';
import 'codemirror/lib/codemirror.css';
import 'codemirror/addon/mode/simple.js';
import Plotly from 'plotly.js-dist-min';

let started = false;
export function startStudio() {
  if (started) return;  // React StrictMode mounts effects twice in dev
  started = true;
const API = import.meta.env.VITE_API_BASE || '';  // dev: Vite proxies /api to :8000
const $ = s => document.querySelector(s);
const state = { model:null, code:'', graph:{nodes:[],edges:[]}, params:{}, original:{}, vars:[], selected:new Set(),
  data:null, pins:[], thread: 'studio-' + Math.random().toString(36).slice(2,8), selComp:null };

/* ---------- utilities ---------- */
async function api(path, opts={}) {
  const res = await fetch(API + path, {headers:{'Content-Type':'application/json'}, ...opts});
  let body; try { body = await res.json(); } catch { body = null; }
  if (!res.ok) throw new Error((body && (body.detail || body.message)) || res.statusText);
  return body;
}
function setStatus(text, kind='') { const el=$('#status'); el.textContent=text; el.className='pill '+kind; }
function log(text, kind='') {
  const f=$('#footLog'); f.textContent=text; f.className='log '+(kind==='e'?'err':'');
  const c=$('#console'); const t=new Date().toLocaleTimeString();
  c.insertAdjacentHTML('beforeend', `<span class="t">[${t}]</span> <span class="${kind}">${esc(text)}</span>\n`);
  c.scrollTop=c.scrollHeight;
}
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---------- code editor ---------- */
CodeMirror.defineSimpleMode('modelica', {
  start: [
    {regex: /\/\/.*/, token: 'comment'},
    {regex: /\/\*/, token: 'comment', next: 'comment'},
    {regex: /"(?:[^\\"]|\\.)*?"/, token: 'string'},
    {regex: /"/, token: 'string', next: 'string'},
    {regex: /\b(model|block|connector|package|function|record|class|type)(\s+)([A-Za-z_]\w*)/, token: ['keyword', null, 'def']},
    {regex: /\b(?:algorithm|and|annotation|break|constant|discrete|each|else|elseif|elsewhen|encapsulated|end|enumeration|equation|expandable|extends|external|final|flow|for|if|import|impure|in|initial|inner|input|loop|not|operator|or|outer|output|parameter|partial|protected|public|pure|redeclare|replaceable|return|stream|then|when|while|within|connect|der|time)\b/, token: 'keyword'},
    {regex: /\b(?:Real|Integer|Boolean|String|sin|cos|tan|exp|log|sqrt|abs|pre|sample|noEvent|smooth|reinit|terminal)\b/, token: 'builtin'},
    {regex: /\bModelica(?:\.\w+)+/, token: 'variable-2'},
    {regex: /(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/, token: 'number'},
    {regex: /[A-Za-z_]\w*/, token: null},  // consume identifiers whole so keywords never match inside them
  ],
  string: [{regex: /(?:[^\\"]|\\.)*?"/, token: 'string', next: 'start'}, {regex: /.*/, token: 'string'}],
  comment: [{regex: /.*?\*\//, token: 'comment', next: 'start'}, {regex: /.*/, token: 'comment'}],
  meta: {lineComment: '//'}
});
const editor = CodeMirror($('#editor'), {mode:'modelica', lineNumbers:true, indentUnit:2, tabSize:2, value:'// Open or generate a model'});
editor.on('change', () => { if (state.model) $('#btnCheck').textContent = editor.getValue() !== state.code ? 'Check • unsaved' : 'Check'; });

/* ---------- tabs & layout ---------- */
$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('.tab'); if (!b) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t===b));
  const v = b.dataset.v, views=$('#views');
  views.classList.toggle('split', v==='split');
  document.querySelectorAll('.view').forEach(el => el.classList.toggle('on', v!=='split' && el.dataset.v===v));
  setTimeout(() => { editor.refresh(); drawDiagram(); }, 0);
});
(() => { // resizable results panel
  let y0, h0; const g=$('#gut'), c=$('.center');
  g.addEventListener('mousedown', e => { y0=e.clientY; h0=$('.results').offsetHeight;
    const mv = ev => { c.style.setProperty('--resH', Math.max(140, Math.min(window.innerHeight-200, h0 - (ev.clientY-y0))) + 'px'); Plotly.Plots.resize('plot'); };
    const up = () => { removeEventListener('mousemove', mv); removeEventListener('mouseup', up); editor.refresh(); };
    addEventListener('mousemove', mv); addEventListener('mouseup', up); });
})();

/* ---------- numeric-only fields ---------- */
// Simulation setup and parameter fields accept numbers only. Characters that cannot be part of a
// number are rejected as you type (the field keeps its last valid value) and a warning is shown.
const NUM_FULL = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;
const NUM_PARTIAL = /^[-+]?(\d*\.?\d*)([eE][-+]?\d*)?$/;
const BOOL_PARTIAL = /^(t|tr|tru|true|f|fa|fal|fals|false)$/;
function fieldWarn(input, msg) {
  const row = input.closest('.prow'); if (!row) return;
  let w = row.nextElementSibling && row.nextElementSibling.classList.contains('fieldwarn') ? row.nextElementSibling : null;
  if (!msg) { if ((input._rejectUntil||0) > Date.now() && w && w.textContent === input._rejectMsg) return;  // keep a rejection warning readable
    if (w) w.remove(); input.classList.remove('invalid'); return; }
  if (!w) { w = document.createElement('div'); w.className = 'fieldwarn'; row.after(w); }
  w.textContent = msg; input.classList.add('invalid');
}
function guardNumeric(input, allowBool=false) {
  const v = input.value.trim();
  const okPartial = v === '' || NUM_PARTIAL.test(v) || (allowBool && BOOL_PARTIAL.test(v));
  if (!okPartial) {  // reject the keystroke / paste: restore the last accepted value
    input.value = input.dataset.ok ?? '';
    input._rejectUntil = Date.now() + 2000;
    input._rejectMsg = allowBool ? 'Numbers only (or true/false), e.g. 10, 0.5, 1e-3' : 'Numbers only, e.g. 10, 0.5 or 1e-3';
    fieldWarn(input, allowBool ? 'Numbers only (or true/false), e.g. 10, 0.5, 1e-3' : 'Numbers only, e.g. 10, 0.5 or 1e-3');
    clearTimeout(input._wt); input._wt = setTimeout(() => validateAll(), 2200);
    return false;
  }
  input.dataset.ok = input.value;
  return true;
}
function guardExpr(input) {
  if (EXPR_CHARS.test(input.value)) { input.dataset.ok = input.value; return true; }
  input.value = input.dataset.ok ?? '';
  input._rejectUntil = Date.now() + 2000;
  input._rejectMsg = 'Not allowed here. Use numbers, parameter names, + - * / ^ and ( ), e.g. 2*k';
  fieldWarn(input, input._rejectMsg);
  clearTimeout(input._wt); input._wt = setTimeout(() => validateAll(), 2200);
  return false;
}
function validateSetup() {
  const st=$('#sStart'), sp=$('#sStop'), n=$('#sN'); let ok=true;
  const num = el => NUM_FULL.test(el.value.trim()) ? +el.value : NaN;
  const a=num(st), b=num(sp), c=num(n);
  fieldWarn(st, isFinite(a) ? '' : 'Enter a number, e.g. 0'); ok = ok && isFinite(a);
  if (!isFinite(b)) { fieldWarn(sp, 'Enter a number, e.g. 10 or 1e-3'); ok=false; }
  else if (isFinite(a) && b <= a) { fieldWarn(sp, 'Stop time must be greater than start time'); ok=false; }
  else fieldWarn(sp, '');
  if (!isFinite(c) || !Number.isInteger(c)) { fieldWarn(n, 'Intervals must be a whole number, e.g. 500'); ok=false; }
  else if (c < 1 || c > 1e7) { fieldWarn(n, 'Intervals must be between 1 and 10,000,000'); ok=false; }
  else fieldWarn(n, '');
  return ok;
}
/* Parameter expressions: numbers, true/false, other parameters, + - * / ^, ( ), functions, pi.
   Parsed with a small recursive-descent parser (no eval). */
const EXPR_CHARS = /^[\w\s.+\-*/^(),]*$/;
const FUNCS = {sqrt:Math.sqrt, sin:Math.sin, cos:Math.cos, tan:Math.tan, asin:Math.asin, acos:Math.acos, atan:Math.atan,
  atan2:Math.atan2, exp:Math.exp, log:Math.log, log10:Math.log10, abs:Math.abs, min:Math.min, max:Math.max,
  sinh:Math.sinh, cosh:Math.cosh, tanh:Math.tanh, floor:Math.floor, ceil:Math.ceil, sign:Math.sign};
const CONSTS = {pi:Math.PI, 'Modelica.Constants.pi':Math.PI, e:Math.E, 'Modelica.Constants.e':Math.E,
  'Modelica.Constants.g_n':9.80665, 'Modelica.Constants.T_zero':-273.15};
function paramLookup(name, scope) {
  // a name inside "spring.c" may refer to a top-level parameter ("k") or a sibling ("spring.d")
  if (name in state.params) return name;
  if (scope && `${scope}.${name}` in state.params) return `${scope}.${name}`;
  return null;
}
function evalExpr(src, scope, depth=0, seen=new Set()) {
  if (depth > 12) throw new Error('Expression is circular or nested too deeply');
  const toks = String(src).match(/\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|[A-Za-z_][\w.]*|[-+*/^(),]|\S/g) || [];
  let i = 0; const peek = () => toks[i], next = () => toks[i++];
  const expect = t => { if (next() !== t) throw new Error(`Expected "${t}"`); };
  function primary() {
    const t = next(); if (t === undefined) throw new Error('Incomplete expression');
    if (/^(\d|\.\d)/.test(t)) return parseFloat(t);
    if (t === '(') { const v = expr(); expect(')'); return v; }
    if (/^[A-Za-z_]/.test(t)) {
      if (t === 'true') return 1; if (t === 'false') return 0;
      if (peek() === '(') { const f = FUNCS[t]; if (!f) throw new Error(`Unknown function: ${t}`);
        next(); const args = [expr()]; while (peek() === ',') { next(); args.push(expr()); } expect(')'); return f(...args); }
      if (t in CONSTS) return CONSTS[t];
      const key = paramLookup(t, scope); if (!key) throw new Error(`Unknown parameter: ${t}`);
      if (seen.has(key)) throw new Error(`Circular reference: ${key}`);
      const raw = String(state.params[key]).trim(); const sc = key.includes('.') ? key.split('.')[0] : null;
      return NUM_FULL.test(raw) ? parseFloat(raw) : evalExpr(raw, sc, depth+1, new Set([...seen, key]));
    }
    throw new Error(`Unexpected "${t}"`);
  }
  function unary() { if (peek() === '-') { next(); return -unary(); } if (peek() === '+') { next(); return unary(); } return power(); }
  function power() { const b = primary(); if (peek() === '^') { next(); return Math.pow(b, unary()); } return b; }
  function term() { let v = unary(); while (peek() === '*' || peek() === '/') { const op = next(), r = unary(); v = op === '*' ? v*r : v/r; } return v; }
  function expr() { let v = term(); while (peek() === '+' || peek() === '-') { const op = next(), r = term(); v = op === '+' ? v+r : v-r; } return v; }
  const v = expr(); if (i < toks.length) throw new Error(`Unexpected "${toks[i]}"`);
  if (!isFinite(v)) throw new Error('Result is not a finite number');
  return v;
}
function checkParam(key, raw) {
  // -> {ok, value, msg}; value is the number used for a run
  const v = String(raw).trim();
  if (v === '') return {ok:true, value:null};
  if (v === 'true' || v === 'false') return {ok:true, value:v};
  if (NUM_FULL.test(v)) return {ok:true, value:v};
  if (!EXPR_CHARS.test(v)) return {ok:false, msg:'Use numbers, parameter names, + - * / ^ and ( )'};
  try { const sc = key.includes('.') ? key.split('.')[0] : null; return {ok:true, value:String(+evalExpr(v, sc).toPrecision(12)), expr:true}; }
  catch (e) { return {ok:false, msg:e.message}; }
}
function validateParams() {
  let ok=true;
  document.querySelectorAll('#params input[data-k]').forEach(el => {
    const r = checkParam(el.dataset.k, el.value), hint = el.closest('.prow').querySelector('.eval');
    fieldWarn(el, r.ok ? '' : r.msg); ok = ok && r.ok;
    if (hint) hint.textContent = r.ok && r.expr ? `= ${fmtSI(+r.value).trim()}` : '';
  });
  return ok;
}
function validateAll() { return validateSetup() & validateParams(); }
['#sStart','#sStop','#sN'].forEach(id => {
  const el=$(id); el.setAttribute('inputmode','decimal'); el.setAttribute('autocomplete','off'); el.dataset.ok = el.value;
  el.addEventListener('input', () => { if (guardNumeric(el)) validateSetup(); });
});
$('#params').addEventListener('input', e => { const el=e.target; if (!el.dataset || !el.dataset.k) return;
  if (!guardExpr(el)) { e.stopImmediatePropagation(); return; }
  state.params[el.dataset.k] = el.value;  // store first so dependent "= value" hints use the new number
  validateParams(); }, true);

/* ---------- live player controls ---------- */
$('#livePlay').addEventListener('click', livePlay);
$('#liveSlider').addEventListener('input', e => { liveStop(); liveSeek(+e.target.value); });
$('#liveSpeed').addEventListener('change', e => { live.dur = +e.target.value; });

/* ---------- themes ---------- */
const THEMES = {graphite:{dark:true}, midnight:{dark:true}, lagoon:{dark:true}, crimson:{dark:true}, forest:{dark:true},
  daylight:{dark:false}, paper:{dark:false}, ocean:{dark:false}, blossom:{dark:false}, sunflower:{dark:false}, eink:{dark:false}};
const theme = {choice:'auto', name:'graphite', dark:true, canvas:'theme'};
const darkMQ = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
function applyTheme(choice, persist=true) {
  theme.choice = THEMES[choice] || choice==='auto' ? choice : 'auto';
  theme.name = theme.choice==='auto' ? (darkMQ && !darkMQ.matches ? 'daylight' : 'graphite') : theme.choice;
  theme.dark = THEMES[theme.name].dark;
  document.documentElement.dataset.theme = theme.name;
  if (persist) { try { localStorage.setItem('modi.theme', theme.choice); } catch {} }
  $('#themeSel') && ($('#themeSel').value = theme.choice);
  updateCanvasBtn();
  try { drawDiagram(); if (state.data) drawPlot(); } catch {}  // first call runs before the diagram code is initialised
}
function updateCanvasBtn() { const b=$('#btnCanvas'); if (b) b.textContent = theme.canvas==='light' ? 'Canvas: light' : 'Canvas: theme'; }
try { theme.canvas = localStorage.getItem('modi.canvas') || 'theme'; } catch {}
try { applyTheme(localStorage.getItem('modi.theme') || 'auto', false); } catch { applyTheme('auto', false); }
if (darkMQ && darkMQ.addEventListener) darkMQ.addEventListener('change', () => { if (theme.choice==='auto') applyTheme('auto', false); });
$('#themeSel').addEventListener('change', e => applyTheme(e.target.value));
$('#btnCanvas').addEventListener('click', () => { theme.canvas = theme.canvas==='light' ? 'theme' : 'light';
  try { localStorage.setItem('modi.canvas', theme.canvas); } catch {} updateCanvasBtn(); drawDiagram(); });

/* ---------- resizable chat panel ---------- */
(() => {
  const app=$('.app'), h=$('#leftGrip'); if (!h) return;
  const set = w => { w=Math.max(240, Math.min(window.innerWidth*0.6, w)); app.style.setProperty('--leftW', w+'px'); return w; };
  try { const w=+localStorage.getItem('modi.leftW'); if (w) set(w); } catch {}
  h.addEventListener('mousedown', e => { e.preventDefault(); const x0=e.clientX, w0=$('.left').offsetWidth; let w=w0;
    const mv = ev => { w=set(w0+ev.clientX-x0); editor.refresh(); };
    const up = () => { removeEventListener('mousemove', mv); removeEventListener('mouseup', up); try { localStorage.setItem('modi.leftW', w); } catch {} drawDiagram(); Plotly.Plots.resize('plot'); };
    addEventListener('mousemove', mv); addEventListener('mouseup', up); });
  h.addEventListener('dblclick', () => { set(280); try { localStorage.removeItem('modi.leftW'); } catch {} drawDiagram(); });
})();

/* ---------- library ---------- */
async function loadLibrary() {
  try {
    const list = await api('/api/studio/models');
    $('#lib').innerHTML = list.length ? list.map(m => `<button data-m="${esc(m.name)}" class="${m.name===state.model?'on':''}"><span class="dot ${m.has_results?'res':''}"></span><span class="lname">${esc(m.name)}</span><span class="del" data-del="${esc(m.name)}" title="Delete ${esc(m.name)}">✕</span></button>`).join('')
      : '<div class="hint">No models yet.</div>';
  } catch (e) { $('#lib').innerHTML = `<div class="hint">Backend unreachable: ${esc(e.message)}</div>`; }
}
$('#lib').addEventListener('click', async e => {
  const del = e.target.closest('[data-del]');
  if (del) {
    e.stopPropagation();
    const name = del.dataset.del;
    if (!window.confirm(`Delete model "${name}"?\n\nIts chat, Modi's memory, runs and revisions are permanently erased.\nThe model files are moved to models/.trash/.`)) return;
    try {
      await api(`/api/studio/models/${name}`, {method:'DELETE'}); log(`Deleted ${name}: memory erased, files moved to models/.trash/`, 'o');
      if (state.model === name) { const rest=[...document.querySelectorAll('#lib button')].map(b=>b.dataset.m).filter(m=>m!==name);
        if (rest.length) await openModel(rest[0]); else $('#btnNew').click(); }
      loadLibrary();
    } catch (err) { log(err.message, 'e'); }
    return;
  }
  const b=e.target.closest('button'); if (b) openModel(b.dataset.m);
});
$('#btnRefresh').onclick = loadLibrary;
$('#btnNew').onclick = () => {
  state.model=null; state.scene=null; state.graph={nodes:[],edges:[]}; state.vars=[]; state.data=null; state.params={}; state.original={};
  editor.setValue('// Describe a new system to Modi in the chat'); $('#crumbName').textContent='new model'; $('#footModel').textContent='no model';
  $('#params').innerHTML='<div class="hint" style="padding:0">Open a model to edit its parameters.</div>'; renderVars(); Plotly.purge('plot');
  state.draft = newDraftId(); drawDiagram(); loadLibrary(); loadThread(null); $('#prompt').focus();
};

async function openModel(name) {
  setStatus('loading', 'busy');
  try {
    const m = await api(`/api/studio/models/${name}`);
    state.model = name; state.code = m.code; state.graph = m.graph || {nodes:[],edges:[]}; state.selComp=null;
    editor.setValue(m.code); $('#btnCheck').textContent='Check';
    $('#crumbName').textContent = name; $('#footModel').textContent = name;
    layoutGraph(); state.scene=null; live.data=null; drawDiagram(true); loadLibrary(); sceneReady = loadScene(true);
    log(`Opened ${name}`);
    loadParams(); loadThread(name);
    state.units = {};
    if (m.has_results) { await loadUnits(); state.vars = await api(`/api/studio/models/${name}/variables`); defaultSelection(); renderVars(); await refreshPlot();
      $('#runInfo').innerHTML = m.results_stale ? '<span style="color:var(--accent)">Results are older than the model. Simulate to update variables and plots.</span>' : 'Last saved results'; }
    else { state.vars=[]; state.data=null; renderVars(); Plotly.purge('plot'); $('#runInfo').textContent='No results'; }
    live.stale = !!m.results_stale; liveStop(); sceneReady.then(prepareLive);
    setStatus('ready', 'ok');
  } catch (e) { setStatus('error','err'); log(e.message,'e'); }
}

async function loadScene(fit) {
  try { state.scene = await api(`/api/studio/models/${state.model}/diagram`); drawDiagram(fit); }
  catch (e) { log('Diagram from OMC unavailable, using simplified view: '+e.message,'e'); state.scene=null; drawDiagram(fit); }
}

/* ---------- parameters ---------- */
async function loadParams() {
  $('#params').innerHTML = '<div class="hint" style="padding:0">Reading parameters from OpenModelica…</div>';
  try {
    const r = await api(`/api/studio/models/${state.model}/parameters`);
    state.params = {...r.parameters}; state.original = {...r.parameters};
    // Always reset the setup for the newly opened model (its experiment annotation, else OMC defaults).
    const o = r.simulation_options || {};
    $('#sStart').value = o.startTime ?? 0;
    $('#sStop').value = o.stopTime ?? 1;
    $('#sN').value = o.numberOfIntervals ?? 500;
    $('#sMethod').value = 'dassl';
    ['#sStart','#sStop','#sN'].forEach(id => { $(id).dataset.ok = $(id).value; }); validateSetup();
    renderParams(); if (state.scene) drawDiagram();  // fill %R-style icon labels
  } catch (e) { $('#params').innerHTML = `<div class="hint" style="padding:0">Could not read parameters: ${esc(e.message)}</div>`; }
}
function renderParams() {
  const groups = {};
  for (const [k,v] of Object.entries(state.params)) {
    const i = k.indexOf('.'); const g = i>0 ? k.slice(0,i) : '(model)'; const p = i>0 ? k.slice(i+1) : k;
    (groups[g] ||= []).push([k,p,v]);
  }
  const names = Object.keys(groups).sort((a,b)=> (b===state.selComp)-(a===state.selComp) || a.localeCompare(b));
  $('#params').innerHTML = names.length ? names.map(g => {
    const node = state.graph.nodes.find(n => n.id===g);
    const typ = node ? node.data.componentType.split('.').pop() : '';
    return `<div class="pgroup ${g===state.selComp?'hl':''}" data-g="${esc(g)}"><h4>${esc(g)}<span>${esc(typ)}</span></h4>` +
      groups[g].map(([k,p,v]) => `<div class="prow"><label title="${esc(k)}">${esc(p)}<span class="eval"></span></label><input data-k="${esc(k)}" data-ok="${esc(v ?? '')}" autocomplete="off" spellcheck="false" value="${esc(v ?? '')}" class="${String(v)!==String(state.original[k])?'dirty':''}"></div>`).join('') + '</div>';
  }).join('') : '<div class="hint" style="padding:0">This model has no parameters.</div>';
  updateDirty(); validateParams();
}
$('#params').addEventListener('input', e => {
  const k = e.target.dataset.k; if (!k) return;
  state.params[k] = e.target.value; e.target.classList.toggle('dirty', String(e.target.value)!==String(state.original[k])); updateDirty();
  if (state.scene) drawDiagram();
});
$('#params').addEventListener('keydown', e => { if (e.key==='Enter' && e.target.dataset.k) simulate(); });
function edits() {  // raw edited values (numbers or expressions) that are valid
  const o={}; for (const k in state.params) { const v=String(state.params[k]).trim();
    if (v!==String(state.original[k]).trim() && v!=='' && checkParam(k, v).ok) o[k]=v; } return o; }
function overrides() {  // numbers for OpenModelica's -override (expressions evaluated with current values)
  const o={}; for (const [k, v] of Object.entries(edits())) { const r=checkParam(k, v); if (r.ok && r.value!=null) o[k]=r.value; } return o; }
function updateDirty() { const n=Object.keys(edits()).length; $('#dirtyCount').textContent = n ? `${n} edited` : ''; }
$('#btnResetParams').onclick = () => { state.params={...state.original}; renderParams(); };
$('#btnBake').onclick = async () => {
  // Server rewrites the values into the source (top-level parameters and component modifiers),
  // then the normal Check saves it, re-validates with OpenModelica and records a revision.
  const values = edits();  // expressions are written as typed (c = 2*k)
  if (!state.model || !Object.keys(values).length) { log('Edit a parameter first, then write it into the model', 'e'); return; }
  try {
    const r = await api(`/api/studio/models/${state.model}/bake`, {method:'POST', body: JSON.stringify({values, code: editor.getValue()})});
    if (!r.applied.length) { log(`Could not find ${r.skipped.join(', ')} in the source`, 'e'); return; }
    editor.setValue(r.code);
    const ok = await check();
    if (ok) { log(`Written into model: ${r.applied.map(k=>`${k}=${values[k]}`).join(', ')}`, 'o'); await loadParams(); }
    if (r.skipped.length) log(`Not written (not found in source): ${r.skipped.join(', ')}`, 'e');
  } catch (e) { log(e.message, 'e'); }
};

/* ---------- animated icons (Modelica DynamicSelect) ---------- */
// Library icons carry DynamicSelect(static, dynamic) graphics, e.g. a tank's level fill or a value text.
// The server tags them with data-dyn-* expressions; during playback they are evaluated with the
// component's own result variables (tank.level, tank.height, ...).
function evalMo(src, lookup) {
  const toks = String(src).match(/"(?:[^"\\]|\\.)*"|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|<>|<=|>=|==|[A-Za-z_][\w.]*|[-+*/^(),{}=<>]/g) || [];
  let i = 0; const peek = () => toks[i], next = () => toks[i++];
  const expect = t => { if (next() !== t) throw new Error('expected ' + t); };
  const FN = {...FUNCS, integer: Math.trunc, noEvent: x => x, smooth: (o, x) => x,
    String: (x, o = {}) => typeof x === 'boolean' ? String(x)
      : typeof x === 'number' ? String(+x.toPrecision(o.significantDigits || 6)) : String(x)};
  function args() { const pos = [], named = {}; if (peek() !== ')') do {
      if (/^[A-Za-z_]/.test(peek()) && toks[i+1] === '=' ) { const k = next(); next(); named[k] = ternary(); } else pos.push(ternary());
    } while (peek() === ',' && next()); expect(')'); return [pos, named]; }
  function primary() {
    const t = next(); if (t === undefined) throw new Error('incomplete');
    if (t[0] === '"') return t.slice(1, -1).replace(/\\(.)/g, '$1');
    if (/^(\d|\.\d)/.test(t)) return parseFloat(t);
    if (t === '(') { const v = ternary(); expect(')'); return v; }
    if (t === '{') { const arr = []; if (peek() !== '}') do arr.push(ternary()); while (peek() === ',' && next()); expect('}'); return arr; }
    if (t === 'true') return true; if (t === 'false') return false;
    if (t === 'not') return !unary();
    if (/^[A-Za-z_]/.test(t)) {
      if (peek() === '(') { next(); const f = FN[t.split('.').pop()]; if (!f) throw new Error('fn ' + t); const [p, n] = args(); return t.endsWith('String') ? f(p[0], n) : f(...p); }
      if (t in CONSTS) return CONSTS[t];
      return lookup(t);
    }
    throw new Error('unexpected ' + t);
  }
  function unary() { if (peek() === '-') { next(); return -unary(); } if (peek() === '+') { next(); return unary(); } return power(); }
  function power() { const b = primary(); if (peek() === '^') { next(); return Math.pow(b, unary()); } return b; }
  function term() { let v = unary(); while (peek() === '*' || peek() === '/') { const op = next(), r = unary(); v = op === '*' ? v*r : v/r; } return v; }
  function sum() { let v = term(); while (peek() === '+' || peek() === '-') { const op = next(), r = term();
    v = op === '+' ? (typeof v === 'string' || typeof r === 'string' ? String(v) + String(r) : v + r) : v - r; } return v; }
  function cmp() { const a = sum(), op = peek(); if (['<','>','<=','>=','==','<>'].includes(op)) { next(); const b = sum();
    return {'<':a<b, '>':a>b, '<=':a<=b, '>=':a>=b, '==':a==b, '<>':a!=b}[op]; } return a; }
  function logic() { let v = cmp(); while (peek() === 'and' || peek() === 'or') { const op = next(), r = cmp(); v = op === 'and' ? (v && r) : (v || r); } return v; }
  function ternary() { if (peek() === 'if') { next(); const c = logic(); expect('then'); const a = ternary();
      while (peek() === 'elseif') { next(); const c2 = logic(); expect('then'); const a2 = ternary(); if (!c && c2) return a2; }
      expect('else'); const b = ternary(); return c ? a : b; } return logic(); }
  const v = ternary(); if (i < toks.length) throw new Error('trailing ' + toks[i]); return v;
}
const DYN_ATTRS = ['visible', 'extent', 'points', 'fill', 'stroke', 'text'];
function dynIdentifiers(svg) {
  // identifiers used by an icon's dynamic expressions (to fetch comp.<id> from the results)
  const ids = new Set(), dec = s => s.replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  for (const m of String(svg).matchAll(/data-dyn-\w+="([^"]*)"/g)) {
    const src = dec(m[1]).replace(/"(?:[^"\\]|\\.)*"/g, '');
    for (const id of src.match(/[A-Za-z_][\w.]*/g) || []) if (!(id in FN_NAMES)) ids.add(id);
  }
  return [...ids];
}
const FN_NAMES = Object.fromEntries([...Object.keys(FUNCS), 'String','integer','noEvent','smooth','if','then','else','elseif','and','or','not',
  'true','false','minimumLength','significantDigits','leftJustified', ...Object.keys(CONSTS)].map(k => [k, 1]));
function applyDynamic(svg) {
  if (!live.data) return;
  const S = live.data.series, i = live.idx;
  svg.querySelectorAll('.comp').forEach(g => {
    const comp = g.dataset.id, isModel = !state.scene.components.some(c => c.name === comp);
    const lookup = id => {
      const key = isModel ? id : `${comp}.${id}`;
      if (S[key]) return S[key][i];
      const p = state.params[key]; if (p != null && NUM_FULL.test(String(p))) return +p;
      throw new Error('unknown ' + id);
    };
    g.querySelectorAll(DYN_ATTRS.map(a => `[data-dyn-${a}]`).join(',')).forEach(el => {
      for (const a of DYN_ATTRS) {
        const src = el.getAttribute('data-dyn-' + a); if (!src) continue;
        let v; try { v = evalMo(src, lookup); } catch { continue; }  // unknown variable: keep the static drawing
        if (a === 'visible') el.style.display = v ? '' : 'none';
        else if (a === 'text') el.textContent = String(v);
        else if ((a === 'fill' || a === 'stroke') && Array.isArray(v) && v.length >= 3) el.setAttribute(a, `rgb(${v.map(Math.round).join(',')})`);
        else if (a === 'extent' && Array.isArray(v) && v.length === 2) {
          const [[x1, y1], [x2, y2]] = v;
          if (el.tagName === 'ellipse') { el.setAttribute('cx', (x1+x2)/2); el.setAttribute('cy', (y1+y2)/2); el.setAttribute('rx', Math.abs(x2-x1)/2); el.setAttribute('ry', Math.abs(y2-y1)/2); }
          else { el.setAttribute('x', Math.min(x1,x2)); el.setAttribute('y', Math.min(y1,y2)); el.setAttribute('width', Math.abs(x2-x1)); el.setAttribute('height', Math.abs(y2-y1)); }
        } else if (a === 'points' && Array.isArray(v) && v.length >= 2) {
          el.setAttribute('d', 'M' + v.map(p => `${p[0]} ${p[1]}`).join(' L') + (el.dataset.closed ? ' Z' : ''));
        }
      }
    });
  });
}

/* ---------- live results on the diagram ---------- */
// Before a run the diagram is just the model. After a run it shows values at the player's time:
// badges on components, animated flow on wires, a time player, and a marker on the plot.
let sceneReady = Promise.resolve();
const live = {data:null, t:null, idx:0, playing:false, raf:0, last:0, phase:{}, keys:{}, flows:[], range:{}, stale:false, dur:6};
const LIVE_PREFS = ['v','i','s','v_rel','s_rel','w','phi','w_rel','T','f','tau','y','Q_flow','m_flow','p','LossPower'];
const PREFIXABLE = new Set(['V','A','W','N','Pa','J','F','H','Ohm','Hz','s','m','g','C','S','Wb','T','N.m','m/s','rad/s']);
function fmtVal(x, unit) {
  // SI prefixes only for units where they read naturally (mV, kN, µF); otherwise plain numbers with the unit.
  if (typeof x !== 'number') return String(x);
  if (unit && PREFIXABLE.has(unit)) return fmtSI(x).trim().replace(/ (\S*)$/, ' $1') + unit;
  const a = Math.abs(x), n = (a !== 0 && (a < 1e-3 || a >= 1e6)) ? x.toExponential(3) : String(+x.toPrecision(4));
  return unit ? `${n} ${unit}` : n;
}
function fmtSI(x) {
  if (!isFinite(x)) return String(x);
  const a=Math.abs(x); if (a===0) return '0 ';
  const P=[[1e9,'G'],[1e6,'M'],[1e3,'k'],[1,''],[1e-3,'m'],[1e-6,'µ'],[1e-9,'n']];
  for (const [f,u] of P) if (a>=f*0.9995) return (x/f).toPrecision(4).replace(/(\.\d*?)0+$/,'$1').replace(/\.$/,'')+' '+u;
  return x.toExponential(2)+' ';
}
function liveHide(msg) {
  live.data=null; liveStop();
  $('#liveBar').classList.add('off'); $('#liveHint').textContent = msg || ''; $('#liveHint').style.display = msg ? '' : 'none';
}
async function prepareLive() {
  if (!state.model) return liveHide('');
  if (!state.vars.length) return liveHide('Simulate to see live values on the diagram');
  if (live.stale) return liveHide('Results are older than the model. Simulate to see live values.');
  const vars=new Set(state.vars), keys={}, flows=[];
  const comps = state.scene ? state.scene.components : [];
  comps.forEach(c => { keys[c.name] = LIVE_PREFS.map(p=>`${c.name}.${p}`).filter(v=>vars.has(v)).slice(0,2); });
  if (!comps.length) keys['@model'] = state.vars.filter(v => !v.includes('.') && !v.startsWith('der(')).slice(0,6);
  (state.scene ? state.scene.wires : []).forEach((w,i) => {
    for (const [end, sign] of [[w.from, 1], [w.to, -1]]) {
      const v = ['i','f','Q_flow','m_flow','tau'].map(q=>`${end}.${q}`).find(x=>vars.has(x));
      if (v) { flows.push({i, v, sign}); break; }
    }
  });
  const dynVars = [];
  comps.forEach(c => dynIdentifiers(c.svg).forEach(id => { if (vars.has(`${c.name}.${id}`)) dynVars.push(`${c.name}.${id}`); }));
  if (!comps.length && state.scene && state.scene.model_icon) dynIdentifiers(state.scene.model_icon.svg).forEach(id => { if (vars.has(id)) dynVars.push(id); });
  const want=[...new Set([...Object.values(keys).flat(), ...flows.map(f=>f.v), ...dynVars])];
  if (!want.length) return liveHide('');
  try { live.data = await api(`/api/studio/models/${state.model}/results?vars=${encodeURIComponent(want.join(','))}`); }
  catch { return liveHide(''); }
  live.keys=keys; live.flows=flows; live.range={}; live.phase={};
  for (const [v, ys] of Object.entries(live.data.series)) if (ys) {
    let lo=Infinity, hi=-Infinity; for (const y of ys) { if (y<lo) lo=y; if (y>hi) hi=y; }
    live.range[v] = {lo, hi, amax: Math.max(Math.abs(lo), Math.abs(hi)) || 1};
  }
  // equation-based models: hide constants (parameters) when real states exist
  if (keys['@model']) { const dyn = keys['@model'].filter(v => live.range[v] && live.range[v].hi!==live.range[v].lo); if (dyn.length) keys['@model'] = dyn.slice(0,4); }
  const T=live.data.time; const sl=$('#liveSlider'); sl.max = String(T.length-1);
  liveSeek(T.length-1);  // start on the final values
  $('#liveBar').classList.remove('off'); $('#liveHint').style.display='none';
}
function liveSeek(i) {
  const T = live.data ? live.data.time : []; if (!T.length) return;
  live.idx = Math.max(0, Math.min(T.length-1, Math.round(i))); live.t = T[live.idx];
  $('#liveSlider').value = String(live.idx);
  $('#liveTime').textContent = `t = ${fmtSI(live.t)}s / ${fmtSI(T[T.length-1])}s`;
  drawDiagram(); livePlotMarker();
}
let markerTimer=0;
function livePlotMarker() {
  if (!state.data || live.t==null) return;
  const now=performance.now(); if (now-markerTimer < 90 && live.playing) return; markerTimer=now;
  try { Plotly.relayout('plot', {shapes:[{type:'line', xref:'x', yref:'paper', x0:live.t, x1:live.t, y0:0, y1:1, line:{color:cssVar('--accent'), width:1, dash:'dot'}}]}); } catch {}
}
function liveStop() { live.playing=false; cancelAnimationFrame(live.raf); const b=$('#livePlay'); if (b) b.textContent='▶'; }
function livePlay() {
  if (!live.data) return;
  if (live.playing) return liveStop();
  const T=live.data.time; if (live.idx >= T.length-1) live.idx = 0;
  live.playing=true; $('#livePlay').textContent='❚❚'; live.last=performance.now();
  const span=T[T.length-1]-T[0] || 1;
  const step = now => {
    if (!live.playing) return;
    const dt=(now-live.last)/1000; live.last=now;
    const tNext = live.t + dt*span/live.dur;
    let i=live.idx; while (i<T.length-1 && T[i+1]<=tNext) i++;
    live.flows.forEach(f => { const r=live.range[f.v], y=live.data.series[f.v][i]*f.sign;
      live.phase[f.i] = (live.phase[f.i]||0) + dt*40*Math.sign(y)*Math.min(1, Math.abs(y)/(r?r.amax:1)+0.15*(y!==0)); });
    if (i>=T.length-1) { liveSeek(T.length-1); return liveStop(); }
    live.idx=i; live.t=tNext; $('#liveSlider').value=String(i);
    $('#liveTime').textContent = `t = ${fmtSI(T[i])}s / ${fmtSI(T[T.length-1])}s`;
    drawDiagram(); livePlotMarker();
    live.raf=requestAnimationFrame(step);
  };
  live.raf=requestAnimationFrame(step);
}
function liveOverlay() {
  if (!live.data || !state.scene) return '';
  const S=live.data.series, i=live.idx, fs=11/view.k, out=[];
  const badge = (x, y, vars) => {
    vars.forEach((v,k) => { const ys=S[v]; if (!ys) return; const val=ys[i], r=live.range[v];
      const label = `${v.split('.').pop()} = ${fmtVal(val, unitOf(v))}`, yy=y-k*fs*1.9, w=fs*0.62*label.length+fs;
      const frac = r ? Math.min(1, Math.abs(val)/r.amax) : 0;
      out.push(`<g transform="translate(${x} ${yy}) scale(1 -1)"><rect class="lv-bg" x="${-w/2}" y="${-fs*0.95}" width="${w}" height="${fs*1.55}" rx="${fs*0.3}"/>
        <rect class="lv-bar" x="${-w/2}" y="${fs*0.42}" width="${w*frac}" height="${fs*0.18}"/>
        <text class="lv-tx" font-size="${fs}" text-anchor="middle" dominant-baseline="central" y="${-fs*0.2}">${esc(label)}</text></g>`); });
  };
  state.scene.components.forEach(c => {
    const vars=live.keys[c.name]||[]; if (!vars.length) return;
    const [x1,y1,x2,y2]=c.coord, pts=[[x1,y1],[x2,y1],[x1,y2],[x2,y2]].map(([u,v])=>mapPt(c.matrix,u,v));
    const cx=(Math.min(...pts.map(p=>p[0]))+Math.max(...pts.map(p=>p[0])))/2, bottom=Math.min(...pts.map(p=>p[1]));
    badge(cx, bottom - fs*1.2, vars);
  });
  if (live.keys['@model']) badge(0, -70, live.keys['@model']);
  live.flows.forEach(f => { const w=state.scene.wires[f.i]; if (!w || w.points.length<2) return;
    const y=S[f.v] ? S[f.v][i] : 0, r=live.range[f.v], mag=r ? Math.abs(y)/r.amax : 0;
    if (mag < 0.01) return;
    out.push(`<polyline class="lv-flow" points="${w.points.map(p=>p.join(',')).join(' ')}" style="stroke-dashoffset:${-(live.phase[f.i]||0)};opacity:${0.35+0.65*mag}"/>`); });
  return `<g class="live">${out.join('')}</g>`;
}

/* ---------- diagram ---------- */
const NODE_W=150, NODE_H=86;
let view = {x:0,y:0,k:1};
function layoutGraph() {
  // Give nodes without positions a grid slot; keep stored positions otherwise.
  state.graph.nodes.forEach((n,i) => { n.position ||= {x: 100 + (i%4)*220, y: 120 + Math.floor(i/4)*170}; });
}
const SYMBOLS = {
  resistor: '<path class="sym" d="M-34 0h10l4-8 8 16 8-16 8 16 8-16 4 8h10"/>',
  capacitor: '<path class="sym" d="M-34 0h30M-4-12v24M4-12v24M4 0h30"/>',
  inductor: '<path class="sym" d="M-34 0h8a6 6 0 0 1 12 0a6 6 0 0 1 12 0a6 6 0 0 1 12 0a6 6 0 0 1 12 0h8"/>',
  constantvoltage: '<circle class="sym" r="13"/><path class="sym" d="M-34 0h21M13 0h21M-6 0h5M-3.5-2.5v5M2 0h5"/>',
  stepvoltage: '<circle class="sym" r="13"/><path class="sym" d="M-34 0h21M13 0h21M-7 5h6v-10h8"/>',
  sinevoltage: '<circle class="sym" r="13"/><path class="sym" d="M-34 0h21M13 0h21M-8 0q4-9 8 0t8 0"/>',
  ground: '<path class="sym" d="M0-16v10M-16-6h32M-10 0h20M-4 6h8"/>',
  mass: '<rect class="sym" x="-16" y="-12" width="32" height="24"/><path class="sym" d="M-34 0h18M16 0h18"/>',
  spring: '<path class="sym" d="M-34 0h10l4-8 6 16 6-16 6 16 6-16 4 8h10"/>',
  damper: '<path class="sym" d="M-34 0h22M-12-10v20M-18-10h22v20h-22M-6 0h40"/>',
  fixed: '<path class="sym" d="M-16 0h32M-14 0l-6 8M-6 0l-6 8M2 0l-6 8M10 0l-6 8"/>',
};
function symbolFor(n) {
  const t = (n.type||'').toLowerCase();
  if (SYMBOLS[t]) return SYMBOLS[t];
  for (const k in SYMBOLS) if (t.includes(k)) return SYMBOLS[k];
  if (t.includes('source') || t.includes('voltage')) return SYMBOLS.constantvoltage;
  return `<rect class="sym" x="-18" y="-12" width="36" height="24" rx="3"/><path class="sym" d="M-34 0h16M18 0h16"/>`;
}
function iconFor(n) {
  const cls = n.data?.componentType || '';
  const fb = `<g transform="translate(${NODE_W/2},${NODE_H/2-8})">${symbolFor(n)}</g>`;
  if (!/^Modelica\./.test(cls)) return fb;
  // Original MSL icon (backend proxies + caches doc.modelica.org); fallback symbol shows if it fails.
  return `<g class="fb" style="display:none">${fb}</g><image href="${API}/api/studio/icon/${cls}" x="${NODE_W/2-26}" y="6" width="52" height="52"
    preserveAspectRatio="xMidYMid meet" onerror="this.previousElementSibling.style.display='';this.remove()"/>`;
}
function portPos(n, handle) {
  const p=n.position, cx=p.x+NODE_W/2, cy=p.y+NODE_H/2-8;
  const h=(handle||'').toLowerCase();
  if ((n.type||'').includes('ground')) return [cx, p.y+10];
  if (h==='n' || h.startsWith('flange_b') || h==='b' || h.endsWith('_b')) return [cx+34, cy];
  return [cx-34, cy];
}
/* Scene from OMC graphical API (original icons, Modelica coords, y up). */
function sceneBBox() {
  const sc=state.scene; let xs=[], ys=[];
  sc.components.forEach(c => { const [x1,y1,x2,y2]=c.coord; [[x1,y1],[x2,y1],[x1,y2],[x2,y2]].forEach(([u,v]) => { const [x,y]=mapPt(c.matrix,u,v); xs.push(x); ys.push(y); }); });
  sc.wires.forEach(w => w.points.forEach(([x,y]) => { xs.push(x); ys.push(y); }));
  if (!sc.components.length && sc.model_icon) { xs.push(-60,60); ys.push(-60,60); }
  if (sc.background || !xs.length) { const [a,b,c,d]=sc.extent; xs.push(a,c); ys.push(b,d); }
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}
const mapPt = (m,u,v) => [m[0]*u+m[2]*v+m[4], m[1]*u+m[3]*v+m[5]];
function substitute(t, name) {
  return t.replace(/%(name|class|[A-Za-z_][\w.]*)/g, (all, key) => {
    if (key==='name') return name;
    if (key==='class') { const c=state.scene.components.find(c=>c.name===name); return c ? c.type.split('.').pop() : ''; }
    const comp = state.scene.components.find(c=>c.name===name) || {};
    let v = state.params[`${name}.${key}`] ?? (comp.modifiers||{})[key] ?? (comp.defaults||{})[key];
    if (v!=null && /^[A-Za-z_]\w*$/.test(String(v)) && state.params[v]!=null) v = state.params[v];  // c=k -> c=10
    return v==null ? all : String(v);
  }).replace(/%%/g,'%');
}
function drawScene(fit) {
  const svg=$('#diagram'), sc=state.scene;
  const nothing = !sc.components.length && !sc.background && !sc.model_icon;
  const eqb = !sc.components.length;
  $('#diagEmpty').style.display = eqb ? 'flex' : 'none';
  $('#diagEmpty').classList.toggle('bottom', eqb && !nothing);
  $('#diagEmpty').innerHTML = !eqb ? '' : nothing
    ? 'Equation-based model<br><small>No components or diagram graphics to draw. See the Modelica tab, or ask Modi to add an icon or rebuild it from library components.</small>'
    : '<small>Equation-based model: showing its icon and diagram graphics. Ask Modi to rebuild it from library components for a full diagram.</small>';
  if (fit) fitScene();
  const wires = sc.wires.map(w => w.points.length<2 ? '' :
    `<polyline class="wire2" points="${w.points.map(p=>p.join(',')).join(' ')}" stroke="${w.color}"/>`).join('');
  const comps = sc.components.map(c => {
    const [x1,y1,x2,y2]=c.coord;
    return `<g class="comp ${c.name===state.selComp?'sel':''}" data-id="${esc(c.name)}" transform="matrix(${c.matrix.join(' ')})">
      <rect class="hit" x="${x1}" y="${y1}" width="${x2-x1}" height="${y2-y1}"/><g class="ico">${c.svg}</g><title>${esc(c.name)} : ${esc(c.type)}${c.comment?'\n'+esc(c.comment):''}</title></g>`;
  }).join('');
  let icon = '';
  if (eqb && sc.model_icon) {  // fit the model's own icon into a 120x120 box at the centre
    const [a,b,c,d]=sc.model_icon.coord, k=120/Math.max(c-a, d-b);
    icon = `<g class="comp" data-id="${esc(state.model||'')}" transform="matrix(${k} 0 0 ${k} ${-(a+c)/2*k} ${-(b+d)/2*k})"><g class="ico">${sc.model_icon.svg}</g></g>`;
  }
  svg.innerHTML = `<g id="vp" transform="translate(${view.x},${view.y}) scale(${view.k},${-view.k})"><g class="bg">${sc.background||''}</g>${icon}${wires}${comps}${liveOverlay()}</g>`;
  svg.querySelectorAll('.bg text[data-t]').forEach(t => { t.textContent = t.dataset.t.replace(/%name/g, state.model||''); });
  svg.querySelectorAll('.comp').forEach(g => g.querySelectorAll('text[data-t]').forEach(t => { t.textContent = substitute(t.dataset.t, g.dataset.id); }));
  applyDynamic(svg);  // animated icon graphics at the player's time
}
function fitScene() {
  const svg=$('#diagram'); if (!svg.clientWidth) return;
  let [x0,y0,x1,y1]=sceneBBox(); const pad=Math.max(x1-x0,y1-y0)*0.08+5; x0-=pad; y0-=pad; x1+=pad; y1+=pad;
  const k=Math.min(svg.clientWidth/(x1-x0), svg.clientHeight/(y1-y0));
  view={k, x:(svg.clientWidth-(x1-x0)*k)/2 - x0*k, y:(svg.clientHeight+(y1-y0)*k)/2 + y0*k};
}
function drawDiagram(fit=false) {
  const dg=$('#diagram'); dg.classList.toggle('paper', !!state.scene);
  dg.classList.toggle('dark-canvas', theme.dark && theme.canvas==='theme');
  dg.classList.toggle('light-canvas', theme.canvas==='light');
  if (state.scene) return drawScene(fit);
  const svg=$('#diagram'), g=state.graph;
  $('#diagEmpty').style.display = g.nodes.length ? 'none' : 'flex';
  if (fit) fitView();
  const byId = Object.fromEntries(g.nodes.map(n=>[n.id,n]));
  const wires = g.edges.map(e => {
    const a=byId[e.source], b=byId[e.target]; if (!a||!b) return '';
    const [x1,y1]=portPos(a,e.sourceHandle), [x2,y2]=portPos(b,e.targetHandle);
    const midY = Math.max(y1,y2) + (Math.abs(x1-x2)<40 ? 0 : 0);
    const d = Math.abs(y1-y2)<2 ? `M${x1} ${y1}H${x2}` : `M${x1} ${y1}H${(x1+x2)/2}V${y2}H${x2}`;
    return `<path class="wire" d="${d}"/><circle cx="${x1}" cy="${y1}" r="2.5" fill="var(--wire)"/><circle cx="${x2}" cy="${y2}" r="2.5" fill="var(--wire)"/>`;
  }).join('');
  const nodes = g.nodes.map(n => {
    const typ=(n.data?.componentType||n.type||'').split('.').pop();
    const val = paramSummary(n.id);
    return `<g class="comp ${n.id===state.selComp?'sel':''}" data-id="${esc(n.id)}" transform="translate(${n.position.x},${n.position.y})">
      <rect class="body" width="${NODE_W}" height="${NODE_H}" rx="8"/>
      ${iconFor(n)}
      <text class="lbl" x="${NODE_W/2}" y="${NODE_H-16}">${esc(n.id)}${val?' · '+esc(val):''}</text>
      <text class="typ" x="${NODE_W/2}" y="${NODE_H-4}">${esc(typ)}</text></g>`;
  }).join('');
  svg.innerHTML = `<g id="vp" transform="translate(${view.x},${view.y}) scale(${view.k})">${wires}${nodes}</g>`;
}
function paramSummary(id) {
  const main = {R:'Ω',C:'F',L:'H',V:'V',m:'kg',c:'N/m',d:'Ns/m',I:'A',J:'kg·m²'};
  for (const p in main) { const v=state.params[`${id}.${p}`]; if (v!=null && v!=='') return `${v} ${main[p]}`; }
  return '';
}
function fitView() {
  const n=state.graph.nodes; const svg=$('#diagram'); if (!n.length || !svg.clientWidth) return;
  const xs=n.map(a=>a.position.x), ys=n.map(a=>a.position.y);
  const x0=Math.min(...xs)-40, y0=Math.min(...ys)-40, x1=Math.max(...xs)+NODE_W+40, y1=Math.max(...ys)+NODE_H+40;
  const k=Math.min(1.6, svg.clientWidth/(x1-x0), svg.clientHeight/(y1-y0));
  view={k, x:(svg.clientWidth-(x1-x0)*k)/2 - x0*k, y:(svg.clientHeight-(y1-y0)*k)/2 - y0*k};
}
$('#btnFit').onclick = () => drawDiagram(true);
function moveWires(name, dx, dy) {
  // Drag the wire ends attached to a component; keep the adjacent segment orthogonal.
  state.scene.wires.forEach(w => {
    const ends = [];
    if (w.from.split('.')[0]===name) ends.push([0,1]);
    if (w.to.split('.')[0]===name) ends.push([w.points.length-1, w.points.length-2]);
    ends.forEach(([i,j]) => { const p=w.points[i], q=w.points[j];
      if (q && w.points.length>2) { if (Math.abs(q[0]-p[0])<1e-6) q[0]+=dx; else if (Math.abs(q[1]-p[1])<1e-6) q[1]+=dy; }
      p[0]+=dx; p[1]+=dy; });
  });
}
(() => { // pan, zoom, drag, select
  const svg=$('#diagram'); let drag=null;
  svg.addEventListener('mousedown', e => {
    const c=e.target.closest('.comp');
    if (c && state.scene) { const sc=state.scene.components.find(x=>x.name===c.dataset.id); drag={sc, sx:e.clientX, sy:e.clientY, ox:sc.matrix[4], oy:sc.matrix[5], moved:false, lx:0, ly:0}; }
    else if (c) { const n=state.graph.nodes.find(x=>x.id===c.dataset.id); drag={n, sx:e.clientX, sy:e.clientY, ox:n.position.x, oy:n.position.y, moved:false}; }
    else drag={pan:true, sx:e.clientX, sy:e.clientY, ox:view.x, oy:view.y};
  });
  addEventListener('mousemove', e => {
    if (!drag) return; const dx=e.clientX-drag.sx, dy=e.clientY-drag.sy;
    if (drag.pan) { view.x=drag.ox+dx; view.y=drag.oy+dy; }
    else if (drag.sc) { if (Math.abs(dx)+Math.abs(dy)>3) drag.moved=true; if (!drag.moved) return;
      const gx=Math.round(dx/view.k/2)*2, gy=Math.round(-dy/view.k/2)*2, ddx=gx-drag.lx, ddy=gy-drag.ly; drag.lx=gx; drag.ly=gy;
      drag.sc.matrix[4]=drag.ox+gx; drag.sc.matrix[5]=drag.oy+gy; moveWires(drag.sc.name, ddx, ddy); }
    else { if (Math.abs(dx)+Math.abs(dy)>3) drag.moved=true; drag.n.position.x=Math.round((drag.ox+dx/view.k)/10)*10; drag.n.position.y=Math.round((drag.oy+dy/view.k)/10)*10; }
    drawDiagram();
  });
  addEventListener('mouseup', () => {
    if (drag && (drag.n||drag.sc) && !drag.moved) { const id = drag.n ? drag.n.id : drag.sc.name; state.selComp = state.selComp===id ? null : id; drawDiagram(); renderParams();
      const el=document.querySelector(`.pgroup[data-g="${CSS.escape(state.selComp||'')}"]`); el && el.scrollIntoView({behavior:'smooth',block:'nearest'}); }
    drag=null;
  });
  svg.addEventListener('wheel', e => { e.preventDefault(); const r=svg.getBoundingClientRect(), mx=e.clientX-r.left, my=e.clientY-r.top;
    const k=Math.max(.02, Math.min(60, view.k*(e.deltaY<0?1.1:1/1.1))); view.x=mx-(mx-view.x)*k/view.k; view.y=my-(my-view.y)*k/view.k; view.k=k; drawDiagram(); }, {passive:false});
  svg.addEventListener('dblclick', e => { const c=e.target.closest('.comp'); if (!c) return; // jump to declaration in code
    const i=editor.getValue().split('\n').findIndex(l=>new RegExp(`\\b${c.dataset.id}\\b`).test(l) && !/connect/.test(l));
    if (i>=0) { document.querySelector('.tab[data-v="split"]').click(); editor.setCursor(i,0); editor.focus(); } });
  new ResizeObserver(() => drawDiagram()).observe(svg);
})();

/* ---------- check & simulate ---------- */
let errorMarks = [];
function markErrors(text) {
  // OMC diagnostics look like  [/app/models/X/X.mo:12:5-12:20:writable] Error: ...
  errorMarks.forEach(h => editor.removeLineClass(h, 'background', 'cm-error-line')); errorMarks = [];
  const lines = [...String(text||'').matchAll(/\.mo:(\d+):\d+/g)].map(m => +m[1]-1);
  [...new Set(lines)].forEach(l => { if (l>=0 && l<editor.lineCount()) errorMarks.push(editor.addLineClass(l, 'background', 'cm-error-line')); });
  if (errorMarks.length) editor.scrollIntoView({line: editor.getLineNumber(errorMarks[0]), ch: 0}, 80);
}
async function check() {
  if (!state.model) return;
  setStatus('checking','busy'); log('Checking model with OpenModelica…');
  try {
    const r = await api(`/api/studio/models/${state.model}`, {method:'PUT', body: JSON.stringify({code: editor.getValue()})});
    state.code = editor.getValue(); $('#btnCheck').textContent='Check';
    if (r.status==='SUCCESS') {
      markErrors('');
      setStatus('check ok','ok'); log(r.message ? r.message.replace(/\s+/g,' ') : 'checkModel: model is valid','o');
      if (r.warnings) log(r.warnings);
      if (r.graph) { state.graph=r.graph; layoutGraph(); }
      loadScene(false);
      if (r.changed) {  // refresh the parameter panel, keeping values the user is experimenting with
        const keep = edits(); await loadParams();
        for (const k in keep) if (k in state.params) state.params[k] = keep[k];
        renderParams(); if (state.scene) drawDiagram();
      }
      if (r.results_stale) { live.stale=true; liveHide('Results are older than the model. Simulate to see live values.'); }
      if (r.results_stale) $('#runInfo').innerHTML = '<span style="color:var(--accent)">Results are older than the model. Simulate to update variables and plots.</span>';
    } else {
      setStatus('check failed','err'); log(r.error || 'Check failed','e'); markErrors(r.error); offerFix('check', r.error || 'Check failed');
      document.querySelector('.tab[data-v="split"]').click();  // show the code with the error lines next to the diagram
    }
    return r.status==='SUCCESS';
  } catch (e) { setStatus('error','err'); log(e.message,'e'); return false; }
}
async function simulate() {
  if (!state.model) { log('Open or generate a model first','e'); return; }
  if (!validateAll()) { setStatus('fix inputs','err'); log('Fix the highlighted fields first: numbers only', 'e'); return; }
  if (editor.getValue()!==state.code && !(await check())) return;
  const body = { start_time:+$('#sStart').value, stop_time:+$('#sStop').value, number_of_intervals:parseInt($('#sN').value),
    method:$('#sMethod').value, parameter_overrides: overrides() };
  setStatus('simulating','busy'); $('#btnSim').disabled=true; const t0=performance.now();
  log(`Simulating ${state.model} [${body.start_time}, ${body.stop_time}] s with ${body.method}` + (Object.keys(body.parameter_overrides).length ? ` · overrides ${JSON.stringify(body.parameter_overrides)}` : ''));
  try {
    const r = await api(`/api/studio/models/${state.model}/run`, {method:'POST', body: JSON.stringify(body)});
    if (r.status!=='SUCCESS') throw new Error(r.messages || 'Simulation failed');
    if (r.messages) log(r.messages.trim());
    state.vars = r.variables; if (!state.selected.size || ![...state.selected].some(v=>state.vars.includes(v))) defaultSelection();
    await loadUnits(); renderVars(); await refreshPlot(); live.stale=false; prepareLive();
    const dt=((performance.now()-t0)/1000).toFixed(2);
    $('#runInfo').textContent = `${state.model} · ${body.method} · ${body.number_of_intervals} intervals · ${dt}s wall`;
    setStatus('done','ok'); log(`Simulation finished in ${dt}s`,'o'); loadLibrary();
  } catch (e) { setStatus('sim failed','err'); log(e.message,'e'); offerFix('simulation', e.message); }
  finally { $('#btnSim').disabled=false; }
}
$('#btnCheck').onclick = check; $('#btnSim').onclick = simulate;
addEventListener('keydown', e => {
  const mod = e.metaKey||e.ctrlKey;
  if (mod && e.key==='s') { e.preventDefault(); check(); }
  if (mod && e.key==='Enter') { e.preventDefault(); simulate(); }
});

/* ---------- variables & plot ---------- */
function defaultSelection() {
  state.selected = new Set();
  const prefer = state.vars.filter(v => !v.startsWith('der(') && /\.(v|i|phi|s|w|T)$/.test(v) && !/\.(p|n|flange|heatPort)/.test(v));
  (prefer.length ? prefer : state.vars).slice(0,2).forEach(v=>state.selected.add(v));
}
function renderVars() {
  const q=$('#vsearch').value.toLowerCase(), groups={};
  state.vars.filter(v=>v.toLowerCase().includes(q)).forEach(v => { const m=v.replace(/der\(|\)|\$/g,'').split('.')[0]; (groups[m] ||= []).push(v); });
  const keys = Object.keys(groups).sort();
  $('#vlist').innerHTML = keys.length ? keys.map(g => `<details class="vgroup" ${q||groups[g].some(v=>state.selected.has(v))?'open':''}><summary>${esc(g)} <span style="color:var(--dim)">${groups[g].length}</span></summary>` +
    groups[g].map(v=>`<label><input type="checkbox" data-v="${esc(v)}" ${state.selected.has(v)?'checked':''}>${esc(v)}</label>`).join('') + '</details>').join('')
    : `<div class="hint">${state.vars.length?'No match.':'Simulate to browse variables.'}</div>`;
}
$('#vsearch').oninput = renderVars;
$('#vlist').addEventListener('change', e => { const v=e.target.dataset.v; if (!v) return; e.target.checked ? state.selected.add(v) : state.selected.delete(v); refreshPlot(); });
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const palette = () => [1,2,3,4,5,6,7,8].map(i => cssVar('--s'+i));
async function refreshPlot() {
  if (!state.model || !state.vars.length) return;
  const vars=[...state.selected];
  state.data = vars.length ? await api(`/api/studio/models/${state.model}/results?vars=${encodeURIComponent(vars.join(','))}`) : {time:[],series:{}};
  drawPlot();
}
function unitOf(v) {
  // Real units from OpenModelica's model description; a name-based guess only if none were loaded.
  if (state.units && Object.keys(state.units).length) return state.units[v] || '';
  const s=v.split('.').pop(); return {i:'A',phi:'rad',w:'rad/s',T:'K',LossPower:'W'}[s] || '';
}
async function loadUnits() { try { state.units = await api(`/api/studio/models/${state.model}/units`); } catch { state.units = {}; } }
function drawPlot() {
  const d=state.data; if (!d) return;
  const P=palette(), C={grid:cssVar('--grid'), line:cssVar('--line2'), muted:cssVar('--muted'), text:cssVar('--text'),
    paper:cssVar('--panel'), plot:cssVar('--plot-bg'), tip:cssVar('--panel2')};
  const traces=[];
  const vars=Object.keys(d.series).filter(v=>d.series[v]);
  const units=[...new Set(vars.map(unitOf))];
  vars.forEach((v,i) => traces.push({x:d.time, y:d.series[v], name:v, mode:'lines', line:{color:P[i%P.length], width:2}, yaxis: units.indexOf(unitOf(v))===1?'y2':'y'}));
  state.pins.forEach((p,pi) => Object.keys(p.series).forEach(v => { if (!p.series[v] || !vars.includes(v)) return; const i=vars.indexOf(v);
    traces.push({x:p.time, y:p.series[v], name:`${v} · ${p.label}`, mode:'lines', line:{color:P[i%P.length], width:1.4, dash:['dash','dot','dashdot'][pi%3]}, opacity:.7, yaxis: units.indexOf(unitOf(v))===1?'y2':'y'}); }));
  const mono='IBM Plex Mono, ui-monospace, monospace';
  const ax = {gridcolor:C.grid, zerolinecolor:C.line, linecolor:C.line, tickfont:{family:mono, size:11, color:C.muted}, exponentformat:'SI'};
  const layout = {
    paper_bgcolor:C.paper, plot_bgcolor:C.plot, margin:{l:60,r:units.length>1?60:20,t:10,b:40}, font:{family:'IBM Plex Sans, system-ui, sans-serif', color:C.text},
    xaxis:{...ax, title:{text:'time [s]', font:{size:11,color:C.muted}}},
    yaxis:{...ax, title:{text:units[0]?`[${units[0]}]`:'', font:{size:11,color:C.muted}}},
    legend:{orientation:'h', y:1.02, yanchor:'bottom', x:0, font:{family:mono, size:11}}, hovermode:'x unified',
    hoverlabel:{bgcolor:C.tip, bordercolor:C.line, font:{family:mono, color:C.text}}
  };
  // Only define yaxis2 when a second unit exists: an undefined yaxis2 key crashes Plotly ("reading 'anchor'").
  if (units.length>1) layout.yaxis2 = {...ax, overlaying:'y', side:'right', showgrid:false, title:{text:`[${units[1]}]`, font:{size:11,color:C.muted}}};
  const cfg = {displaylogo:false, responsive:true, modeBarButtonsToRemove:['lasso2d','select2d']};
  if (live.data && live.t!=null) layout.shapes=[{type:'line', xref:'x', yref:'paper', x0:live.t, x1:live.t, y0:0, y1:1, line:{color:cssVar('--accent'), width:1, dash:'dot'}}];
  try { Plotly.react('plot', traces, layout, cfg); } catch (err) { Plotly.purge('plot'); Plotly.newPlot('plot', traces, layout, cfg); }
}
$('#btnPin').onclick = () => { if (!state.data) return;
  const o=overrides(); const label = Object.keys(o).length ? Object.entries(o).map(([k,v])=>`${k.split('.').pop()}=${v}`).join(' ') : `run ${state.pins.length+1}`;
  state.pins.push({...JSON.parse(JSON.stringify(state.data)), label}); log(`Pinned "${label}" for comparison`); drawPlot(); };
$('#btnClearPin').onclick = () => { state.pins=[]; drawPlot(); };
$('#btnCsv').onclick = () => { const d=state.data; if (!d) return; const vs=Object.keys(d.series).filter(v=>d.series[v]);
  const rows=[['time',...vs].join(',')].concat(d.time.map((t,i)=>[t,...vs.map(v=>d.series[v][i])].join(',')));
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([rows.join('\n')],{type:'text/csv'})); a.download=`${state.model}_results.csv`; a.click(); };

/* ---------- Modi chat ---------- */
function addMsg(cls, html) { const d=document.createElement('div'); d.className='msg '+cls; d.innerHTML=html; $('#msgs').appendChild(d); $('#msgs').scrollTop=1e9; return d; }
const newDraftId = () => '_new_' + Math.random().toString(36).slice(2, 10);  // fresh thread per "+ New"
const INTRO = `Hi, I'm Modi. Describe a physical system and I'll write the Modelica model, compile it with OpenModelica (fixing errors on my own), pick solver settings, and simulate it.`;
async function loadThread(name) {
  // Restore this model's conversation so the user can pick up where they left off.
  state.thread = name || state.draft || (state.draft = newDraftId());
  const box = $('#msgs'); box.innerHTML = '';
  try {
    const [msgs, pend] = await Promise.all([api(`/api/studio/models/${state.thread}/messages`),
      api(`/api/studio/agent/pending?model=${encodeURIComponent(state.thread)}`)]);
    const pendingId = pend.pending && pend.pending.proposal_id;
    if (!msgs.length) addMsg('bot', esc(name ? `No conversation about ${name} yet. I'm Modi: ask me to change, explain or simulate it.` : INTRO));
    let day = '';
    msgs.forEach(m => {
      const d = new Date(m.created*1000), ds = d.toLocaleDateString();
      if (ds !== day) { day = ds; addMsg('day', ds === new Date().toLocaleDateString() ? 'Today' : ds); }
      if (m.meta?.kind === 'proposal' && m.meta.proposal?.proposal_id === pendingId) return;  // shown live below
      if (m.meta?.kind === 'proposal') { const el = addMsg('bot proposal past', `<b>Proposal</b> · ${esc(m.content)}`); el.title = d.toLocaleString(); return; }
      const el = addMsg(m.role==='user' ? 'user' : 'bot' + (m.meta?.status==='FAILED' ? ' err' : ''), md(m.content));
      el.title = d.toLocaleString();
    });
    $('#suggest').style.display = msgs.length || name ? 'none' : '';
    if (pend.pending) approvalCard(pend.pending);
  } catch (e) { addMsg('bot err', 'Could not load chat history: ' + esc(e.message)); }
}
function md(t) { // minimal formatting: code fences and **bold**
  return esc(t).replace(/```(\w*)\n([\s\S]*?)```/g, (_, l, c) => `<pre class="code">${c}</pre>`).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
}
function uiContext() {
  return { selected_component: state.selComp, plotted: [...state.selected], pending_overrides: overrides(),
    sim_setup: { start_time:+$('#sStart').value, stop_time:+$('#sStop').value, number_of_intervals:parseInt($('#sN').value), method:$('#sMethod').value } };
}
async function stream(path, body, onEvent) {
  const res = await fetch(API + path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if (!res.ok || !res.body) throw new Error(res.statusText || 'Agent unavailable');
  const reader = res.body.getReader(), dec = new TextDecoder(); let buf = '';
  for (;;) {
    const {value, done} = await reader.read(); if (done) break;
    buf += dec.decode(value, {stream:true});
    let i; while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i+2);
      const line = chunk.split('\n').find(l => l.startsWith('data: ')); if (line) await onEvent(JSON.parse(line.slice(6)));
    }
  }
}
const TOOL_LABEL = {read_model:'Reading the model', read_results:'Reading results', validate:'Validating', simulate:'Simulating',
  plan_simulation:'Simulation agent: planning the run', propose_edit:'Designing the change', propose_new_model:'Designing a new model',
  set_parameters:'Filling parameters', plot:'Updating the plot', remember_note:'Noting that'};
async function runAgent(path, body) {
  $('#suggest').style.display='none'; setStatus('Modi is working','busy'); $('#btnSend').disabled=true;
  const run = addMsg('bot run', '<div class="steps"></div>'); const steps = run.querySelector('.steps');
  const addStep = (text, cls='run', detail) => { steps.querySelectorAll('.s.run').forEach(s=>s.className='s done');
    const last=steps.lastElementChild;
    if (last && last.dataset.tool && cls==='run') { last.textContent=text; delete last.dataset.tool; last.className='s run'; if (detail) last.title=detail; return; }  // tool's own step replaces its generic label
    const d=document.createElement('div'); d.className='s '+cls; d.textContent=text; if (detail) d.title=detail; steps.appendChild(d); $('#msgs').scrollTop=1e9; return d; };
  let reload = null, sawSteps = false, failed = null;
  try {
    await stream(path, body, async e => {
      if (e.type==='step') { sawSteps=true; addStep(e.text, 'run', e.detail); if (e.detail) log(e.detail,'e'); }
      else if (e.type==='tool') { sawSteps=true; const d=addStep(TOOL_LABEL[e.name] || e.name); if (d) d.dataset.tool=e.name; }
      else if (e.type==='tool_result') { steps.querySelectorAll('.s.run').forEach(s=>s.className = e.ok ? 's done' : 's fail'); if (!e.ok && e.error) addStep(e.error, 'fail'); }
      else if (e.type==='message') addMsg('bot', md(e.text));
      else if (e.type==='approval') { steps.querySelectorAll('.s.run').forEach(s=>s.className='s done'); approvalCard(e); }
      else if (e.type==='ui') { try { await uiAction(e); } catch (err) { log(err.message, 'e'); } }
      else if (e.type==='failed') failed = e;
      else if (e.type==='applied') { if (e.action==='applied' && (e.kind==='edit' || e.kind==='new_model')) reload = e.model; log(`Proposal ${e.action}${e.model?' · '+e.model:''}`, 'o'); }
      else if (e.type==='error') { addMsg('bot err', 'Something went wrong: '+esc(e.text)); log(e.text,'e'); }
      else if (e.type==='done') { if (e.model && e.model!==state.model) reload = e.model; }
    });
    steps.querySelectorAll('.s.run').forEach(s=>s.className='s done');
    if (!sawSteps) run.remove();
    setStatus('ready','ok');
    if (failed) { setStatus(failed.kind==='check' ? 'check failed' : 'sim failed', 'err'); offerFix(failed.kind, failed.message); }
  } catch (err) { run.remove(); addMsg('bot err', 'Something went wrong: '+esc(err.message)); setStatus('Modi failed','err'); log(err.message,'e'); }
  finally { $('#btnSend').disabled=false; }
  if (reload) { await loadLibrary(); await openModel(reload); }
}
async function uiAction(e) {
  if (e.action==='refresh_results' && e.model===state.model) {
    const s=e.settings||{}; if (s.start_time!=null) $('#sStart').value=s.start_time; if (s.stop_time!=null) $('#sStop').value=s.stop_time;
    if (s.number_of_intervals!=null) $('#sN').value=s.number_of_intervals; if (s.method) $('#sMethod').value=s.method;
    ['#sStart','#sStop','#sN'].forEach(id => { $(id).dataset.ok = $(id).value; }); validateSetup();
    state.vars = await api(`/api/studio/models/${state.model}/variables`);
    if (![...state.selected].some(v=>state.vars.includes(v))) defaultSelection();
    await loadUnits(); renderVars(); await refreshPlot(); $('#runInfo').textContent = `${state.model} · run by Modi`; loadLibrary(); live.stale=false; prepareLive();
  } else if (e.action==='plot' && state.vars.length) {
    const ok=(e.variables||[]).filter(v=>state.vars.includes(v)); if (ok.length) { state.selected=new Set(ok); renderVars(); await refreshPlot(); }
  } else if (e.action==='set_parameters') {
    for (const [k,v] of Object.entries(e.values||{})) if (k in state.params) state.params[k]=String(v);
    renderParams(); if (state.scene) drawDiagram();
  }
}
const KIND_TITLE = {edit:'Proposed change', new_model:'Proposed new model', sim_settings:'Proposed run settings'};
function approvalCard(p) {
  const el = addMsg('bot proposal', '');
  let body = `<div class="ptitle">${KIND_TITLE[p.kind]||'Proposal'}${p.model?` · <code>${esc(p.model)}</code>`:''}</div><div>${esc(p.summary||'')}</div>`;
  if (p.attempts>1) body += `<div class="pmeta">Compiled after ${p.attempts} attempts (self-corrected)</div>`;
  if (p.kind==='new_model' && p.exists) body += `<div class="pmeta warn">A model with this name exists and would be replaced.</div>`;
  if (p.kind==='sim_settings') body += `<div class="pmeta">${Object.entries(p.settings||{}).map(([k,v])=>`${esc(k)}=${esc(v)}`).join(' · ')}${Object.keys(p.overrides||{}).length?'<br>overrides: '+esc(JSON.stringify(p.overrides)):''}</div>`;
  if (p.diff) body += `<details><summary>Show diff</summary><pre class="diff">${esc(p.diff).split('\n').map(l=>`<span class="${l.startsWith('+')&&!l.startsWith('+++')?'add':l.startsWith('-')&&!l.startsWith('---')?'del':''}">${l}</span>`).join('\n')}</pre></details>`;
  body += `<div class="pbtns"><button class="btn primary" data-a="apply">Apply</button><button class="btn" data-a="discard">Discard</button><span class="pmeta">or reply below to refine</span></div>`;
  el.innerHTML = body;
  el.querySelectorAll('[data-a]').forEach(b => b.onclick = () => {
    el.querySelectorAll('button').forEach(x=>x.disabled=true); el.classList.add('past');
    addMsg('user', b.dataset.a==='apply' ? 'Apply' : 'Discard');
    runAgent('/api/studio/agent/resume', {model: state.model || state.draft, proposal_id: p.proposal_id, action: b.dataset.a});
  });
}
function offerFix(kind, message) {
  // After a failed Simulate or Check: one click hands the exact error to Modi, who proposes a fix.
  const msg = String(message || '').replace(/^OMSessionException:\s*/,'').slice(0, 700);
  document.querySelectorAll('.msg.fixcard:not(.past)').forEach(c => c.classList.add('past'));
  const el = addMsg('bot fixcard', `<div class="ptitle">${kind === 'check' ? 'Check failed' : 'Simulation failed'}</div>
    <pre class="code">${esc(msg)}</pre><div class="pbtns"><button class="btn primary">Fix with Modi</button><span class="pmeta">Modi explains the cause and proposes a fix for you to approve</span></div>`);
  el.querySelector('button').onclick = () => { el.classList.add('past'); el.querySelector('button').disabled = true;
    send(`Fix this ${kind} error in ${state.model}:\n${msg}`); };
}
async function send(text) {
  text = text.trim(); if (!text) return;
  $('#prompt').value=''; addMsg('user', esc(text));
  document.querySelectorAll('.msg.proposal:not(.past) button').forEach(x=>x.disabled=true);  // a reply refines the open proposal
  await runAgent('/api/studio/agent', {message: text, model: state.model || state.draft, ui: uiContext()});
}
$('#btnSend').onclick = () => send($('#prompt').value);
$('#prompt').addEventListener('keydown', e => { if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); send($('#prompt').value); } });
$('#suggest').addEventListener('click', e => { if (e.target.tagName==='BUTTON') send(e.target.textContent); });

/* ---------- boot ---------- */
loadLibrary().then(async () => { const first=document.querySelector('#lib button'); if (first) openModel(first.dataset.m); else loadThread(null); });
}
