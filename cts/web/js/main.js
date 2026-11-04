// CTS front end entry point: zip box (volumes), generation options, and wiring of the other modules.
import { api } from './api.js';
import { initSettings, openSettings, currentSettings } from './settings.js';
import { initStory, openJob, refreshHistory } from './story.js';
import { groupsFromDrop, groupsFromFiles, uploadGroup } from './upload.js';
import { $, h, icon, naturalCompare, toast } from './util.js';

const state = {
  volumes: [],       // [{ id, name, pages, status: 'ready'|'uploading'|'error', progress, text }]
  rating: 'sfw',
  jobActive: false,
};

// ---------------------------------------------------------------- volumes ---
function renderVolumes() {
  const ul = $('#volumes');
  ul.replaceChildren(...state.volumes.map((v, i) => {
    const sub = v.status === 'uploading' ? v.text || 'Uploading...' : v.status === 'error' ? v.text : `${v.pages} pages`;
    const thumb = h('div', { class: 'thumb' });
    if (v.status === 'ready') thumb.style.backgroundImage = `url("/api/volumes/${v.id}/pages/0/thumb")`;
    const tools = v.status === 'uploading' ? [] : [
      h('button', { class: 'icon-btn', 'aria-label': `Move ${v.name} up`, disabled: i === 0, onclick: () => move(i, -1) }, icon('up')),
      h('button', { class: 'icon-btn', 'aria-label': `Move ${v.name} down`, disabled: i === state.volumes.length - 1, onclick: () => move(i, 1) }, icon('down')),
      h('button', { class: 'icon-btn', 'aria-label': `Remove ${v.name}`, onclick: () => remove(v) }, icon('x')),
    ];
    return h('li', { class: `volume ${v.status}` },
      thumb,
      h('div', { class: 'meta' },
        h('div', { class: 'name', title: v.name }, h('span', { class: 'order' }, `${i + 1}.`), v.name),
        h('div', { class: 'sub' }, sub),
        v.status === 'uploading' ? h('div', { class: 'progress slim bar' }, h('span', { style: { width: `${Math.round((v.progress || 0) * 100)}%` } })) : null),
      h('div', { class: 'tools' }, tools));
  }));
  updateStart();
}

function move(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= state.volumes.length) return;
  [state.volumes[i], state.volumes[j]] = [state.volumes[j], state.volumes[i]];
  renderVolumes();
}

async function remove(v) {
  if (v.status === 'ready') {
    try { await api.del(`/api/volumes/${v.id}`); } catch (e) { return toast(e.message, 'err'); }
  }
  state.volumes = state.volumes.filter((x) => x !== v);
  renderVolumes();
}

function updateStart() {
  const ready = state.volumes.filter((v) => v.status === 'ready').length;
  const busy = state.volumes.some((v) => v.status === 'uploading');
  const btn = $('#start');
  btn.disabled = !ready || busy || state.jobActive;
  $('span', btn).textContent = state.jobActive ? 'Generating...' : ready > 1 ? `Generate ${ready} novels` : 'Generate novel';
}

async function handleGroups(groups) {
  // Volumes dropped together are queued in natural order (Vol 1, Vol 2, ... Vol 10).
  groups = [...groups].sort((a, b) => naturalCompare(a.label, b.label));
  const fresh = [];
  await Promise.all(groups.map(async (group) => {
    const row = { id: `tmp-${Math.random()}`, name: group.label.replace(/\.(zip|cbz)$/i, ''), pages: 0, status: 'uploading', progress: 0, text: 'Preparing...' };
    fresh.push(row);
    state.volumes.push(row);
    renderVolumes();
    try {
      const volumes = await uploadGroup(group, (fraction, text) => {
        row.progress = fraction; row.text = text;
        updateRowProgress(row);
      });
      const index = state.volumes.indexOf(row);
      const ready = volumes.map((v) => ({ ...v, status: 'ready' })).sort((a, b) => naturalCompare(a.name, b.name));
      state.volumes.splice(index, 1, ...ready);
      toast(`Added ${ready.map((v) => `${v.name} (${v.pages} pages)`).join(', ')}`, 'ok');
    } catch (e) {
      row.status = 'error'; row.text = e.message;
      toast(e.message, 'err');
    }
    renderVolumes();
  }));
}

function updateRowProgress(row) {
  // cheap in-place update so thousands of progress events don't rebuild the list
  const i = state.volumes.indexOf(row);
  const li = $('#volumes').children[i];
  if (!li) return;
  const bar = li.querySelector('.bar span');
  if (bar) bar.style.width = `${Math.round(row.progress * 100)}%`;
  const sub = li.querySelector('.sub');
  if (sub) sub.textContent = row.text;
}

