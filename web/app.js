/* quietfeed front-end. Talks only to localhost; every YouTube image is proxied by the server. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const KIND_LABEL = { yt: 'videos', tw: 'tweets', rss: 'posts', arxiv: 'papers' };
const KIND_ONE = { yt: 'channel', tw: 'account', rss: 'feed', arxiv: 'query' };

let view = 'feed', kindFilter = 'all', tagFilter = null, paperWindow = 1, axResults = null;
let libKind = 'all', libTag = null;
let data = null, subs = null;
const seen = new Set(), saved = new Set();
let savedItems = {};

function toast(m, ms = 2500) { const e = $('#msg'); e.textContent = m; e.style.opacity = 1; clearTimeout(toast.t); toast.t = setTimeout(() => e.style.opacity = 0, ms); }

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const ct = r.headers.get('content-type') || '';
  const out = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((out && out.error) || out || r.statusText);
  return out;
}

function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 3600) return Math.max(1, Math.round(d / 60)) + ' min';
  if (d < 86400) return Math.round(d / 3600) + ' h';
  return Math.round(d / 86400) + ' d';
}
function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const start = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(now) - start(d)) / 86400000);
  if (diff <= 0) return 'today';
  if (diff === 1) return 'yesterday';
  if (diff < 7) return d.toLocaleDateString(undefined, { weekday: 'long' });
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'long' }) + (d.getFullYear() !== now.getFullYear() ? ' ' + d.getFullYear() : '');
}
function dur(s) { if (s == null) return ''; const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h} h ${m} min` : `${m} min`; }
function dateStr(ts) { return new Date(ts * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }); }

function calm(title) {
  if (!data?.settings?.calm_titles) return title;
  let t = title.replace(/!{2,}/g, '!').replace(/\?{2,}/g, '?').replace(/\s*(?:[!?]\s*){2,}$/, m => m.trim()[0]);
  const letters = t.replace(/[^A-Za-zÀ-ÿ]/g, '');
  const upper = letters.replace(/[^A-ZÀ-Ý]/g, '');
  if (letters.length >= 8 && upper.length / letters.length > 0.6) {
    t = t.toLowerCase().replace(/(^|[.!?]\s+|["“(]\s*)([a-zà-ÿ])/g, (m, p, c) => p + c.toUpperCase());
  } else {
    t = t.replace(/\b[A-ZÀ-Ý]{4,}\b/g, w => w[0] + w.slice(1).toLowerCase());
  }
  return t;
}

/* ------------------------------------------------------------ theme */

function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  $('#theme').textContent = t === 'light' ? '☼' : t === 'dark' ? '☾' : '◐';
  $('#theme').title = 'theme: ' + (t || 'auto') + ' (click to change)';
}
function cycleTheme() {
  let t; try { t = localStorage.getItem('theme'); } catch (e) { t = null; }
  const next = !t ? 'light' : t === 'light' ? 'dark' : null;
  try { next ? localStorage.setItem('theme', next) : localStorage.removeItem('theme'); } catch (e) { /* ignore */ }
  applyTheme(next);
}

/* ------------------------------------------------------------ feed */

async function load() {
  try { data = await api('/api/items'); } catch (e) { toast('server not answering: ' + e.message); return; }
  seen.clear(); data.seen.forEach(i => seen.add(i));
  saved.clear(); data.saved.forEach(i => saved.add(i));
  savedItems = {}; (data.saved_items || []).forEach(i => { savedItems[i.id] = i; });
  $('#authors').innerHTML = (data.arxiv_authors || []).map(a => `<option value="${esc(a)}">`).join('');
  migrateLocalSeen();
  render();
}

function migrateLocalSeen() {
  try {
    const old = JSON.parse(localStorage.getItem('seen') || '[]');
    if (old.length && !localStorage.getItem('seen_migrated')) {
      api('/api/mark', { ids: old, field: 'seen', on: true }).then(() => { localStorage.setItem('seen_migrated', '1'); old.forEach(i => seen.add(i)); render(); });
    }
  } catch (e) { /* ignore */ }
}

function render() {
  document.querySelectorAll('.views button').forEach(b => b.classList.toggle('on', b.dataset.v === view));
  ['feed', 'library', 'following'].forEach(v => $('#view-' + v).hidden = v !== view);
  if (!data) return;
  if (view === 'feed') renderFeed();
  else if (view === 'library') renderLibrary();
  else renderSubs();
}

function kindChips(active, counts, extraTags, activeTag, prefix) {
  const chips = [`<button data-${prefix}f="all" class="${active === 'all' ? 'on' : ''}">all</button>`];
  for (const k of ['yt', 'tw', 'rss', 'arxiv']) if (counts[k]) chips.push(`<button data-${prefix}f="${k}" class="${active === k ? 'on' : ''}">${KIND_LABEL[k]}</button>`);
  chips.push(...extraTags.map(t => `<button data-${prefix}tag="${esc(t)}" class="tag ${activeTag === t ? 'on' : ''}">${esc(t)}</button>`));
  return chips.join('');
}

