// Settings dialog: provider (frontier / local GGUF), quantization picker, context window.
import { api } from './api.js';
import { $, h, icon, fmtBytes, fmtContext, toast } from './util.js';

let options = { providers: [], context_presets: [], languages: [] };
let settings = null;
let local = { curated: [], installed: [], installs: [], runtime: {}, active: '' };
let repoInfo = null;        // result of /api/local/repo
let chosenQuant = '';
let runtimeCheck = null;    // result of the last "Check" (runs llama-server --version)
let clearKey = false;       // the user pressed "Clear" for the current provider's key
let poll = null;
let onChange = () => {};

const CUSTOM = '__custom__';
const providerOf = (id) => options.providers.find((p) => p.id === id);

export async function initSettings(opts, changed) {
  options = opts;
  onChange = changed;
  settings = await api.get('/api/settings');

  // static wiring
  $('#open-settings').addEventListener('click', openSettings);
  $('#save-btn').addEventListener('click', () => save().then((ok) => ok && $('#settings').close()));
  $('#test-btn').addEventListener('click', testConnection);
  $('#quit-btn').addEventListener('click', quit);
  $('#settings').addEventListener('close', () => { clearInterval(poll); poll = null; });
  for (const btn of $('#provider-type').querySelectorAll('button')) {
    btn.addEventListener('click', () => setProviderType(btn.dataset.value));
  }
  $('#f-provider').addEventListener('change', () => { clearKey = false; fillFrontier($('#f-provider').value); });
  $('#f-key-clear').addEventListener('click', () => {
    clearKey = true; $('#f-key').value = '';
    $('#f-key-state').textContent = '- will be removed when you save';
  });
  $('#ctx').addEventListener('change', syncCtxCustom);
  $('#l-model').addEventListener('change', () => { resetQuants(); syncCustomRepo(); });
  $('#l-check').addEventListener('click', showQuants);
  $('#l-search-btn').addEventListener('click', searchRepos);
  $('#l-custom').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); searchRepos(); } });
  $('#l-install').addEventListener('click', installModel);
  $('#runtime-btn').addEventListener('click', async () => {
    const mode = $('#runtime-btn').dataset.mode || 'install';
    try {
      await api.post('/api/local/runtime/install', { update: mode === 'update', repair: mode === 'repair' });
      runtimeCheck = null;
      startPolling();
      await refreshLocal();
    } catch (e) { toast(e.message, 'err'); }
  });
  $('#runtime-check-btn').addEventListener('click', async () => {
    const btn = $('#runtime-check-btn');
    btn.disabled = true; btn.textContent = 'Checking...';
    try { runtimeCheck = await api.post('/api/local/runtime/check'); } catch (e) { toast(e.message, 'err'); }
    finally { btn.disabled = false; btn.textContent = 'Check'; renderLocal(); }
  });

  // context presets
  const ctx = $('#ctx');
  ctx.replaceChildren(...options.context_presets.map((n) => h('option', { value: n }, fmtContext(n))), h('option', { value: CUSTOM }, 'Custom...'));
  renderChip();
}

export const currentSettings = () => settings;

export async function reloadSettings() {
  settings = await api.get('/api/settings');
  renderChip();
  return settings;
}

// ------------------------------------------------------------------- chip ---
export async function renderChip() {
  const chip = $('#provider-chip');
  chip.classList.remove('warn');
  if (!settings) return;
  if (settings.provider_type === 'local') {
    try { local = await api.get('/api/local/state'); } catch { /* offline */ }
    const m = local.installed.find((x) => x.id === local.active);
    if (m) chip.textContent = `Local - ${m.label}`;
    else { chip.textContent = 'Local - install a model'; chip.classList.add('warn'); }
    return;
  }
  const pid = settings.frontier.provider;
  const prov = providerOf(pid);
  const model = settings.frontier.models[pid] || prov?.models[0] || '';
  const hasKey = settings.frontier.api_keys_set[pid] || !prov?.requires_key;
  chip.textContent = hasKey ? `${prov?.label.split(' - ')[0] || pid}${model ? ` - ${model}` : ''}` : `${prov?.label.split(' - ')[0] || pid} - add an API key`;
  if (!hasKey) chip.classList.add('warn');
}