function wireDropzone() {
  const drop = $('#drop');
  const zipInput = $('#pick-zip');
  const folderInput = $('#pick-folder');
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  for (const ev of ['dragenter', 'dragover']) drop.addEventListener(ev, (e) => { stop(e); drop.classList.add('over'); });
  drop.addEventListener('dragleave', (e) => { stop(e); drop.classList.remove('over'); });
  drop.addEventListener('drop', async (e) => {
    stop(e); drop.classList.remove('over');
    try { await handleGroups(await groupsFromDrop(e.dataTransfer)); } catch (err) { toast(err.message, 'err'); }
  });
  // Dropping anywhere else on the page must not navigate away from CTS.
  for (const ev of ['dragover', 'drop']) window.addEventListener(ev, (e) => { if (!drop.contains(e.target)) e.preventDefault(); });
  drop.addEventListener('click', (e) => { if (!e.target.closest('button')) zipInput.click(); });
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); zipInput.click(); } });
  $('#pick-zip-btn').addEventListener('click', () => zipInput.click());
  $('#pick-folder-btn').addEventListener('click', () => folderInput.click());
  for (const input of [zipInput, folderInput]) {
    input.addEventListener('change', async () => {
      const groups = groupsFromFiles([...input.files]);
      input.value = '';
      await handleGroups(groups);
    });
  }
}

// ---------------------------------------------------------------- options ---
function wireOptions(options) {
  $('#language').replaceChildren(...options.languages.map((l) => h('option', { value: l.value }, l.label)));
  const radios = [...$('#rating').querySelectorAll('button')];
  const setRating = (value) => {
    state.rating = value;
    radios.forEach((b) => b.setAttribute('aria-checked', String(b.dataset.value === value)));
  };
  for (const b of radios) {
    b.addEventListener('click', async () => {
      if (b.dataset.value === 'nsfw' && !adultConfirmed()) {
        if (!(await askAdult())) return;
      }
      setRating(b.dataset.value);
    });
  }
  $('#start').addEventListener('click', startJob);
  return setRating;
}

const adultConfirmed = () => { try { return localStorage.getItem('cts.adult') === '1'; } catch { return false; } };

function askAdult() {
  return new Promise((resolve) => {
    const dlg = $('#adult');
    const done = (ok) => {
      if (ok) { try { localStorage.setItem('cts.adult', '1'); } catch { /* private mode */ } }
      dlg.close(); resolve(ok);
    };
    $('#adult-yes').onclick = () => done(true);
    $('#adult-no').onclick = () => done(false);
    dlg.addEventListener('cancel', () => resolve(false), { once: true });
    dlg.showModal();
  });
}

async function startJob() {
  const ready = state.volumes.filter((v) => v.status === 'ready');
  if (!ready.length) return;
  const words = Math.max(80, Math.min(1200, Number($('#words').value) || 300));
  $('#words').value = words;
  try {
    const job = await api.post('/api/jobs', {
      volumes: ready.map((v) => v.id),
      rating: state.rating,
      words_per_page: words,
      language: $('#language').value,
      carry_bible: $('#carry').checked,
      skip_covers: $('#skip-covers').checked,
    });
    await openJob(job.id);
    refreshHistory();
  } catch (e) {
    toast(e.message, 'err', 8000);
    if (/api key|model|settings|install|base url/i.test(e.message)) openSettings();
  }
}

// ------------------------------------------------------------------- init ---
async function main() {
  let options;
  try {
    options = await api.get('/api/options');
  } catch (e) {
    document.body.prepend(h('div', { class: 'alert' }, e.message));
    return;
  }
  const setRating = wireOptions(options);
  wireDropzone();
  initStory({
    openSettings,
    onStatus: (status) => { state.jobActive = status === 'running' || status === 'queued'; updateStart(); },
  });
  await initSettings(options, () => {});

  const defaults = currentSettings().defaults;
  $('#words').value = defaults.words_per_page;
  $('#language').value = defaults.language;
  $('#carry').checked = defaults.carry_story_bible;
  $('#skip-covers').checked = defaults.skip_covers !== false;
  if (defaults.rating === 'nsfw' && adultConfirmed()) setRating('nsfw');

  try {
    state.volumes = (await api.get('/api/volumes')).map((v) => ({ ...v, status: 'ready' }));
  } catch { /* ignore */ }
  renderVolumes();
  await refreshHistory(true);

  // First run: nudge the user toward configuring a provider.
  const s = currentSettings();
  const needsSetup = s.provider_type === 'frontier' && !Object.values(s.frontier.api_keys_set).some(Boolean)
    && !['custom'].includes(s.frontier.provider);
  if (needsSetup && !localStorage.getItem('cts.welcomed')) {
    try { localStorage.setItem('cts.welcomed', '1'); } catch { /* ignore */ }
    openSettings();
  }
}

main();