function renderFeed() {
  const s = data.settings;
  $('#when').textContent = data.refreshing ? 'refreshing…' : data.last_refresh ? 'updated ' + ago(data.last_refresh) + ' ago' : 'loading…';
  $('#chips').innerHTML = kindChips(kindFilter, data.counts, data.tags, tagFilter, '');
  $('#errs').innerHTML = data.errors.length
    ? `<details class="err"><summary>${data.errors.length} source${data.errors.length > 1 ? 's' : ''} could not be fetched</summary>${esc(data.errors.join('\n'))}</details>` : '';
  const papers = kindFilter === 'arxiv';
  $('#papers').hidden = !papers;
  document.querySelectorAll('#windows button').forEach(b => b.classList.toggle('on', +b.dataset.w === paperWindow));
  $('#ax_clear').hidden = !axResults;
  const L = $('#list');
  if (papers && axResults) {
    L.innerHTML = axResults.length ? `<div class="day">${axResults.length} results from arXiv</div>` + axResults.map(article).join('') : '<div class="empty">arXiv found nothing for that</div>';
    return;
  }
  let items = data.items.filter(i => (kindFilter === 'all' || i.kind === kindFilter) && (!tagFilter || i.tags.includes(tagFilter)) && !(s.hide_seen && seen.has(i.id)));
  if (papers && paperWindow) {
    // arXiv dates a paper by submission and lists it a day or two later, so "latest" means the newest batch
    // (within a day of the newest paper you have), and the longer windows count back from now.
    const newest = Math.max(0, ...items.map(i => i.ts));
    const cut = paperWindow === 1 ? newest - 86400 : Date.now() / 1000 - paperWindow * 86400;
    items = items.filter(i => i.ts >= cut);
  }
  if (!items.length) {
    const n = Object.values(data.counts).reduce((a, b) => a + b, 0);
    L.innerHTML = `<div class="empty">${n ? (data.last_refresh ? (papers && paperWindow ? 'no papers in this window' : papers ? 'no papers yet — follow a category or author in <b>following</b>' : 'nothing here') : 'fetching your sources…') : 'you are not following anything yet — open <b>following</b> to import or search'}</div>`;
    return;
  }
  let html = '', day = null, dayPapers = [];
  const flushPapers = () => { if (dayPapers.length) { html += digest(dayPapers); dayPapers = []; } };
  for (const i of items) {
    const d = dayLabel(i.ts);
    if (d !== day) { flushPapers(); html += `<div class="day">${esc(d)}</div>`; day = d; }
    if (kindFilter === 'all' && i.kind === 'arxiv') { dayPapers.push(i); continue; }  // papers fold into one calm line per day in "all"
    html += article(i);
  }
  flushPapers();
  L.innerHTML = html;
}

function digest(papers) {
  const top = papers.slice(0, 3).map(p => `<span>${esc(p.title)}</span>`).join('');
  const via = [...new Set(papers.flatMap(p => p.via || []))].slice(0, 4).join(', ');
  return `<article class="digest" data-id="digest"><div><p class="t">${papers.length} new paper${papers.length > 1 ? 's' : ''} <span class="cats">${esc(via)}</span></p>
    <div class="desc">${top}</div><div class="acts"><button data-a="papers">open papers</button></div></div></article>`;
}