// ------------------------------------------------------------------- open ---
export async function openSettings() {
  settings = await api.get('/api/settings');
  try { local = await api.get('/api/local/state'); } catch { /* ignore */ }
  const info = await api.get('/api/info').catch(() => null);
  $('#about-text').textContent = info ? `CTS ${info.version} - your data lives in ${info.data_dir}` : '';
  $('#test-result').textContent = '';
  $('#test-result').className = 'test-result';
  clearKey = false;

  $('#f-provider').replaceChildren(...options.providers.map((p) => h('option', { value: p.id }, p.label)));
  $('#f-provider').value = settings.frontier.provider;
  fillFrontier(settings.frontier.provider);
  setProviderType(settings.provider_type);

  const ctx = settings.context_window;
  if (options.context_presets.includes(ctx)) { $('#ctx').value = String(ctx); } else { $('#ctx').value = CUSTOM; $('#ctx-custom').value = ctx; }
  syncCtxCustom();

  $('#l-token').value = '';
  $('#l-token-state').textContent = settings.local.hf_token_set ? '(saved)' : '';
  renderLocal();
  $('#settings').showModal();
  startPolling();
}

function setProviderType(type) {
  for (const b of $('#provider-type').querySelectorAll('button')) b.setAttribute('aria-checked', String(b.dataset.value === type));
  $('#panel-frontier').hidden = type !== 'frontier';
  $('#panel-local').hidden = type !== 'local';
  settings.provider_type = type;
}

// --------------------------------------------------------------- frontier ---
function fillFrontier(pid) {
  const prov = providerOf(pid);
  const saved = settings.frontier.models[pid];
  $('#f-model').value = saved || prov.models[0] || '';
  $('#f-models').replaceChildren(...prov.models.map((m) => h('option', { value: m })));
  $('#f-key').value = '';
  $('#f-key').placeholder = prov.requires_key ? 'Paste your key' : 'Optional';
  $('#f-key-state').textContent = settings.frontier.api_keys_set[pid] ? '- saved on this computer' : '';
  $('#f-url-field').hidden = !prov.custom_base_url;
  $('#f-url').value = settings.frontier.base_urls[pid] || '';
  $('#f-note').textContent = prov.note || (prov.default_base_url ? `Endpoint: ${prov.default_base_url}` : '');
}

function syncCtxCustom() { $('#ctx-custom-field').hidden = $('#ctx').value !== CUSTOM; }

// ------------------------------------------------------------------ local ---
function renderLocal() {
  // engine
  const rt = local.runtime || {};
  const inst = rt.install || {};
  const engineBusy = inst.status === 'running';
  const label = rt.tag ? `llama.cpp ${rt.tag}${rt.flavor ? ` (${rt.flavor})` : ''}` : 'llama.cpp';
  const broken = runtimeCheck && !runtimeCheck.ok;
  $('#runtime-text').textContent = engineBusy ? inst.message
    : rt.installed ? `Ready - ${label}${rt.origin === 'system' ? ' (installed on your system)' : ''}`
    : 'Not installed yet - needed to run local models.';
  $('#runtime-text').style.color = '';
  const btn = $('#runtime-btn');
  btn.dataset.mode = !rt.installed ? 'install' : (broken || inst.status === 'error') ? 'repair' : rt.origin === 'cts' ? 'update' : 'repair';
  btn.textContent = { install: 'Install llama.cpp', update: 'Update', repair: 'Repair' }[btn.dataset.mode];
  btn.hidden = Boolean(rt.installed && rt.origin !== 'cts' && !broken && inst.status !== 'error');
  btn.disabled = engineBusy;
  $('#runtime-check-btn').hidden = !rt.installed;
  const rp = $('#runtime-progress');
  rp.hidden = !engineBusy;
  if (engineBusy) setBar(rp, inst.done, inst.total);
  const hint = $('#runtime-hint');
  let hintText = '', hintKind = '';
  if (inst.status === 'error') { hintText = inst.message; hintKind = 'err'; }
  else if (broken) { hintText = `${runtimeCheck.detail}${runtimeCheck.hint ? `\n${runtimeCheck.hint}` : ''}`; hintKind = 'err'; }
  else if (runtimeCheck && runtimeCheck.ok) { hintText = `Works: ${runtimeCheck.detail}`; hintKind = 'ok'; }
  hint.hidden = !hintText || engineBusy;
  hint.textContent = hintText;
  hint.className = `hint hint-box ${hintKind}`;

  // model select (keep the user's current choice while polling)
  const sel = $('#l-model');
  const prev = sel.value;
  sel.replaceChildren(...local.curated.map((m) => h('option', { value: m.repo }, m.label)), h('option', { value: CUSTOM }, 'Other Hugging Face repository...'));
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  syncCustomRepo();

  // installs in flight
  const running = (local.installs || []).find((i) => i.status === 'running');
  const last = (local.installs || []).slice(-1)[0];
  const bar = $('#l-progress'), text = $('#l-progress-text');
  const show = running || (last && last.status === 'error');
  bar.hidden = !running; text.hidden = !show;
  if (running) { setBar(bar, running.done, running.total); text.textContent = `${running.message} - ${fmtBytes(running.done)} / ${fmtBytes(running.total)}`; }
  else if (last?.status === 'error') { text.textContent = last.message; text.style.color = 'var(--err)'; }
  else text.style.color = '';
  $('#l-install').disabled = Boolean(running);

  // installed list
  const list = $('#l-installed');
  $('#l-installed-wrap').hidden = !local.installed.length;
  list.replaceChildren(...local.installed.map((m) => {
    const active = m.id === local.active;
    return h('li', { class: active ? 'active' : '' },
      h('div', { class: 'nm' }, h('b', {}, m.label), h('small', {}, `${fmtBytes(m.size)}${m.vision ? ' - reads images' : ' - NO vision (cannot read pages)'}${m.ok ? '' : ' - files missing'}`)),
      h('div', {},
        h('button', { class: 'ghost', disabled: active, onclick: () => useModel(m.id) }, active ? 'In use' : 'Use'),
        h('button', { class: 'icon-btn', 'aria-label': `Delete ${m.label}`, onclick: () => deleteModel(m) }, icon('trash'))));
  }));
}

