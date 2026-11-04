// The story box: live view of a generation job (progress, per-page steps, novel text).
import { api } from './api.js';
import { $, h, icon, fmtDuration, paragraphs, toast } from './util.js';

const STEPS = [
  ['analyze', 'Read', 'eye'],
  ['write', 'Write', 'pen'],
  ['polish', 'Polish', 'sparkle'],
  ['memory', 'Remember', 'memory'],
];
const COVER_STEP = ['cover', 'Cover?', 'book'];
const STATUS_LABEL = {
  queued: 'queued', running: 'generating', done: 'finished', failed: 'failed',
  cancelled: 'stopped', interrupted: 'interrupted', halted: 'stopped by age rule',
};
const TERMINAL = new Set(['done', 'failed', 'cancelled', 'interrupted', 'halted']);
// A page that is finished for progress purposes: written, or deliberately skipped (a book cover).
const finished = (p) => p.status === 'done' || p.status === 'skipped';

let cur = null; // { id, snap, selected, es, follow, cards: Map }
let tick = null;
let hooks = { onStatus: () => {} };

export function initStory(callbacks) {
  hooks = { ...hooks, ...callbacks };
  $('#stop-btn').addEventListener('click', async () => {
    if (!cur) return;
    $('#stop-btn').disabled = true;
    try { await api.post(`/api/jobs/${cur.id}/cancel`); toast('Stopping after the current step...'); }
    catch (e) { toast(e.message, 'err'); }
  });
  $('#resume-btn').addEventListener('click', async () => {
    if (!cur) return;
    try { await api.post(`/api/jobs/${cur.id}/resume`); await openJob(cur.id); }
    catch (e) { toast(e.message, 'err'); if (/key|model|settings/i.test(e.message)) hooks.openSettings?.(); }
  });
  $('#delete-job-btn').addEventListener('click', async () => {
    if (!cur || !confirm('Delete this run and its generated files?')) return;
    try { await api.del(`/api/jobs/${cur.id}`); closeStream(); cur = null; await refreshHistory(true); }
    catch (e) { toast(e.message, 'err'); }
  });
  $('#job-history').addEventListener('change', (e) => openJob(e.target.value));
  $('#bible-btn').addEventListener('click', () => {
    const v = selectedVolume();
    $('#bible-text').textContent = v?.bible || 'The story bible appears after the first page is written.';
    $('#bible').showModal();
  });
  $('#log-btn').addEventListener('click', () => { renderLog(); $('#log').showModal(); });
  $('#feed').addEventListener('scroll', () => {
    if (!cur) return;
    const f = $('#feed');
    cur.follow = f.scrollHeight - f.scrollTop - f.clientHeight < 160;
  });
  $('#lightbox').addEventListener('click', (e) => { if (e.target.id === 'lightbox') e.currentTarget.close(); });
}

// ------------------------------------------------------------------ history ---
export async function refreshHistory(openLatest = false) {
  let jobs = [];
  try { jobs = await api.get('/api/jobs'); } catch { return; }
  const select = $('#job-history');
  select.replaceChildren(...jobs.map((j) => h('option', { value: j.id },
    `${new Date(j.created * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })} - ${j.volumes.length} vol - ${STATUS_LABEL[j.status] || j.status}`)));
  select.hidden = jobs.length < 2;
  if (cur) select.value = cur.id;
  if (!cur && jobs.length && openLatest) await openJob(jobs[0].id);
  if (!jobs.length) showEmpty();
}

function showEmpty() {
  $('#empty').hidden = false;
  $('#job').hidden = true;
  $('#story-head').hidden = true;
}

// -------------------------------------------------------------------- stream ---
function closeStream() {
  if (cur?.es) cur.es.close();
  clearInterval(tick);
}

export async function openJob(id) {
  closeStream();
  cur = { id, snap: null, selected: 0, es: null, follow: true, cards: new Map() };
  const es = new EventSource(`/api/jobs/${id}/events`);
  cur.es = es;
  es.addEventListener('snapshot', (e) => {
    cur.snap = normalise(JSON.parse(e.data));
    if (cur.selected >= cur.snap.volumes.length) cur.selected = 0;
    // Follow the volume that is running.
    const running = cur.snap.volumes.findIndex((v) => v.status === 'running');
    if (running >= 0) cur.selected = running;
    renderAll();
  });
  es.onmessage = (e) => { if (cur && cur.es === es) applyEvent(JSON.parse(e.data)); };
  es.addEventListener('end', () => es.close());
  tick = setInterval(renderStats, 1000);
  $('#empty').hidden = true;
  $('#job').hidden = false;
  $('#story-head').hidden = false;
}