function article(i) {
  const sn = seen.has(i.id), sv = saved.has(i.id);
  const tags = (i.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('');
  const saveBtn = `<button data-a="save">${sv ? 'unsave' : 'save'}</button><button data-a="share">share</button>`;
  const seenBtn = `<button data-a="seen" class="faint">${sn ? 'unmark' : 'mark seen'}</button>`;
  const cls = `${i.kind}${sn ? ' seen' : ''}`;
  if (i.kind === 'yt') {
    const nothumb = data.settings.hide_thumbnails;
    return `<article class="${cls}${nothumb ? ' nothumb' : ''}" data-id="${esc(i.id)}">${nothumb ? '' : `<img loading="lazy" src="/thumb/${esc(i.id)}" alt="">`}
      <div><p class="t">${esc(calm(i.title))}</p>
      <div class="by">${esc(i.author)} · ${i.dur != null ? dur(i.dur) + ' · ' : ''}${ago(i.ts)} ago${tags}</div>
      <div class="acts"><button data-a="play">▶ play</button><button data-a="dl">↓ download</button>${saveBtn}${seenBtn}</div></div></article>`;
  }
  if (i.kind === 'tw') {
    return `<article class="${cls}" data-id="${esc(i.id)}"><div>
      <div class="by">${esc(i.author)} · ${ago(i.ts)} ago${tags}</div>
      <div class="tweet">${esc(i.title)}</div>
      <div class="acts">${saveBtn}${seenBtn}</div></div></article>`;
  }
  if (i.kind === 'rss') {
    return `<article class="${cls}" data-id="${esc(i.id)}"><div>
      <p class="t"><a href="${esc(i.link)}" target="_blank" rel="noopener">${esc(i.title)}</a></p>
      <div class="by">${esc(i.author)}${i.by && i.by !== i.author ? ' · ' + esc(i.by) : ''} · ${ago(i.ts)} ago${tags}</div>
      ${i.desc ? `<div class="desc">${esc(i.desc)}</div>` : ''}
      <div class="acts"><a href="${esc(i.link)}" target="_blank" rel="noopener">open ↗</a>${saveBtn}${seenBtn}</div></div></article>`;
  }
  if (i.kind === 'arxiv') {
    const mine = new Set((data.arxiv_authors || []).map(a => a.toLowerCase()));
    const authors = (i.authors || []).map(a => `<button data-author="${esc(a)}" class="${mine.has(a.toLowerCase()) ? 'me' : ''}">${esc(a)}</button>`).join(', ');
    const cats = `<span class="cats"><span class="p">${esc(i.primary)}</span>${(i.cats || []).filter(c => c !== i.primary).slice(0, 3).map(c => ' ' + esc(c)).join('')}</span>`;
    return `<article class="${cls}" data-id="${esc(i.id)}"><div>
      <p class="t"><a href="${esc(i.link)}" target="_blank" rel="noopener">${esc(i.title)}</a></p>
      <div class="authors">${authors}</div>
      <div class="by">${dateStr(i.ts)}${i.updated && i.updated - i.ts > 86400 ? ' · updated ' + dateStr(i.updated) : ''}${cats}${tags}${i.comment ? ` · <span class="cats">${esc(i.comment)}</span>` : ''}</div>
      <div class="abs" hidden>${esc(i.desc)}</div>
      <div class="menu" hidden></div>
      <div class="acts"><button data-a="abs">abstract</button><a href="${esc(i.link)}" target="_blank" rel="noopener">abs ↗</a><a href="${esc(i.pdf)}" target="_blank" rel="noopener">pdf ↗</a><button data-a="copy" class="faint">copy link</button>${saveBtn}${seenBtn}</div></div></article>`;
  }
  return '';
}

async function setMark(id, field, on) {
  const item = data.items.find(i => i.id === id) || (axResults || []).find(i => i.id === id) || savedItems[id];
  const set = field === 'saved' ? saved : seen;
  on ? set.add(id) : set.delete(id);
  if (field === 'saved') { if (on && item) savedItems[id] = { ...item, saved_at: Date.now() / 1000 }; else delete savedItems[id]; }
  render();
  const snap = item && Object.fromEntries(Object.entries(item).filter(([k]) => ['kind', 'id', 'cid', 'title', 'author', 'authors', 'ts', 'updated', 'dur', 'tags', 'desc', 'link', 'pdf', 'cats', 'primary', 'by', 'comment'].includes(k)));
  try { await api('/api/mark', { id, field, on, item: snap }); }
  catch (e) { toast('could not save: ' + e.message); }
}

/* ------------------------------------------------------------ share to Zulip */

function findItem(id) { return data.items.find(i => i.id === id) || (axResults || []).find(i => i.id === id) || savedItems[id]; }
function snapshot(item) { return Object.fromEntries(Object.entries(item).filter(([k]) => ['kind', 'id', 'cid', 'title', 'author', 'authors', 'ts', 'updated', 'dur', 'tags', 'desc', 'link', 'pdf', 'cats', 'primary', 'by', 'comment'].includes(k))); }

function openShare(art) {
  const old = art.querySelector('.share'); if (old) { old.remove(); return; }
  const it = findItem(art.dataset.id); if (!it) return;
  const z = data.settings.zulip || {};
  const box = document.createElement('div'); box.className = 'share';
  if (!z.site || !z.email || !z.api_key_set) {
    box.innerHTML = `<span class="meta">sharing goes to Zulip, and it is not set up yet — </span><button class="link" data-a="share-settings">open settings</button>`;
  } else {
    box.innerHTML = `<textarea rows="3" placeholder="why is this worth their time? (LaTeX works: $…$)" data-share="note"></textarea>
      <input data-share="topic" value="${esc((it.title || '').slice(0, 60))}" placeholder="topic">
      <div class="row"><button class="solid" data-a="share-send">post to #${esc(z.stream || 'quietfeed')}</button><button class="link" data-a="share-cancel">cancel</button><span class="meta" data-share="status"></span></div>`;
  }
  art.querySelector('.acts').after(box);
  box.querySelector('textarea')?.focus();
}
async function sendShare(art) {
  const box = art.querySelector('.share'), it = findItem(art.dataset.id);
  const note = box.querySelector('[data-share=note]').value, topic = box.querySelector('[data-share=topic]').value, st = box.querySelector('[data-share=status]');
  st.textContent = 'posting…';
  try {
    const r = await api('/api/share', { item: snapshot(it), note, topic });
    box.remove(); toast(`shared to #${r.stream} › ${r.topic}`, 4000);
  } catch (e) { st.textContent = e.message; }
}

/* ------------------------------------------------------------ arXiv search */

async function searchArxiv() {
  const q = $('#ax_q').value.trim(), author = $('#ax_author').value.trim(), from = $('#ax_from').value, to = $('#ax_to').value, sort = $('#ax_sort').value;
  if (!q && !author && !from && !to) return;
  $('#list').innerHTML = '<div class="empty">asking arXiv…</div>';
  try {
    axResults = await api('/api/arxiv/search?' + new URLSearchParams({ q, author, from, to, sort }));
    axResults.forEach(r => { r.tags = []; });
    render();
  } catch (e) { axResults = null; render(); toast(e.message, 5000); }
}

/* ------------------------------------------------------------ library */

function renderLibrary() {
  const items = Object.values(savedItems).filter(i => i.title).sort((a, b) => (b.saved_at || 0) - (a.saved_at || 0));
  const counts = {}; items.forEach(i => counts[i.kind] = (counts[i.kind] || 0) + 1);
  const tags = [...new Set(items.flatMap(i => i.tags_mine || []))].sort();
  $('#libchips').innerHTML = kindChips(libKind, counts, tags, libTag, 'lib');
  const q = $('#libsearch').value.trim().toLowerCase();
  const shown = items.filter(i => (libKind === 'all' || i.kind === libKind) && (!libTag || (i.tags_mine || []).includes(libTag))
    && (!q || [i.title, i.author, i.desc, i.note, ...(i.authors || []), ...(i.tags_mine || [])].some(x => (x || '').toLowerCase().includes(q))));
  $('#liblist').innerHTML = shown.length ? shown.map(i => article(i).replace('</article>', libFields(i) + '</article>')).join('')
    : `<div class="empty">${items.length ? 'nothing matches' : 'nothing saved yet — use “save” on anything in the feed'}</div>`;
}
function libFields(i) {
  return `<div class="lib"><input class="tags" data-lk="tags" value="${esc((i.tags_mine || []).join(', '))}" placeholder="tags">
    <input class="note" data-lk="note" value="${esc(i.note || '')}" placeholder="a note to yourself"><span class="saved">saved ${dateStr(i.saved_at || 0)}</span></div>`;
}
const libTimers = {};
function queueLibUpdate(id, patch) {
  clearTimeout(libTimers[id]);
  libTimers[id] = setTimeout(async () => {
    try {
      await api('/api/mark', { id, field: 'saved', on: true, patch });
      const it = savedItems[id];
      if (it) { if ('tags' in patch) it.tags_mine = patch.tags.split(',').map(t => t.trim().toLowerCase()).filter(Boolean); if ('note' in patch) it.note = patch.note; }
    } catch (e) { toast('could not save: ' + e.message); }
  }, 500);
}

/* ------------------------------------------------------------ settings */

function fillSettings() {
  const s = data.settings;
  for (const k of ['hide_thumbnails', 'calm_titles', 'hide_seen', 'hide_shorts']) $('#s_' + k).checked = !!s[k];
  $('#s_min_minutes').value = s.min_minutes ?? 0;
  $('#s_max_per_channel').value = s.max_per_channel ?? 0;
  $('#s_block_words').value = (s.block_words || []).join('\n');
  const z = s.zulip || {};
  $('#z_site').value = z.site || ''; $('#z_stream').value = z.stream || 'quietfeed'; $('#z_email').value = z.email || '';
  $('#z_key').value = ''; $('#z_key').placeholder = z.api_key_set ? 'saved — leave empty to keep it' : 'Zulip → settings → account → API key';
  $('#z_status').textContent = '';
}
async function testZulip() {
  await saveSettings(true);
  $('#z_status').textContent = 'testing…';
  try { const r = await api('/api/zulip/test', {}); $('#z_status').textContent = `connected as ${r.name}; posting to #${r.stream}`; }
  catch (e) { $('#z_status').textContent = e.message; }
}
async function saveSettings(keepOpen) {
  const patch = {};
  for (const k of ['hide_thumbnails', 'calm_titles', 'hide_seen', 'hide_shorts']) patch[k] = $('#s_' + k).checked;
  patch.min_minutes = +$('#s_min_minutes').value || 0;
  patch.max_per_channel = +$('#s_max_per_channel').value || 0;
  patch.block_words = $('#s_block_words').value;
  patch.zulip = { site: $('#z_site').value, stream: $('#z_stream').value, email: $('#z_email').value, api_key: $('#z_key').value };
  try {
    data.settings = await api('/api/settings', patch);
    if ($('#z_key').value) { $('#z_key').value = ''; $('#z_key').placeholder = 'saved — leave empty to keep it'; }
    if (!keepOpen) { toast('saved'); $('#settings').hidden = true; await load(); }
  } catch (e) { toast('could not save: ' + e.message); }
}

/* ------------------------------------------------------------ following */

const HINTS = {
  yt: 'a name searches YouTube channels; @handle, channel id or link follows directly. For an X handle, switch the dropdown to X first',
  tw: 'a handle or an x.com link (no search on X)',
  rss: 'any site or feed link — substack.com pages, blogs, newsletters',
  arxiv: 'a category (math.PR, cs.LG, math for all of it), an author (au:Name Surname), or keywords to watch',
};

async function loadSubs() {
  try { subs = await api('/api/subs'); } catch (e) { toast(e.message); return; }
  if (view === 'following') renderSubs();
}

function personRow(s) {
  const av = s.kind === 'yt' && s.avatar ? `<img class="av" src="/avatar/yt/${esc(s.id)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'av',textContent:'yt'}))">`
    : `<div class="av">${{ tw: '𝕏', rss: 'rss', arxiv: 'arXiv' }[s.kind] || 'yt'}</div>`;
  const handle = s.kind === 'tw' ? '@' + s.id : s.kind === 'rss' ? (s.site || '').replace(/^https?:\/\//, '') : s.kind === 'arxiv' ? ({ category: 'category', author: 'author', search: 'keywords' }[s.qtype] || '') : (s.handle || '');
  return `<div class="person${s.muted ? ' muted' : ''}" data-kind="${s.kind}" data-id="${esc(s.id)}">${av}
    <div><div class="nm">${esc(s.name || handle || s.id)}<span class="h">${esc(handle)}</span></div>
    ${s.error ? `<div class="warn">last fetch failed: ${esc(s.error)}</div>` : ''}
    <div class="fields">
      <label>tags <input class="tags" data-k="tags" value="${esc((s.tags || []).join(', '))}" placeholder="math, cooking"></label>
      ${s.kind === 'yt' ? `<label>min <input class="mm" data-k="min_minutes" type="number" min="0" value="${s.min_minutes ?? ''}" placeholder="–"> min</label>` : ''}
      <label>why <input class="note" data-k="note" value="${esc(s.note || '')}" placeholder="why do I follow this?"></label>
    </div></div>
    <div class="side"><button class="link" data-a="mute">${s.muted ? 'unmute' : 'mute'}</button><button class="link" data-a="unfollow">unfollow</button></div></div>`;
}

function renderSubs() {
  if (!subs) { loadSubs(); return; }
  const q = $('#subfilter').value.trim().toLowerCase();
  const sort = $('#subsort').value;
  let list = subs.filter(s => !q || [s.name, s.handle, s.id, s.site, ...(s.tags || [])].some(x => (x || '').toLowerCase().includes(q)));
  const c = {}; subs.forEach(s => c[s.kind] = (c[s.kind] || 0) + 1);
  $('#subcount').textContent = Object.entries(c).map(([k, n]) => `${n} ${KIND_ONE[k]}${n === 1 ? '' : 's'}`).join(' · ') + (q ? ` · ${list.length} shown` : '');
  const byName = (a, b) => (a.name || a.id).localeCompare(b.name || b.id, undefined, { sensitivity: 'base' });
  if (sort === 'added') list.sort((a, b) => (b.added || 0) - (a.added || 0)); else list.sort(byName);
  let html = '';
  if (sort === 'tag') {
    const groups = {};
    for (const s of list) for (const t of (s.tags?.length ? s.tags : ['untagged'])) (groups[t] ||= []).push(s);
    for (const t of Object.keys(groups).sort((a, b) => a === 'untagged' ? 1 : b === 'untagged' ? -1 : a.localeCompare(b)))
      html += `<div class="taghead">#${esc(t)}</div>` + groups[t].map(personRow).join('');
  } else if (sort === 'kind') {
    for (const k of ['yt', 'tw', 'rss', 'arxiv']) { const g = list.filter(s => s.kind === k); if (g.length) html += `<div class="kindhead">${KIND_LABEL[k]}</div>` + g.map(personRow).join(''); }
  } else html = list.map(personRow).join('');
  $('#subs').innerHTML = html || '<div class="empty">nothing yet</div>';
}

const saveTimers = {};
function queueSubUpdate(kind, id, patch) {
  const key = kind + id;
  clearTimeout(saveTimers[key]);
  saveTimers[key] = setTimeout(async () => {
    try { await api('/api/subs/update', { kind, id, patch }); const s = subs.find(x => x.kind === kind && x.id === id); if (s) Object.assign(s, patch, patch.tags !== undefined ? { tags: String(patch.tags).split(',').map(t => t.trim().toLowerCase()).filter(Boolean) } : {}); }
    catch (e) { toast('could not save: ' + e.message); }
  }, 500);
}

function guessKind(q) {
  // mirrors the server's parse_ref: what is unambiguous switches the dropdown so you see what will happen
  if (/^(https?:\/\/)?(www\.|m\.|mobile\.)?(x\.com|twitter\.com|xcancel\.com|nitter\.)/i.test(q)) return 'tw';
  if (/^(https?:\/\/)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)\//i.test(q) || /^UC[\w-]{22}$/.test(q)) return 'yt';
  if (/^(https?:\/\/)?(www\.)?arxiv\.org\//i.test(q) || /^(au|author|cat):/i.test(q)) return 'arxiv';
  if (/^(math|cs|stat|physics|q-bio|q-fin|econ|eess|nlin|astro-ph|cond-mat|hep-th|hep-ph|hep-ex|hep-lat|gr-qc|quant-ph|nucl-th|nucl-ex|math-ph)(\.[A-Za-z-]{2,12})?$/.test(q)) return 'arxiv';
  if (/^https?:\/\//i.test(q) || (!q.startsWith('@') && /^([\w-]+\.)+[a-z]{2,24}(\/\S*)?$/i.test(q))) return 'rss';
  return null;
}

function looksDirect(q, kind) {
  if (kind !== 'yt') return true;
  return /^UC[\w-]{22}$/.test(q) || q.startsWith('@') || /^(https?:\/\/|www\.)/.test(q) || /^[\w.-]+\.(com|be)\//.test(q);
}

async function followRef(ref, kind, name) {
  const r = await api('/api/subs/add', { ref, kind, name });
  if (r.added.length) { const a = r.added[0]; toast(`now following ${a.name || a.handle || a.id}` + (a.matched ? ` (${a.matched})` : ''), 4000); }
  else toast('already following');
  await loadSubs(); setTimeout(load, 5000);
  return r;
}

async function addOrSearch() {
  const q = $('#addq').value.trim(), kind = $('#addkind').value;
  if (!q) return;
  if (looksDirect(q, kind)) {
    $('#results').innerHTML = '<div class="meta">adding…</div>';
    try { const r = await followRef(q, kind); $('#results').innerHTML = ''; if (r.added.length) $('#addq').value = ''; }
    catch (e) { $('#results').innerHTML = `<div class="warn">${esc(e.message)}</div>`; }
    return;
  }
  $('#results').innerHTML = '<div class="meta">searching…</div>';
  try {
    const rs = await api('/api/search?q=' + encodeURIComponent(q));
    if (!rs.length) { $('#results').innerHTML = '<div class="meta">no channels found</div>'; return; }
    $('#results').innerHTML = rs.map(r => `<div class="person" data-kind="yt" data-id="${esc(r.id)}">
      ${r.avatar ? `<img class="av" src="/img?u=${encodeURIComponent(r.avatar)}" alt="" loading="lazy">` : '<div class="av">yt</div>'}
      <div><div class="nm">${esc(r.name)}<span class="h">${esc(r.handle)}${r.subs ? ' · ' + esc(r.subs) : ''}</span></div><div class="d">${esc(r.desc)}</div></div>
      <div class="side">${r.following ? '<span class="meta">following</span>' : `<button class="solid" data-a="follow" data-name="${esc(r.name)}">follow</button>`}</div></div>`).join('');
  } catch (e) { $('#results').innerHTML = `<div class="warn">${esc(e.message)}</div>`; }
}

/* ------------------------------------------------------------ account sync */

let syncTimer = null;
async function loadSync() {
  let s;
  try { s = await api('/api/sync'); } catch (e) { return; }
  const when = x => x.at ? ' · ' + ago(x.at) + ' ago' : '';
  const g = s.google;
  $('#gstatus').textContent = !g.configured ? 'needs client_secret.json next to quietfeed (see README)'
    : !g.connected ? 'not connected'
    : g.ok === false ? 'error: ' + g.error
    : g.count != null ? `connected · ${g.count} subscriptions, ${g.added} new${when(g)}` : 'connected · syncing on next refresh';
  $('#gconnect').hidden = !g.configured || g.connected;
  $('#gdisconnect').hidden = !g.connected;
  $('#syncbrowser').value = s.browser.name || '';
  const b = s.browser;
  $('#bstatus').textContent = !b.name ? '' : b.ok === false ? 'error: ' + b.error : b.count != null ? `${b.count} found, ${b.added} new${when(b)}` : 'on next refresh';
  const x = s.x;
  $('#xstatus').textContent = x.pending ? `${x.pending} ids still waiting to be resolved (retried every refresh)`
    : x.resolved ? `resolved ${x.resolved}${when(x)}` : "drop your archive's following.js below; ids are resolved to handles in the background";
  clearTimeout(syncTimer);
  if (view === 'following' && (g.connected || b.name || x.pending)) syncTimer = setTimeout(loadSync, 15000);
}

async function connectGoogle() {
  try {
    const { url } = await api('/api/sync/google/start', {});
    window.open(url, '_blank');
    $('#gstatus').textContent = 'waiting for Google… finish in the tab that just opened';
    const poll = setInterval(async () => { const s = await api('/api/sync'); if (s.google.connected) { clearInterval(poll); loadSync(); loadSubs(); setTimeout(load, 6000); } }, 3000);
    setTimeout(() => clearInterval(poll), 5 * 60000);
  } catch (e) { $('#gstatus').textContent = e.message; }
}

function showImport(r, label) {
  const o = $('#importout'); o.hidden = false;
  const lines = [`${label}: ${r.found ?? r.added.length} found, ${r.added.length} added, ${r.skipped} already followed` + (r.pending ? `, ${r.pending} X ids queued for background resolution` : '') + (r.failed.length ? `, ${r.failed.length} failed` : '')];
  if (r.added.length) lines.push('added: ' + r.added.slice(0, 40).map(a => a.matched ? a.matched : (a.name || a.handle || ('@' + a.id))).join('; ') + (r.added.length > 40 ? ' …' : ''));
  if (r.failed.length) lines.push('failed:\n  ' + r.failed.slice(0, 20).join('\n  '));
  if (r.bad?.length) lines.push('not understood: ' + r.bad.join(' | '));
  o.textContent = (o.textContent ? o.textContent + '\n\n' : '') + lines.join('\n');
  if (r.added.length) { toast(`added ${r.added.length}; fetching…`); loadSubs(); setTimeout(load, 6000); }
}

async function importText(text, filename) {
  const kind = $('#importkind').value;
  $('#importout').hidden = false; $('#importout').textContent += (filename ? filename : 'pasted list') + ': working…\n';
  try { showImport(await api('/api/import', { text, filename, kind }), filename || 'pasted list'); }
  catch (e) { $('#importout').textContent += `${filename || 'pasted list'}: ${e.message}\n`; }
}
async function importFiles(files) {
  for (const f of files) importText(await f.text(), f.name);
}

/* ------------------------------------------------------------ events */

document.addEventListener('click', async e => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.v) { view = b.dataset.v; render(); if (view === 'following') { loadSubs(); loadSync(); } else load(); return; }
  if (b.dataset.f) { kindFilter = b.dataset.f; if (kindFilter !== 'arxiv') axResults = null; render(); return; }
  if (b.dataset.tag !== undefined) { tagFilter = tagFilter === b.dataset.tag ? null : b.dataset.tag; render(); return; }
  if (b.dataset.libf) { libKind = b.dataset.libf; render(); return; }
  if (b.dataset.libtag !== undefined) { libTag = libTag === b.dataset.libtag ? null : b.dataset.libtag; render(); return; }
  if (b.dataset.w !== undefined) { paperWindow = +b.dataset.w; axResults = null; render(); return; }
  if (b.id === 'ax_clear') { axResults = null; render(); return; }
  if (b.id === 'theme') { cycleTheme(); return; }
  if (b.id === 'rf') { toast('refreshing…'); await api('/api/refresh', {}); setTimeout(load, 4000); return; }
  if (b.id === 'gear') { const p = $('#settings'); p.hidden = !p.hidden; if (!p.hidden) fillSettings(); return; }
  if (b.id === 's_save') { saveSettings(); return; }
  if (b.id === 'z_test') { testZulip(); return; }
  if (b.id === 'quit') { if (confirm('stop quietfeed? the page will stop updating until you start it again.')) { await api('/api/quit', {}); document.body.innerHTML = '<main><p class="meta">quietfeed stopped. start it again to continue.</p></main>'; } return; }
  if (b.id === 'importbtn') { const t = $('#paste').value.trim(); if (t) { importText(t, ''); $('#paste').value = ''; } return; }
  if (b.id === 'gconnect') { connectGoogle(); return; }
  if (b.id === 'gdisconnect') { await api('/api/sync/google/disconnect', {}); loadSync(); return; }
  if (b.id === 'syncnow') { toast('syncing…'); await api('/api/sync/now', {}); setTimeout(() => { loadSync(); loadSubs(); }, 8000); return; }
  if (b.id === 'browserbtn') {
    const browser = $('#browser').value;
    $('#importout').hidden = false; $('#importout').textContent += `${browser}: reading YouTube cookies and your subscriptions page…\n`;
    try { showImport(await api('/api/import/browser', { browser }), browser); } catch (err) { $('#importout').textContent += `${browser}: ${err.message}\n`; }
    return;
  }
  const person = b.closest('.person');
  if (person) {
    const { kind, id } = person.dataset;
    if (b.dataset.a === 'follow') {
      b.disabled = true; b.textContent = 'adding…';
      try { await followRef(id, 'yt', b.dataset.name); b.replaceWith(Object.assign(document.createElement('span'), { className: 'meta', textContent: 'following' })); }
      catch (err) { b.disabled = false; b.textContent = 'follow'; toast(err.message); }
    }
    if (b.dataset.a === 'mute') {
      const s = subs.find(x => x.kind === kind && x.id === id); s.muted = !s.muted;
      await api('/api/subs/update', { kind, id, patch: { muted: s.muted } }); renderSubs();
    }
    if (b.dataset.a === 'unfollow') {
      const s = subs.find(x => x.kind === kind && x.id === id);
      if (!confirm(`unfollow ${s.name || s.id}?`)) return;
      await api('/api/subs/remove', { kind, id }); subs = subs.filter(x => x !== s); renderSubs(); toast('unfollowed');
    }
    return;
  }
  const art = b.closest('article');
  if (!art) return;
  const id = art.dataset.id;
  if (b.dataset.a === 'papers') { kindFilter = 'arxiv'; render(); window.scrollTo(0, 0); return; }
  if (b.dataset.a === 'share') { openShare(art); return; }
  if (b.dataset.a === 'share-send') { sendShare(art); return; }
  if (b.dataset.a === 'share-cancel') { art.querySelector('.share')?.remove(); return; }
  if (b.dataset.a === 'share-settings') { $('#settings').hidden = false; fillSettings(); window.scrollTo(0, 0); return; }
  if (b.dataset.a === 'abs') { const a = art.querySelector('.abs'); a.hidden = !a.hidden; b.textContent = a.hidden ? 'abstract' : 'hide abstract'; return; }
  if (b.dataset.a === 'copy') { const it = (data.items.find(i => i.id === id) || (axResults || []).find(i => i.id === id) || savedItems[id]); try { await navigator.clipboard.writeText(it.link); toast('link copied'); } catch (err) { toast(it.link, 6000); } return; }
  if (b.dataset.author !== undefined) {
    const a = b.dataset.author, m = art.querySelector('.menu'), mine = (data.arxiv_authors || []).some(x => x.toLowerCase() === a.toLowerCase());
    m.hidden = !m.hidden || m.dataset.author !== a; m.dataset.author = a;
    m.innerHTML = m.hidden ? '' : `<span>${esc(a)}:</span> ${mine ? '<span>followed</span>' : `<button data-a="follow-author">follow</button>`}<button data-a="search-author">their papers</button>`;
    return;
  }
  if (b.dataset.a === 'follow-author') { const a = art.querySelector('.menu').dataset.author; try { await followRef('au:' + a, 'arxiv'); art.querySelector('.menu').hidden = true; } catch (err) { toast(err.message); } return; }
  if (b.dataset.a === 'search-author') { const a = art.querySelector('.menu').dataset.author; kindFilter = 'arxiv'; $('#ax_author').value = a; $('#ax_q').value = ''; $('#ax_from').value = $('#ax_to').value = ''; render(); searchArxiv(); window.scrollTo(0, 0); return; }
  if (b.dataset.a === 'seen') return setMark(id, 'seen', !seen.has(id));
  if (b.dataset.a === 'save') return setMark(id, 'saved', !saved.has(id));
  if (b.dataset.a === 'play' || b.dataset.a === 'dl') {
    const r = await fetch('/api/' + b.dataset.a + '?id=' + encodeURIComponent(id), { method: 'POST' });
    toast(await r.text());
    if (b.dataset.a === 'play') { seen.add(id); render(); }
  }
});

document.addEventListener('input', e => {
  const inp = e.target;
  if (inp.id === 'subfilter') return renderSubs();
  if (inp.id === 'addq') { const k = guessKind(inp.value.trim()); if (k && $('#addkind').value !== k) { $('#addkind').value = k; $('#addhint').textContent = HINTS[k]; } return; }
  if (inp.id === 'libsearch') return renderLibrary();
  if (inp.dataset.k && inp.closest('.person')) {
    const { kind, id } = inp.closest('.person').dataset;
    queueSubUpdate(kind, id, { [inp.dataset.k]: inp.value });
  }
  if (inp.dataset.lk && inp.closest('article')) queueLibUpdate(inp.closest('article').dataset.id, { [inp.dataset.lk]: inp.value });
});
document.addEventListener('change', async e => {
  if (e.target.id === 'subsort') renderSubs();
  if (e.target.id === 'file') importFiles(e.target.files);
  if (e.target.id === 'addkind') $('#addhint').textContent = HINTS[e.target.value];
  if (e.target.id === 'syncbrowser') { await api('/api/sync/browser', { browser: e.target.value }); toast(e.target.value ? 'will re-read on every refresh' : 'browser sync off'); loadSync(); }
});
$('#addf').addEventListener('submit', e => { e.preventDefault(); addOrSearch(); });
$('#axsearch').addEventListener('submit', e => { e.preventDefault(); searchArxiv(); });
const drop = $('#drop');
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); importFiles(e.dataTransfer.files); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') $('#settings').hidden = true; });

let theme = null; try { theme = localStorage.getItem('theme'); } catch (e) { /* ignore */ }
applyTheme(theme);
$('#addhint').textContent = HINTS.yt;
if (location.hash === '#following') view = 'following';
load();
if (view === 'following') { loadSubs(); loadSync(); }
setInterval(() => { if (view === 'feed' && !axResults) load(); }, 60000);