function syncCustomRepo() {
  const custom = $('#l-model').value === CUSTOM;
  $('#l-custom-field').hidden = !custom;
  if (!custom) $('#l-results').hidden = true;
}

function resetQuants() { repoInfo = null; chosenQuant = ''; $('#l-quants-wrap').hidden = true; }

const selectedRepo = () => ($('#l-model').value === CUSTOM ? $('#l-custom').value.trim() : $('#l-model').value);

async function searchRepos() {
  const q = $('#l-custom').value.trim();
  if (!q) return;
  if (/^[\w.-]+\/[\w.-]+$/.test(q) || q.includes('huggingface.co/')) { showQuants(); return; }
  const ul = $('#l-results');
  try {
    const found = await api.get(`/api/local/search?q=${encodeURIComponent(q)}`);
    ul.hidden = false;
    ul.replaceChildren(...(found.length ? found : [{ repo: '', downloads: 0 }]).map((r) => r.repo
      ? h('li', { onclick: () => { $('#l-custom').value = r.repo; ul.hidden = true; showQuants(); } }, h('span', {}, r.repo), h('small', { class: 'muted' }, `${r.downloads.toLocaleString()} downloads`))
      : h('li', {}, 'No vision-capable GGUF models found.')));
  } catch (e) { toast(e.message, 'err'); }
}

async function showQuants() {
  const repo = selectedRepo();
  if (!repo) return toast('Enter a Hugging Face repository first.', 'err');
  const btn = $('#l-check');
  btn.disabled = true; btn.textContent = 'Loading...';
  try {
    repoInfo = await api.get(`/api/local/repo?repo=${encodeURIComponent(repo)}`);
    chosenQuant = repoInfo.recommended || repoInfo.quants[0].label;
    renderQuants();
  } catch (e) { toast(e.message, 'err'); resetQuants(); }
  finally { btn.disabled = false; btn.textContent = 'Show quantizations'; }
}

function renderQuants() {
  $('#l-quants-wrap').hidden = false;
  $('#l-quants').replaceChildren(...repoInfo.quants.map((q) => h('button', {
    type: 'button', class: 'quant', role: 'radio', 'aria-checked': String(q.label === chosenQuant),
    onclick: () => { chosenQuant = q.label; renderQuants(); },
  }, h('span', { class: 'radio' }),
  h('span', {}, h('span', { class: 'q' }, q.label, q.label === repoInfo.recommended ? h('small', {}, 'recommended') : null), h('br'), h('span', { class: 'd' }, q.hint)),
  h('span', { class: 'sz' }, fmtBytes(q.size)))));
  const q = repoInfo.quants.find((x) => x.label === chosenQuant);
  const mm = repoInfo.mmproj.find((m) => m.path === repoInfo.default_mmproj);
  $('#l-size').textContent = q ? `Download: ${fmtBytes(q.size + (mm?.size || 0))}${mm ? ' (includes the vision projector)' : ''}` : '';
  $('#l-vision').textContent = repoInfo.vision ? '' : 'This repository has no vision projector (mmproj): the model could not read comic pages.';
  $('#l-vision').style.color = repoInfo.vision ? '' : 'var(--warn)';
  $('#l-install').disabled = !repoInfo.vision;
}