function normalise(snap) {
  for (const v of snap.volumes) v.pageMap = new Map(v.pages.map((p) => [p.index, p]));
  return snap;
}

function selectedVolume() { return cur?.snap?.volumes?.[cur.selected]; }

function page(vol, index) {
  const v = cur.snap.volumes[vol];
  let p = v.pageMap.get(index);
  if (!p) {
    p = { index, status: 'pending', steps: {}, text: '', analysis: '', notes: [], words: 0, rating_used: '', page_type: '' };
    v.pageMap.set(index, p);
    v.pages.push(p);
  }
  return p;
}

function applyEvent(e) {
  const snap = cur.snap;
  if (!snap) return;
  const v = snap.volumes[e.volume];
  switch (e.type) {
    case 'job_status':
      snap.status = e.status; snap.error = e.error || '';
      if (e.status === 'running' && !snap.started) snap.started = e.ts;
      if (TERMINAL.has(e.status)) {
        snap.finished = e.ts; clearInterval(tick);
        for (const vol of snap.volumes) {  // nothing is running any more
          for (const pg of vol.pageMap.values()) {
            if (pg.status === 'running') { pg.status = 'stopped'; pg.steps = Object.fromEntries(Object.entries(pg.steps || {}).filter(([, x]) => x === 'done')); }
          }
          if (vol.status === 'running') vol.status = 'partial';
        }
        renderFeed();
      }
      renderHeader(); renderAlert(); renderStats(); renderTabs(); renderNow();
      hooks.onStatus(e.status, snap);
      refreshHistory();
      if (e.status === 'done') toast('Your novel is ready - use the download buttons.', 'ok');
      if (e.status === 'failed') toast(e.error || 'The job failed.', 'err');
      return;
    case 'activity': snap.activity = e.message; renderNow(); return;
    case 'log': snap.log.push(e); snap.log.splice(0, Math.max(0, snap.log.length - 300)); return;
    case 'volume_start': v.status = 'running'; v.total = e.total; if (e.volume !== cur.selected && !cur.userPicked) selectVolume(e.volume); renderTabs(); return;
    case 'page_start': { const p = page(e.volume, e.page); p.status = 'running'; p.steps = {}; break; }
    case 'step': { const p = page(e.volume, e.page); p.status = 'running'; p.steps[e.step] = e.status; snap.activity = `Volume ${e.volume + 1} - page ${e.page}: ${labelFor(e.step)}`; break; }
    case 'page_done': {
      const p = page(e.volume, e.page);
      Object.assign(p, e.record);
      if (e.record.status === 'skipped') { p.steps = { cover: 'done' }; p.status = 'skipped'; }
      else { p.steps = Object.fromEntries(STEPS.map(([k]) => [k, 'done'])); p.status = e.record.status === 'done' ? 'done' : 'failed'; }
      v.done = [...v.pageMap.values()].filter(finished).length;
      if (e.tokens != null) snap.tokens = Math.max(snap.tokens, e.tokens);
      snap.pages_done = snap.volumes.reduce((n, vol) => n + [...vol.pageMap.values()].filter(finished).length, 0);
      break;
    }
    case 'page_failed': { const p = page(e.volume, e.page); p.status = 'failed'; p.error = e.error; break; }
    case 'bible': v.bible = e.text; return;
    case 'volume_files': v.files = e.files; v.partial = e.partial; renderDownloads(); return;
    case 'volume_done': v.status = 'done'; renderTabs(); return;
    default: return;
  }
  if (e.volume === cur.selected) updateCard(e.volume, e.page);
  renderStats(); renderTabs(); renderNow();
}

const labelFor = (step) => ({ cover: 'checking whether it is a book cover', analyze: 'reading the comic page', write: 'writing the draft', polish: 'polishing', memory: 'updating the story bible' }[step]);

// ------------------------------------------------------------------- render ---
function renderAll() {
  renderHeader(); renderAlert(); renderStats(); renderTabs(); renderNow(); renderDownloads(); renderFeed();
  hooks.onStatus(cur.snap.status, cur.snap);
}

function renderHeader() {
  const s = cur.snap;
  const names = s.volumes.map((v) => v.name);
  $('#story-title').textContent = names.length === 1 ? names[0] : `${names.length} volumes`;
  const pill = $('#job-pill');
  pill.textContent = STATUS_LABEL[s.status] || s.status;
  pill.className = `pill ${s.status}`;
  const active = s.status === 'running' || s.status === 'queued';
  $('#stop-btn').hidden = !active;
  $('#stop-btn').disabled = false;
  $('#resume-btn').hidden = !['cancelled', 'failed', 'interrupted'].includes(s.status);
  $('#delete-job-btn').hidden = active;
}

function renderAlert() {
  const el = $('#job-alert');
  const s = cur.snap;
  el.hidden = !s.error;
  el.textContent = s.error || '';
  el.classList.toggle('warn', s.status === 'interrupted' || s.status === 'cancelled');
}

function renderStats() {
  if (!cur?.snap) return;
  const s = cur.snap;
  const total = s.volumes.reduce((n, v) => n + v.total, 0);
  const done = s.volumes.reduce((n, v) => n + [...v.pageMap.values()].filter(finished).length, 0);
  const pct = total ? Math.round((done / total) * 100) : 0;
  const bar = $('#overall-bar');
  bar.firstElementChild.style.width = `${pct}%`;
  bar.setAttribute('aria-valuenow', pct);
  const end = s.finished || Date.now() / 1000;
  const elapsed = s.started ? end - s.started : 0;
  const timed = [...s.volumes.flatMap((v) => [...v.pageMap.values()])].filter((p) => p.seconds > 0 && p.status === 'done').slice(-12);
  const avg = timed.length ? timed.reduce((a, p) => a + p.seconds, 0) / timed.length : 0;
  const remaining = total - done;
  const bits = [
    [`${done} / ${total}`, 'pages'],
    [`${pct}%`, 'done'],
  ];
  if (s.started) bits.push([fmtDuration(elapsed), 'elapsed']);
  if (s.status === 'running' && avg && remaining) bits.push([`~${fmtDuration(avg * remaining)}`, 'left']);
  if (s.tokens) bits.push([s.tokens.toLocaleString(), 'tokens']);
  const stats = $('#stats');
  stats.replaceChildren(...bits.map(([a, b]) => h('span', {}, h('b', {}, a), ' ', b)),
    h('span', {}, s.rating === 'nsfw' ? 'NSFW edition' : 'SFW edition'),
    h('span', {}, s.provider));
}

function renderTabs() {
  const box = $('#vol-tabs');
  const s = cur.snap;
  box.hidden = s.volumes.length < 2;
  box.replaceChildren(...s.volumes.map((v, i) => {
    const done = [...v.pageMap.values()].filter(finished).length;
    const pct = v.total ? (done / v.total) * 100 : 0;
    const state = v.status === 'running' ? 'in progress' : v.status === 'done' ? 'finished' : v.status === 'partial' ? 'partial' : 'waiting';
    const bar = h('div', { class: 'progress slim' }, h('span', { style: { width: `${pct}%` } }));
    return h('button', {
      class: 'vol-tab', role: 'tab', 'aria-selected': String(i === cur.selected),
      onclick: () => { cur.userPicked = true; selectVolume(i); },
    }, h('span', { class: 't' }, `${i + 1}. ${v.name}`), bar, h('span', { class: 's' }, h('span', {}, `${done}/${v.total}`), h('span', {}, state)));
  }));
}

function selectVolume(i) {
  cur.selected = i;
  renderTabs(); renderDownloads(); renderFeed(); renderNow();
}

function renderNow() {
  const s = cur.snap;
  const el = $('#now');
  if (s.status === 'running' && s.activity) el.replaceChildren(h('span', { class: 'dot' }), s.activity);
  else if (s.status === 'queued') el.replaceChildren(h('span', { class: 'dot' }), 'Waiting for the previous job to finish...');
  else if (s.status === 'done') el.textContent = 'All pages written.';
  else if (s.status === 'halted') el.textContent = 'Stopped by the age rule - see the message above.';
  else el.textContent = '';
}

function renderDownloads() {
  const v = selectedVolume();
  const box = $('#downloads');
  if (!v) return box.replaceChildren();
  const label = v.partial ? ' (partial)' : '';
  box.replaceChildren(...['txt', 'docx', 'pdf'].map((fmt) => {
    const ready = Boolean(v.files?.[fmt]);
    const a = h('a', { class: `dl ${ready ? '' : 'off'}`, 'aria-disabled': String(!ready) }, icon('download'), fmt.toUpperCase() + (ready ? label : ''));
    if (ready) { a.href = `/api/jobs/${cur.id}/volumes/${cur.selected}/download?format=${fmt}`; a.setAttribute('download', ''); }
    return a;
  }));
}

function renderLog() {
  const list = $('#log-list');
  const entries = cur?.snap?.log || [];
  list.replaceChildren(...(entries.length ? entries : [{ ts: 0, level: '', message: 'Nothing to report yet.' }]).slice().reverse().map((e) =>
    h('li', {}, h('time', {}, e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : ''), h('span', { class: e.level }, e.message))));
}