async function installModel() {
  if (!repoInfo) return;
  try {
    await api.post('/api/local/install', { repo: repoInfo.repo, quant: chosenQuant });
    startPolling();
    await refreshLocal();
  } catch (e) { toast(e.message, 'err'); }
}

async function useModel(id) {
  try { await api.post('/api/local/select', { id }); await refreshLocal(); await reloadSettings(); } catch (e) { toast(e.message, 'err'); }
}

async function deleteModel(m) {
  if (!confirm(`Delete ${m.label} (${fmtBytes(m.size)}) from this computer?`)) return;
  try { await api.del(`/api/local/models/${encodeURIComponent(m.id)}`); await refreshLocal(); await reloadSettings(); } catch (e) { toast(e.message, 'err'); }
}

async function refreshLocal() {
  local = await api.get('/api/local/state');
  renderLocal();
  renderChip();
}

function startPolling() {
  clearInterval(poll);
  poll = setInterval(async () => {
    if (!$('#settings').open) return;
    const wasRunning = (local.installs || []).some((i) => i.status === 'running') || local.runtime?.install?.status === 'running';
    try { local = await api.get('/api/local/state'); } catch { return; }
    renderLocal();
    const nowRunning = (local.installs || []).some((i) => i.status === 'running') || local.runtime?.install?.status === 'running';
    if (wasRunning && !nowRunning) { runtimeCheck = null; await reloadSettings(); setProviderType(settings.provider_type); toast('Installation finished.', 'ok'); }
  }, 1000);
}

function setBar(bar, done, total) {
  bar.classList.toggle('indeterminate', !total);
  bar.firstElementChild.style.width = total ? `${Math.min(100, (done / total) * 100)}%` : '';
}

// ------------------------------------------------------------ save & test ---
async function save() {
  const pid = $('#f-provider').value;
  const patch = {
    provider_type: settings.provider_type,
    frontier: { provider: pid, models: { [pid]: $('#f-model').value.trim() }, base_urls: {}, api_keys: {} },
    local: {},
    context_window: $('#ctx').value === CUSTOM ? Number($('#ctx-custom').value) : Number($('#ctx').value),
  };
  if (providerOf(pid).custom_base_url) patch.frontier.base_urls[pid] = $('#f-url').value.trim();
  const key = $('#f-key').value.trim();
  if (key) patch.frontier.api_keys[pid] = key;
  else if (clearKey) patch.frontier.api_keys[pid] = '';
  const token = $('#l-token').value.trim();
  if (token) patch.local.hf_token = token;
  if (!(patch.context_window >= 2048)) { toast('The context window must be at least 2048 tokens.', 'err'); return false; }
  try {
    settings = await api.put('/api/settings', patch);
    clearKey = false;
    await renderChip();
    onChange(settings);
    return true;
  } catch (e) { toast(e.message, 'err'); return false; }
}

async function testConnection() {
  const out = $('#test-result');
  if (!(await save())) return;
  out.className = 'test-result';
  out.textContent = settings.provider_type === 'local' ? 'Starting the local model...' : 'Testing...';
  $('#test-btn').disabled = true;
  try {
    const res = await api.post('/api/settings/test');
    out.className = `test-result ${res.ok ? 'ok' : 'err'}`;
    out.textContent = res.message;
  } catch (e) { out.className = 'test-result err'; out.textContent = e.message; }
  finally { $('#test-btn').disabled = false; }
}

async function quit() {
  if (!confirm('Quit CTS? Any generation in progress will stop (you can resume it later).')) return;
  try { await api.post('/api/shutdown'); } catch { /* the server may drop the connection */ }
  document.body.replaceChildren(h('div', { style: { display: 'grid', placeItems: 'center', height: '100vh', color: '#8f8f8f', fontFamily: 'sans-serif' } }, 'CTS has stopped. You can close this tab.'));
}