// ---- feed ----------------------------------------------------------------------
function renderFeed() {
  const feed = $('#feed');
  feed.replaceChildren();
  cur.cards.clear();
  const v = selectedVolume();
  if (!v) return;
  const pages = [...v.pageMap.values()].sort((a, b) => a.index - b.index);
  for (const p of pages) feed.append(buildCard(cur.selected, p));
  if (cur.follow) feed.scrollTop = feed.scrollHeight;
}

function updateCard(vol, index) {
  const p = cur.snap.volumes[vol].pageMap.get(index);
  const old = cur.cards.get(index);
  const fresh = buildCard(vol, p);
  fresh.style.animation = 'none';
  if (old) {
    // keep the "analysis" disclosure state while the card refreshes
    const wasOpen = old.querySelector('details')?.open;
    old.replaceWith(fresh);
    if (wasOpen) fresh.querySelector('details')?.setAttribute('open', '');
  } else {
    $('#feed').append(fresh);
  }
  if (cur.follow) $('#feed').scrollTo({ top: $('#feed').scrollHeight, behavior: 'smooth' });
}

function stepChip([key, label, ico], p) {
  const status = p.steps?.[key] || '';
  const name = status === 'running' ? 'refresh' : status === 'done' ? 'check' : ico;
  return h('span', { class: `step ${status}` }, icon(name), label);
}

function buildCard(vol, p) {
  const job = cur;
  const shot = h('div', { class: 'shot', 'data-n': p.index, role: 'button', tabindex: '0', 'aria-label': `Open comic page ${p.index}` });
  shot.style.backgroundImage = `url("/api/jobs/${job.id}/volumes/${vol}/pages/${p.index}/thumb")`;
  const open = () => { $('#lightbox-img').src = `/api/jobs/${job.id}/volumes/${vol}/pages/${p.index}/preview`; $('#lightbox').showModal(); };
  shot.addEventListener('click', open);
  shot.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });

  const body = [];
  if (p.status === 'skipped') {
    const c = p.cover || {};
    const what = c.kind && c.kind !== 'none' ? `${c.kind} cover` : 'cover';
    body.push(h('div', { class: 'skipped' }, icon('book'),
      h('div', {}, h('b', {}, `Book ${what}${c.title ? `: ${c.title}` : ''}`),
        h('div', { class: 'muted' }, 'Skipped - a cover is not part of the story, so no novel page is written for it.'))));
  } else if (p.status === 'done' || (p.status === 'failed' && p.text)) {
    body.push(h('div', { class: 'prose' }, paragraphs(p.text).map((t) => h('p', {}, t))));
  } else if (p.status === 'stopped') {
    body.push(h('div', { class: 'note' }, icon('alert'), 'The process stopped before this page was written.'));
  } else if (p.status === 'running') {
    body.push(h('div', { class: 'skeleton', 'aria-label': 'Writing...' }, h('i'), h('i'), h('i'), h('i')));
  } else if (p.status === 'failed') {
    body.push(h('div', { class: 'note' }, icon('alert'), p.error || 'This page failed.'));
  }
  const notes = (p.status === 'skipped' ? [] : (p.notes || [])).map((n) => h('div', { class: 'note' }, icon('alert'), n));
  if (notes.length) body.push(h('div', { class: 'notes' }, notes));
  if (p.analysis && p.status !== 'skipped') {
    body.push(h('details', { class: 'analysis' }, h('summary', {}, icon('eye'), 'What CTS saw on this page'), h('pre', {}, p.analysis)));
  }

  const meta = [];
  if (p.page_type && p.page_type !== 'story' && p.status !== 'skipped') meta.push(p.page_type);
  if (p.words) meta.push(`${p.words} words`);
  const card = h('article', { class: `page ${p.status}`, id: `p-${vol}-${p.index}` },
    shot,
    h('div', { class: 'page-main' },
      h('div', { class: 'page-head' },
        h('div', { class: 'page-title' }, `Page ${p.index}`, meta.length ? h('small', {}, meta.join(' - ')) : null,
          p.rating_used ? h('span', { class: `tag ${p.rating_used}` }, p.rating_used) : null),
        h('div', { class: 'steps' }, (p.status === 'skipped' ? [COVER_STEP] : p.steps?.cover ? [COVER_STEP, ...STEPS] : STEPS).map((s) => stepChip(s, p)))),
      body));
  cur.cards.set(p.index, card);
  return card;
}
