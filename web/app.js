/* feedel front-end. Talks only to localhost; every YouTube image is proxied by the server. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const KIND_LABEL = { yt: 'videos', tw: 'tweets', rss: 'posts', arxiv: 'papers', ig: 'reels' };
const KIND_ONE = { yt: 'channel', tw: 'account', rss: 'feed', arxiv: 'query' };

let view = 'feed', kindFilter = 'all', tagFilter = null, paperWindow = 1, axResults = null, feedQuery = '', ytResults = null;
let paperTypes = { new: true, cross: true, replace: false };  // what arXiv announced; replacements are off until asked for
const ANNOUNCE_RANK = { new: 0, cross: 1, replace: 2 };
let kwre = null;  // the keyword watches, as one regex, rebuilt at each render
let curId = null, curIndex = -1;  // the item the keyboard is on
const SNAP_KEYS = ['kind', 'id', 'cid', 'title', 'author', 'authors', 'ts', 'updated', 'dur', 'tags', 'desc', 'link', 'pdf', 'cats', 'primary', 'by', 'comment', 'announce_date', 'announce_type', 'replaced'];
let libKind = 'all', libTag = null;
let data = null, subs = null;
let loadSig = '', refreshPoll = null, refreshPolls = 0;  // while the server is refreshing, the feed is re-read every 3 s
const seen = new Set(), saved = new Set();
let savedItems = {};

function toast(m, ms = 2500) { const e = $('#msg'); e.textContent = m; e.style.opacity = 1; clearTimeout(toast.t); toast.t = setTimeout(() => e.style.opacity = 0, ms); }

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const ct = r.headers.get('content-type') || '';
  const out = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) {
    // a route this page knows but the server does not: the server is older than the page
    if ((r.status === 404 || (r.status === 400 && out === 'bad request')) && path.startsWith('/api/')) { const n = $('#stale'); if (n) n.hidden = false; throw new Error('this copy of feedel is older than the page — restart it (notice at the top)'); }
    throw new Error((out && out.error) || out || r.statusText);
  }
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
function dur(s) { if (s == null) return ''; const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h} h ${m} min` : m ? `${m} min` : `${Math.round(s)} s`; }
function dateStr(ts) { return new Date(ts * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }); }
function isoDay(ts) { const d = new Date(ts * 1000); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
function listDay(i) { return i.announce_date || isoDay(i.ts); }  // the arXiv listing a paper belongs to (its own day for papers from the API)
function listingLabel(day) { const [y, m, d] = day.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' }); }

/* keyword watches: their words are highlighted inside titles and abstracts, and papers that match come first within their day */
function kwRegex() {
  const k = (data && data.arxiv_keywords || []).map(x => x.trim()).filter(Boolean);
  return k.length ? new RegExp('(' + k.map(x => esc(x).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi') : null;
}
function hl(s) { const t = esc(s); return kwre ? t.replace(kwre, '<mark>$1</mark>') : t; }
function kwHits(i) {
  if (!kwre || i.kind !== 'arxiv') return [];
  const found = new Set(); let m; const t = esc((i.title || '') + ' ' + (i.desc || '')); kwre.lastIndex = 0;
  while ((m = kwre.exec(t))) found.add(m[1].toLowerCase());
  return [...found];
}

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

async function load(onlyIfChanged = false) {
  let d;
  try { d = await api('/api/items'); } catch (e) { toast('server not answering: ' + e.message); return; }
  pollWhileRefreshing(d.refreshing);
  setMsgCount(d.messages_unread || 0);
  const sig = [d.refreshing, d.last_refresh, d.errors.length, d.inbox.length, d.items.map(i => i.id).join(',')].join('|');
  if (onlyIfChanged && sig === loadSig) return;  // nothing new yet: leave the page alone
  loadSig = sig; data = d;
  seen.clear(); data.seen.forEach(i => seen.add(i));
  saved.clear(); data.saved.forEach(i => saved.add(i));
  savedItems = {}; (data.saved_items || []).forEach(i => { savedItems[i.id] = i; });
  $('#authors').innerHTML = (data.arxiv_authors || []).map(a => `<option value="${esc(a)}">`).join('');
  $('#stale').hidden = !data.stale;
  showUpdate(d.update);
  migrateLocalSeen();
  render();
}

// the server merges each source into the feed as it answers, so while it says "refreshing" the page re-reads the
// feed every 3 s (re-rendering only when something changed) instead of waiting for the slowest source
function pollWhileRefreshing(refreshing) {
  clearTimeout(refreshPoll);
  if (!refreshing) { refreshPolls = 0; return; }
  if (refreshPolls++ > 200) return;  // ten minutes: fall back to the minute timer
  refreshPoll = setTimeout(() => load(true), 3000);
}

async function restartServer() {
  const st = $('#restart-status'); st.textContent = 'restarting…';
  try { await api('/api/restart', {}); } catch (e) { /* the old process is gone before it can answer cleanly */ }
  const t0 = Date.now();
  const poll = setInterval(async () => {
    try { const r = await fetch('/api/items', { cache: 'no-store' }); if (r.ok) { clearInterval(poll); location.reload(); } }
    catch (e) { if (Date.now() - t0 > 30000) { clearInterval(poll); st.textContent = 'it did not come back; start it by hand'; } }
  }, 1000);
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
  captureDrafts();
  kwre = kwRegex();
  document.querySelectorAll('.views button').forEach(b => b.classList.toggle('on', b.dataset.v === view));
  ['feed', 'talk', 'library', 'following', 'source', 'friends', 'messages'].forEach(v => $('#view-' + v).hidden = v !== view);
  if (!data) return;
  if (view === 'feed') renderFeed();
  else if (view === 'talk') renderTalk();
  else if (view === 'friends') renderFriends();
  else if (view === 'messages') renderMessages();
  else if (view === 'library') renderLibrary();
  else if (view === 'source') { if (src) renderSource(); }
  else renderSubs();
  restoreThreads();
  for (const [id, st] of twOpen) { const a = document.querySelector(`article[data-id="${CSS.escape(id)}"]`); if (a && !a.querySelector('.twthread')) openTwThread(a, st.cur, false, st.stack); }
  restoreInspire();
  restoreCur();
  fillInspire();
}

/* ------------------------------------------------------------ discussions (Zulip as the backend) */

// the talk tab: the streams from settings, one list of topics per stream (cached until something is posted),
// or the results of a search within the selected stream
let talkStream = null, talkTopics = {}, talkSearch = null;

function zulipStreams() { return ((data && data.zulip && data.zulip.streams) || []).map(s => s.name); }
function curStream() {
  const ss = zulipStreams();
  if (!talkStream || !ss.includes(talkStream)) talkStream = ss[0] || (data && data.zulip && data.zulip.stream) || 'feedel';
  return talkStream;
}

async function renderTalk() {
  const L = $('#talklist');
  captureDrafts();
  if (!data.zulip || !data.zulip.on) {
    $('#streams').innerHTML = ''; $('#talkwhen').textContent = ''; $('#talkerrs').innerHTML = '';
    L.innerHTML = '<div class="empty">talk reads your group\'s Zulip. set it up under the gear: site, e-mail, API key and a stream.</div>';
    return;
  }
  const stream = curStream();
  $('#streams').innerHTML = zulipStreams().map(s => `<button data-stream="${esc(s)}" class="${s === stream ? 'on' : ''}">#${esc(s)}</button>`).join('');
  $('#talkq').placeholder = `search #${stream}`;
  $('#nt_send').textContent = `post to #${stream}`;
  const st = (data.zulip.streams || []).find(s => s.name === stream);
  $('#talkwhen').textContent = st && st.at ? 'read ' + ago(st.at) + ' ago' : '';
  $('#talkerrs').innerHTML = st && st.error ? `<div class="err">Zulip: ${esc(st.error)}</div>` : '';
  if (talkSearch && talkSearch.stream === stream) { renderSearch(talkSearch); return; }
  $('#talkq_clear').hidden = true;
  let t = talkTopics[stream];
  if (!t) {
    if (!L.querySelector('.topic')) L.innerHTML = '<div class="empty">reading the stream…</div>';
    try { t = await api('/api/zulip/topics?stream=' + encodeURIComponent(stream)); }
    catch (e) { L.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    talkTopics[stream] = t;
    if (talkStream !== stream || view !== 'talk' || talkSearch) return;  // the reader moved on while this loaded
    captureDrafts();
  }
  if (t.error) $('#talkerrs').innerHTML = `<div class="err">Zulip: ${esc(t.error)}</div>`;
  if (!t.topics.length) { L.innerHTML = `<div class="empty">nothing in #${esc(stream)} yet. <b>+ topic</b> starts one${st && st.share ? ', and “share” on any item does too' : ''}.</div>`; return; }
  L.innerHTML = t.topics.map(topicRow).join('');
  restoreThreads();
}
function topicMeta(t) {
  return `${esc(t.people.join(', '))} · ${t.count} message${t.count > 1 ? 's' : ''} · last ${ago(t.last_ts)} ago${t.new ? ` · <span class="new">${t.new} new</span>` : ''}`;
}
function topicActs(t) {
  return `<div class="acts"><button data-a="disc" data-topic="${esc(t.topic)}" data-stream="${esc(t.stream)}">${threads.has(topicKey(t.stream, t.topic)) ? 'close' : 'open'} discussion</button><a class="faint" href="${zulipTopicUrl(t.topic, t.stream)}" target="_blank" rel="noopener">in Zulip ↗</a></div>`;
}
function topicRow(t) {
  return `<div class="topic" data-topic="${esc(t.topic)}" data-stream="${esc(t.stream)}">
    <p class="t">${esc(t.topic)}</p>
    <div class="meta">${topicMeta(t)}</div>
    <div class="last">${esc(t.last_who || '')}: ${esc(t.last)}</div>
    ${t.item ? article(t.item).replace(/<div class="disc">[\s\S]*?<\/div>/, '') : ''}
    ${topicActs(t)}</div>`;
}

function renderSearch(r) {
  const L = $('#talklist');
  $('#talkq_clear').hidden = false;
  if (!r.topics.length) { L.innerHTML = `<div class="empty">nothing in #${esc(r.stream)} matches “${esc(r.query)}”</div>`; return; }
  const n = r.topics.reduce((a, t) => a + t.messages.length, 0);
  L.innerHTML = `<div class="day">${n} message${n > 1 ? 's' : ''} in ${r.topics.length} topic${r.topics.length > 1 ? 's' : ''}</div>` + r.topics.map(t => `<div class="topic" data-topic="${esc(t.topic)}" data-stream="${esc(t.stream)}">
    <p class="t">${t.topic_html || esc(t.topic)}</p>
    ${t.messages.map(m => `<div class="hit"><div class="who"><b>${esc(m.who)}</b> · ${ago(m.ts)} ago</div><div class="zc">${m.content}</div></div>`).join('')}
    ${topicActs(t)}</div>`).join('');
  restoreThreads();
}
async function searchTalk() {
  const q = $('#talkq').value.trim(), stream = curStream();
  if (!q) { talkSearch = null; renderTalk(); return; }
  $('#talklist').innerHTML = '<div class="empty">asking Zulip…</div>';
  try { talkSearch = await api('/api/zulip/search?' + new URLSearchParams({ stream, q })); renderTalk(); }
  catch (e) { talkSearch = null; $('#talklist').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function postTopic() {
  const topic = $('#nt_topic').value.trim(), content = $('#nt_body').value, st = $('#nt_status'), stream = curStream();
  if (!topic) { $('#nt_topic').focus(); return; }
  if (!content.trim()) { $('#nt_body').focus(); return; }
  st.textContent = 'posting…'; $('#nt_send').disabled = true;
  try {
    const r = await api('/api/zulip/post', { stream, topic, content });
    $('#nt_topic').value = ''; $('#nt_body').value = ''; $('#newtopic').hidden = true; st.textContent = '';
    talkTopics = {}; talkSearch = null; $('#talkq').value = '';
    toast(`posted to #${r.stream} › ${r.topic}`, 4000);
    await load();
  } catch (e) { st.textContent = e.message; }
  $('#nt_send').disabled = false;
}

// when a thread you have open loses its "new", the row above it and the line under the item say so too
function bumpNew(stream, topic, n) {
  const fix = o => { if (o && o.topic === topic && (o.stream || stream) === stream) o.new = Math.max(0, (o.new || 0) - n); };
  Object.values(talkTopics).forEach(t => (t.topics || []).forEach(fix));
  Object.values((data && data.discussions) || {}).forEach(fix);
  (talkTopics[stream] ? talkTopics[stream].topics : []).forEach(t => {
    if (t.topic !== topic) return;
    const row = document.querySelector(`.topic[data-stream="${CSS.escape(stream)}"][data-topic="${CSS.escape(topic)}"] > .meta`);
    if (row) row.innerHTML = topicMeta(t);
  });
  document.querySelectorAll('article[data-id]').forEach(art => {
    const d = data && data.discussions && data.discussions[art.dataset.id];
    const el = d && d.topic === topic && (d.stream || stream) === stream && art.querySelector(':scope > div > .disc');
    if (el) el.outerHTML = discLine({ id: art.dataset.id });
  });
}

// open threads survive re-renders (the feed refreshes itself every minute): host key → {topic, stream, msgs, draft}
const threads = new Map();
const topicKey = (stream, topic) => 'topic:' + stream + '\n' + topic;
const hostKey = host => host.dataset.id ? 'id:' + host.dataset.id : topicKey(host.dataset.stream || '', host.dataset.topic);
function findHost(key) {
  if (key.startsWith('id:')) return document.querySelector(`article[data-id="${CSS.escape(key.slice(3))}"]`);
  const i = key.indexOf('\n'), stream = key.slice(6, i), topic = key.slice(i + 1);
  return document.querySelector(`.topic[data-stream="${CSS.escape(stream)}"][data-topic="${CSS.escape(topic)}"]`);
}
function threadBox(host) { return host.querySelector(':scope > .thread, :scope > div > .thread'); }

// reactions: Zulip sends the emoji name and codepoint; a few common ones are offered when you click "+"
const EMOJI_PICK = [['+1', '1f44d'], ['heart', '2764'], ['tada', '1f389'], ['thinking', '1f914'], ['eyes', '1f440'], ['joy', '1f602']];
function emojiChar(r) {
  if (r.type && r.type !== 'unicode_emoji') return ':' + r.name + ':';
  const code = r.code || '';
  if (/^[0-9a-f]+(-[0-9a-f]+)*$/i.test(code)) { try { return String.fromCodePoint(...code.split('-').map(h => parseInt(h, 16))); } catch (e) { /* fall through */ } }
  const known = EMOJI_PICK.find(p => p[0] === r.name);
  return known ? String.fromCodePoint(parseInt(known[1], 16)) : ':' + r.name + ':';
}
function editBox(text) {
  return `<div class="edit"><textarea rows="3" data-edit>${esc(text)}</textarea><div class="row"><button class="solid" data-a="edit-save">save</button><button class="link" data-a="edit-cancel">cancel</button><span class="meta" data-edit-status></span></div></div>`;
}
function msgHtml(m, edit) {
  const reacts = (m.reactions || []).map(r => `<button data-a="react" data-emoji="${esc(r.name)}" data-code="${esc(r.code || '')}" data-type="${esc(r.type || '')}" class="${r.mine ? 'mine' : ''}" title=":${esc(r.name)}:${r.mine ? ' — you; click to take it back' : ''}">${emojiChar(r)} ${r.count}</button>`).join('');
  const editing = edit && edit.mid === m.id;
  return `<div class="msg" data-mid="${m.id}"><div class="who"><b>${esc(m.who)}</b> · ${ago(m.ts)} ago${m.edited ? ' <span class="edited">· edited</span>' : ''}${m.fresh ? ' <span class="new">· new</span>' : ''}${m.mine && !editing ? '<button data-a="edit">edit</button>' : ''}</div>
    ${editing ? editBox(edit.text) : `<div class="zc">${m.content}</div>`}
    <div class="reacts">${reacts}<button class="add" data-a="react-pick" title="react">+</button></div></div>`;
}
function renderThread(box, t, draft = '') {
  box.dataset.topic = t.topic; box.dataset.stream = t.stream;
  box.innerHTML = `<div class="row thhead"><span class="meta">#${esc(t.stream)} › ${esc(t.topic)}</span><span class="grow"></span><button class="link" data-a="thread-refresh">refresh</button><button class="link" data-a="thread-close">close</button></div>`
    + (t.msgs === null ? '<div class="meta">loading…</div>' : t.msgs.length ? t.msgs.map(m => msgHtml(m, t.edit)).join('') : '<div class="meta">no messages yet</div>')
    + `<textarea rows="2" placeholder="reply — markdown and $LaTeX$ work" data-reply>${esc(draft)}</textarea>
       <div class="row"><button class="solid" data-a="reply">post</button><a class="meta" href="${zulipTopicUrl(t.topic, t.stream)}" target="_blank" rel="noopener">open in Zulip ↗</a><span class="meta" data-reply-status></span></div>`;
}
function mountThread(host, t, draft) {
  let box = threadBox(host);
  if (!box) { box = document.createElement('div'); box.className = 'thread'; (host.querySelector(':scope > .acts, :scope > div > .acts') || host).after(box); }
  renderThread(box, t, draft);
  return box;
}
function discLabel(host, open) { const b = host.querySelector(':scope > .acts [data-a=disc], :scope > div > .disc [data-a=disc], :scope > div > .acts [data-a=disc]'); if (b) b.textContent = (open ? 'close' : 'open') + ' discussion'; }
async function fetchThread(t) {
  const msgs = await api('/api/zulip/topic?' + new URLSearchParams({ name: t.topic, stream: t.stream }));
  const fresh = new Set((t.msgs || []).filter(m => m.fresh).map(m => m.id));  // what was new when you opened it stays marked while it is open
  msgs.forEach(m => { m.fresh = fresh.has(m.id) || m.read === false; });
  markRead(t, msgs);
  return msgs;
}
function markRead(t, msgs) {
  // Zulip's own read flag, so its apps and feedel agree on what you have seen; the row's "n new" follows
  const ids = msgs.filter(m => m.read === false).map(m => m.id);
  if (!ids.length) return;
  msgs.forEach(m => { if (ids.includes(m.id)) m.read = true; });
  api('/api/zulip/read', { ids }).then(() => bumpNew(t.stream, t.topic, ids.length)).catch(() => { /* asked again next time */ });
}
async function openThread(host, topic, stream) {
  const key = hostKey(host);
  if (threads.has(key)) { closeThread(host); return; }
  const t = { topic, stream: stream || (data.zulip && data.zulip.stream) || 'feedel', msgs: null, draft: '' };
  threads.set(key, t);
  const box = mountThread(host, t);
  discLabel(host, true);
  try { t.msgs = await fetchThread(t); if (threads.get(key) === t) { renderThread(box, t); box.querySelector('[data-reply]').focus(); } }
  catch (e) { threads.delete(key); box.innerHTML = `<span class="warn">${esc(e.message)}</span>`; discLabel(host, false); }
}
function closeThread(host) { threads.delete(hostKey(host)); threadBox(host)?.remove(); discLabel(host, false); }
async function refreshThread(host, keepDraft = true) {
  const key = hostKey(host), t = threads.get(key); if (!t) return;
  const box = threadBox(host), draft = keepDraft && box ? box.querySelector('[data-reply]')?.value || '' : '';
  try { t.msgs = await fetchThread(t); mountThread(host, t, draft); }
  catch (e) { toast(e.message); }
}
function captureDrafts() {  // before a re-render wipes the DOM, keep what was typed in each open thread: the reply, and an edit in progress
  for (const [key, t] of threads) {
    const host = findHost(key), box = host && threadBox(host); if (!box) continue;
    t.draft = box.querySelector('[data-reply]')?.value || '';
    const ed = box.querySelector('.msg [data-edit]');
    if (ed) t.edit = { mid: +ed.closest('.msg').dataset.mid, text: ed.value }; else if (t.edit && !t.editOpening) t.edit = null;
  }
}
function restoreThreads() {
  for (const [key, t] of threads) { const host = findHost(key); if (host && !threadBox(host)) mountThread(host, t, t.draft || ''); }
}

async function sendReply(box) {
  const ta = box.querySelector('[data-reply]'), st = box.querySelector('[data-reply-status]'), host = box.closest('article, .topic'), t = threads.get(hostKey(host));
  if (!t || !ta.value.trim()) return;
  st.textContent = 'posting…';
  try {
    await api('/api/zulip/reply', { topic: t.topic, stream: t.stream, content: ta.value });
    ta.value = '';
    await refreshThread(host, false);  // the thread stays open and simply gains the message
    const b = threadBox(host); if (b) b.querySelector('[data-reply-status]').textContent = 'posted';
    talkTopics = {};  // the topic list and the "who talks about this" lines catch up on the next render
  } catch (e) { st.textContent = e.message; }
}

async function react(btn, name, code, type) {
  const msgEl = btn.closest('.msg'), host = btn.closest('article, .topic'), t = threads.get(hostKey(host));
  const mid = +msgEl.dataset.mid, m = t && t.msgs && t.msgs.find(x => x.id === mid); if (!m) return;
  const cur = (m.reactions || []).find(r => r.name === name), on = !(cur && cur.mine);
  btn.disabled = true;
  try {
    const r = await api('/api/zulip/react', { id: mid, emoji: name, code: code || (cur && cur.code) || '', type: type || (cur && cur.type) || '', on });
    if (r.message) m.reactions = r.message.reactions;
    msgEl.outerHTML = msgHtml(m, t.edit);
  } catch (e) { toast(e.message); btn.disabled = false; }
}
async function startEdit(btn) {
  const msgEl = btn.closest('.msg'), mid = +msgEl.dataset.mid, host = btn.closest('article, .topic'), t = threads.get(hostKey(host));
  if (!t) return;
  btn.disabled = true; t.editOpening = true;
  try {
    const r = await api('/api/zulip/raw?id=' + mid);  // the markdown source, not the rendered HTML
    t.edit = { mid, text: r.content };
    const box = mountThread(host, t, threadBox(host)?.querySelector('[data-reply]')?.value || '');
    box.querySelector('[data-edit]')?.focus();
  } catch (e) { toast(e.message); btn.disabled = false; }
  t.editOpening = false;
}
async function saveEdit(btn) {
  const msgEl = btn.closest('.msg'), host = btn.closest('article, .topic'), t = threads.get(hostKey(host)), st = msgEl.querySelector('[data-edit-status]');
  if (!t) return;
  st.textContent = 'saving…';
  try { await api('/api/zulip/edit', { id: +msgEl.dataset.mid, content: msgEl.querySelector('[data-edit]').value, stream: t.stream }); t.edit = null; talkTopics = {}; await refreshThread(host); }
  catch (e) { st.textContent = e.message; }
}
function cancelEdit(btn) {
  const host = btn.closest('article, .topic'), t = threads.get(hostKey(host)), box = threadBox(host);
  if (t && box) { t.edit = null; mountThread(host, t, box.querySelector('[data-reply]')?.value || ''); }
}

/* ------------------------------------------------------------ source pages */

let src = null;
async function openSource(kind, id) {
  view = 'source'; src = null; render();
  $('#srchead').innerHTML = '<span class="meta">fetching…</span>'; $('#srclist').innerHTML = ''; $('#srcmeta').textContent = '';
  location.hash = 'source/' + kind + '/' + encodeURIComponent(id);
  try { src = await api('/api/source?' + new URLSearchParams({ kind, id })); renderSource(); }
  catch (e) { $('#srchead').innerHTML = `<span class="warn">${esc(e.message)}</span>`; }
}
function renderSource() {
  const s = src.sub, f = src.followed, k = s.kind;
  const av = s.avatar ? `<img class="av" src="/img?u=${encodeURIComponent(s.avatar)}" alt="">` : `<div class="av"></div>`;
  const handle = k === 'tw' ? '@' + s.id : k === 'rss' ? (s.site || '').replace(/^https?:\/\//, '') : k === 'arxiv' ? (s.qtype || '') : (s.handle || '');
  const fields = f ? `<div class="fields">
      <label>tags <input class="tags" data-k="tags" value="${esc((s.tags || []).join(', '))}" placeholder="math, cooking"></label>
      ${k === 'yt' ? `<label>min <input class="mm" data-k="min_minutes" type="number" min="0" value="${s.min_minutes ?? ''}" placeholder="–"> min</label>` : ''}
      <label>why <input class="note" data-k="note" value="${esc(s.note || '')}" placeholder="why do I follow this?"></label></div>` : '';
  $('#srchead').innerHTML = `<div class="srchead person" data-kind="${k}" data-id="${esc(s.id)}">${av}<div>
    <h2>${esc(s.name || handle)} <span class="meta">${esc(handle)} · ${KIND_LABEL[k]}</span></h2>
    ${s.error ? `<div class="warn">last fetch failed: ${esc(s.error)}</div>` : ''}
    <div class="row">${f ? `<button class="link" data-a="mute">${s.muted ? 'unmute' : 'mute'}</button><button class="link" data-a="unfollow">unfollow</button>` : `<button class="solid" data-a="src-follow">follow</button>`}
      ${s.muted ? '<span class="meta">muted: followed, but hidden from the feed</span>' : ''}</div>${fields}</div></div>`;
  $('#srcmeta').textContent = `${src.count} item${src.count === 1 ? '' : 's'}${f ? '' : ' · not followed'}`;
  $('#srclist').innerHTML = src.items.length ? src.items.map(article).join('') : '<div class="empty">nothing fetched yet</div>';
}

function kindChips(active, counts, extraTags, activeTag, prefix) {
  const chips = [`<button data-${prefix}f="all" class="${active === 'all' ? 'on' : ''}">all</button>`];
  for (const k of ['yt', 'tw', 'rss', 'arxiv', 'ig']) if (counts[k]) chips.push(`<button data-${prefix}f="${k}" class="${active === k ? 'on' : ''}">${KIND_LABEL[k]}</button>`);
  chips.push(...extraTags.map(t => `<button data-${prefix}tag="${esc(t)}" class="tag ${activeTag === t ? 'on' : ''}">${esc(t)}</button>`));
  return chips.join('');
}

function renderFeed() {
  const s = data.settings;
  $('#when').textContent = data.refreshing ? 'refreshing…' : data.last_refresh ? 'updated ' + ago(data.last_refresh) + ' ago' : 'loading…';
  const inboxChip = (data.inbox.length || kindFilter === 'inbox') ? `<button data-f="inbox" class="${kindFilter === 'inbox' ? 'on' : ''}">inbox${data.inbox.length ? ' · ' + data.inbox.length : ''}</button>` : '';
  const recCards = (data.recs && data.recs.cards) || [];
  const recChip = (recCards.length || kindFilter === 'recs') ? `<button data-f="recs" class="${kindFilter === 'recs' ? 'on' : ''}">from friends${recCards.length ? ' · ' + recCards.length : ''}</button>` : '';
  $('#chips').innerHTML = kindChips(kindFilter, data.counts, data.tags, tagFilter, '') + inboxChip + recChip;
  if (kindFilter === 'recs') {
    $('#papers').hidden = true; $('#errs').innerHTML = '';
    $('#list').innerHTML = recCards.length ? recCards.map(c => {
      const it = { ...c.item, cid: c.item.cid || 'rec' };
      const lines = c.by.map(b => `<div class="rec"><b>${esc(b.who)}</b>${c.for_all ? ' recommends this to everyone' : ' recommends this to you'}${b.note ? ': ' + esc(b.note) : ''} · ${ago(b.ts)} ago · ${b.thanked ? '<span>thanked</span>' : `<button data-a="thanks" data-msg="${b.msg_id}">thanks</button>`}</div>`).join('');
      const disc = `<div class="disc"><button data-a="disc" data-topic="${esc(c.topic)}" data-stream="${esc(c.stream)}">${threads.has('id:' + it.id) ? 'close' : 'open'} discussion</button></div>`;
      return article(it).replace(/<div class="(rec|disc)">[\s\S]*?<\/div>(?=<\/div><\/article>)/g, '').replace('</div></article>', lines + disc + '</div></article>');
    }).join('') : '<div class="empty">nothing recommended to you yet. when a friend uses “recommend” on something, it lands here with their note.</div>';
    return;
  }
  if (kindFilter === 'inbox') {
    $('#papers').hidden = true; $('#errs').innerHTML = '';
    $('#list').innerHTML = data.inbox.length ? data.inbox.map(i => article(i).replace('</article>', inboxRow(i) + '</article>')).join('')
      : '<div class="empty">nothing here. paste a link with <b>+ link</b>, or turn on the clipboard watcher in settings and copy one from WhatsApp.</div>';
    return;
  }
  $('#errs').innerHTML = data.errors.length
    ? `<details class="err"><summary>${data.errors.length} source${data.errors.length > 1 ? 's' : ''} could not be fetched</summary>${esc(data.errors.join('\n'))}</details>` : '';
  const papers = kindFilter === 'arxiv';
  $('#papers').hidden = !papers;
  document.querySelectorAll('#windows button').forEach(b => b.classList.toggle('on', +b.dataset.w === paperWindow));
  document.querySelectorAll('#ptypes button').forEach(b => b.classList.toggle('on', !!paperTypes[b.dataset.pt]));
  $('#ax_clear').hidden = !axResults;
  const L = $('#list');
  if (papers && axResults) {
    L.innerHTML = axResults.length ? `<div class="day">${axResults.length} results from arXiv</div>` + axResults.map(article).join('') : '<div class="empty">arXiv found nothing for that</div>';
    return;
  }
  if (ytResults) {  // a deliberate search on YouTube: a flat list, nothing around it
    L.innerHTML = `<div class="day">from YouTube · ${esc(feedQuery)} · <button class="link" data-a="yt-clear">back to your feed</button></div>`
      + (ytResults.length ? ytResults.map(article).join('') : '<div class="empty">YouTube found nothing for that</div>');
    return;
  }
  const q = feedQuery.toLowerCase();
  const hit = i => !q || [i.title, i.author, i.desc, ...(i.authors || []), ...(i.tags || [])].some(x => (x || '').toLowerCase().includes(q));
  let items = data.items.filter(i => (kindFilter === 'all' || i.kind === kindFilter) && (!tagFilter || i.tags.includes(tagFilter)) && !(s.hide_seen && seen.has(i.id)) && hit(i)
    && (i.kind !== 'arxiv' || !i.announce_type || paperTypes[i.announce_type]));
  let listing = null;
  if (papers && paperWindow === 1) {
    // "today's listing": the most recent announcement day among the papers you have (Friday's on a weekend).
    // A paper from an author or keyword watch is placed on the listing its submission time implies.
    listing = items.reduce((m, i) => listDay(i) > m ? listDay(i) : m, '');
    items = items.filter(i => listDay(i) === listing);
  } else if (papers && paperWindow) {
    const cut = Date.now() / 1000 - paperWindow * 86400;
    items = items.filter(i => i.ts >= cut);
  }
  if (papers) {  // within a day: papers matching a keyword watch first, then as arXiv lists them (new, cross-lists, replacements); chronological otherwise
    items = items.map((i, n) => ({ i, n, d: listDay(i), kw: kwHits(i).length ? 0 : 1, r: ANNOUNCE_RANK[i.announce_type] ?? 0 }))
      .sort((a, b) => a.d !== b.d ? (a.d > b.d ? -1 : 1) : (a.kw - b.kw || a.r - b.r || a.n - b.n)).map(x => x.i);
  }
  const ytOffer = q && (kindFilter === 'all' || kindFilter === 'yt') ? `<div class="offer meta">not in what you follow? <button class="link" data-a="yt-search">search YouTube for “${esc(feedQuery)}”</button></div>` : '';
  if (!items.length) {
    const n = Object.values(data.counts).reduce((a, b) => a + b, 0);
    L.innerHTML = `<div class="empty">${q ? 'nothing you follow matches' : n ? (data.last_refresh ? (papers && paperWindow ? 'no papers in this window' : papers ? 'no papers yet — follow a category or author in <b>following</b>' : 'nothing here') : 'fetching your sources…') : 'you are not following anything yet — open <b>following</b> to import or search'}</div>` + ytOffer;
    return;
  }
  let html = '', day = null, dayPapers = [];
  const flushPapers = () => { if (dayPapers.length) { html += digest(dayPapers); dayPapers = []; } };
  for (const i of items) {
    const d = papers ? listingLabel(listDay(i)) : dayLabel(i.ts);
    if (d !== day) { flushPapers(); html += `<div class="day">${papers ? 'listing of ' : ''}${esc(d)}</div>`; day = d; }
    if (kindFilter === 'all' && i.kind === 'arxiv') { dayPapers.push(i); continue; }  // papers fold into one calm line per day in "all"
    html += article(i);
  }
  flushPapers();
  L.innerHTML = html + ytOffer;
}

async function searchYouTube() {
  const L = $('#list'); L.innerHTML = '<div class="empty">asking YouTube…</div>';
  try { ytResults = await api('/api/yt/search?q=' + encodeURIComponent(feedQuery)); render(); }
  catch (e) { ytResults = null; render(); toast(e.message, 5000); }
}

function inboxRow(i) {
  const f = i.follow;
  let follow = '';
  if (f && f.ref) {
    const already = f.kind !== 'arxiv' && (data.followed[f.kind] || []).includes(f.ref);
    follow = already ? `<span class="meta">following ${esc(f.name || f.ref)}</span>`
      : `<button data-a="inbox-follow">follow ${esc(f.name || f.ref)}${f.kind === 'rss' ? ' (feed)' : f.kind === 'arxiv' ? ' (author)' : ''}</button>`;
  }
  const more = f && f.ref && f.kind !== 'arxiv' ? `<button class="faint" data-src="${esc(f.kind)}|${esc(f.ref)}">more from ${esc(f.name || f.ref)}</button>` : '';
  return `<div class="acts inboxacts"><span class="meta">via ${esc(i.via || 'link')} · ${ago(i.added)} ago</span>${follow}${more}<button data-a="inbox-remove" class="faint">remove</button></div>`;
}

function digest(papers) {
  const top = papers.slice(0, 3).map(p => `<span>${esc(p.title)}</span>`).join('');
  const via = [...new Set(papers.flatMap(p => p.via || []))].slice(0, 4).join(', ');
  return `<article class="digest" data-id="digest"><div><p class="t">${papers.length} new paper${papers.length > 1 ? 's' : ''} <span class="cats">${esc(via)}</span></p>
    <div class="desc">${top}</div><div class="acts"><button data-a="papers">open papers</button></div></div></article>`;
}

function srcBtn(i) {
  // the author's name opens the source page: for feed items via the source, for inbox items via the follow suggestion
  const k = i.kind, id = (i.cid && i.cid !== 'inbox') ? i.cid : (i.follow && i.follow.kind === k ? i.follow.ref : null);
  const name = i.author || (i.follow && i.follow.name) || '';
  return id && k !== 'arxiv' ? `<button class="src" data-src="${esc(k)}|${esc(id)}">${esc(name)}</button>` : esc(name);
}
function recLine(i) {
  const by = data.recs && data.recs.by_id && data.recs.by_id[i.id];
  if (!by || !by.length) return '';
  return by.map(b => `<div class="rec"><b>${esc(b.who)}</b> recommends this${b.note ? ': ' + esc(b.note) : ''} · ${ago(b.ts)} ago · ${b.thanked ? '<span>thanked</span>' : `<button data-a="thanks" data-msg="${b.msg_id}">thanks</button>`}</div>`).join('');
}
function discLine(i) {
  const d = data.discussions && data.discussions[i.id];
  if (!d) return recLine(i);
  const open = threads.has('id:' + i.id);
  return recLine(i) + `<div class="disc">${esc(d.people.slice(0, 3).join(', '))}${d.people.length > 3 ? ' and others' : ''} talk about this in #${esc(d.stream || data.zulip.stream)} · ${d.count} message${d.count > 1 ? 's' : ''} · last ${ago(d.last_ts)} ago · <button data-a="disc" data-topic="${esc(d.topic)}" data-stream="${esc(d.stream || '')}">${open ? 'close' : 'open'} discussion</button></div>`;
}
function zulipTopicUrl(topic, stream) {
  const z = data.zulip || {}, enc = s => encodeURIComponent(s).replace(/%/g, '.');
  return `${z.site}/#narrow/stream/${enc(stream || z.stream)}/topic/${enc(topic)}`;
}

function article(i) {
  const sn = seen.has(i.id), sv = saved.has(i.id);
  const tags = (i.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('');
  // primary actions stay visible; the rest fade in when the item is hovered or focused, so the list reads as text
  const saveBtn = `<button data-a="save">${sv ? 'unsave' : 'save'}</button><button data-a="copy" class="sec">copy link</button><button data-a="share" class="sec">share</button>${['arxiv', 'tw', 'rss'].includes(i.kind) ? `<button data-a="claude" class="${i.kind === 'arxiv' ? '' : 'sec'}" title="a new conversation on claude.ai with this in the prompt">take to claude</button>` : ''}`;
  const seenBtn = `<button data-a="seen" class="sec">${sn ? 'unmark' : 'mark seen'}</button>`;
  const cls = `${i.kind}${sn ? ' seen' : ''}`;
  if (i.kind === 'yt') {
    const nothumb = data.settings.hide_thumbnails;
    return `<article class="${cls}${nothumb ? ' nothumb' : ''}" data-id="${esc(i.id)}">${nothumb ? '' : `<img loading="lazy" src="/thumb/${esc(i.id)}" alt="">`}
      <div><p class="t">${esc(calm(i.title))}</p>
      <div class="by">${srcBtn(i)} · ${i.dur != null ? dur(i.dur) + ' · ' : ''}${ago(i.ts)} ago${i.short ? ' · short' : ''}${tags}</div>
      <div class="acts"><button data-a="play">▶ play</button><button data-a="dl" class="sec">↓ download</button>${saveBtn}${seenBtn}</div>${discLine(i)}</div></article>`;
  }
  if (i.kind === 'tw') {
    // a video in a post from someone you follow, or in a post you opened yourself, is pre-approved: show it
    const vid = !!i.video;
    const thumb = vid && i.thumb && !data.settings.hide_thumbnails ? `<img loading="lazy" src="/img?u=${encodeURIComponent(i.thumb)}" alt="">` : '';
    const playBtns = vid ? `<button data-a="playurl">▶ play${i.dur ? ' · ' + dur(i.dur) : ''}</button><button data-a="dlurl" class="sec">↓ download</button>` : '';
    const rt = (i.title || '').match(/^RT by (@\w+): ([\s\S]*)$/), rp = (i.title || '').match(/^R to (@\w+): ([\s\S]*)$/);
    const me = (i.author || '').replace(/^@/, '').toLowerCase();
    // the post being answered, or quoted, sits right above the post: a reply without its context is half a sentence
    const ctx = (c, label) => c ? `<div class="ctx"><span class="meta">${label} <b>${esc(c.name || '@' + c.user)}</b> @${esc(c.user)}${c.media ? ' · has media' : ''} · <a href="${esc(c.link)}" target="_blank" rel="noopener">${c.ts ? ago(c.ts) + ' ago' : 'open ↗'}</a> · <button class="link" data-a="twgo" data-url="${esc(c.link)}">see it</button></span><div class="ctxt">${esc(c.text)}</div></div>` : '';
    const context = ctx(i.parent, i.parent && i.parent.user === me ? 'continuing their thread:' : 'replying to') + ctx(i.quote, 'quoting');
    const text = rt ? `<span class="rt">reposted ${esc(rt[1])}</span>${esc(rt[2])}` : rp ? (i.parent ? esc(rp[2]) : `<span class="rt">replying to ${esc(rp[1])}</span>${esc(rp[2])}`) : esc(i.title);
    const threadBtn = `<button data-a="twthread" class="${i.convo || i.reply_to || rp ? '' : 'sec'}">thread</button>`;
    const long = (i.title || '').length > 420;
    return `<article class="${cls}${thumb ? ' withthumb' : ''}" data-id="${esc(i.id)}">${thumb}<div>
      <div class="by">${srcBtn(i)} · ${ago(i.ts)} ago${tags}</div>${context}
      <div class="tweet${long ? ' long' : ''}">${text}</div>${long ? '<button class="more" data-a="more">more</button>' : ''}${photos(i.photos)}
      <div class="acts">${playBtns}${threadBtn}${i.cid === 'inbox' ? `<a href="${esc(i.link)}" target="_blank" rel="noopener" class="faint">open ↗</a>` : ''}${saveBtn}${seenBtn}</div>${discLine(i)}</div></article>`;
  }
  if (i.kind === 'ig') {
    // a reel someone sent you: pre-approved, played in your player, never on Instagram
    const thumb = i.thumb && !data.settings.hide_thumbnails ? `<img loading="lazy" src="/img?u=${encodeURIComponent(i.thumb)}" alt="">` : '';
    const playBtns = i.video ? `<button data-a="playurl">▶ play${i.dur ? ' · ' + dur(i.dur) : ''}</button><button data-a="dlurl" class="sec">↓ download</button>` : '';
    const long = (i.title || '').length > 420;
    return `<article class="${cls}${thumb ? ' withthumb' : ''}" data-id="${esc(i.id)}">${thumb}<div>
      <div class="by"><b>${esc(i.name || i.author || '')}</b>${i.name && i.author ? ' · ' + esc(i.author) : ''} · instagram · ${ago(i.ts)} ago${tags}</div>
      <div class="tweet${long ? ' long' : ''}">${esc(i.title)}</div>${long ? '<button class="more" data-a="more">more</button>' : ''}
      <div class="acts">${playBtns}<a href="${esc(i.link)}" target="_blank" rel="noopener" class="faint">open ↗</a>${saveBtn}${seenBtn}</div>${discLine(i)}</div></article>`;
  }
  if (i.kind === 'rss') {
    return `<article class="${cls}" data-id="${esc(i.id)}"><div>
      <p class="t"><a href="${esc(i.link)}" target="_blank" rel="noopener">${esc(i.title)}</a></p>
      <div class="by">${srcBtn(i)}${i.by && i.by !== i.author ? ' · ' + esc(i.by) : ''} · ${ago(i.ts)} ago${tags}</div>
      ${i.desc ? `<div class="desc">${esc(i.desc)}</div>${i.desc.length > 180 ? '<button class="more" data-a="more">more</button>' : ''}` : ''}
      <div class="acts"><a href="${esc(i.link)}" target="_blank" rel="noopener">open ↗</a>${saveBtn}${seenBtn}</div>${discLine(i)}</div></article>`;
  }
  if (i.kind === 'follow') {
    return `<article class="rss" data-id="${esc(i.id)}"><div>
      <p class="t">${esc(i.title)}</p><div class="by">${esc(i.author || '')}${i.author ? ' · ' : ''}a source, not a single item</div></div></article>`;
  }
  if (i.kind === 'arxiv') {
    const mine = new Set((data.arxiv_authors || []).map(a => a.toLowerCase()));
    const authors = (i.authors || []).map(a => `<button data-author="${esc(a)}" class="${mine.has(a.toLowerCase()) ? 'me' : ''}">${esc(a)}</button>`).join(', ');
    const cats = `<span class="cats"><span class="p">${esc(i.primary)}</span>${(i.cats || []).filter(c => c !== i.primary).slice(0, 3).map(c => ' ' + esc(c)).join('')}</span>`;
    const atype = i.announce_type === 'cross' ? ' · <span class="atype">cross-list</span>' : i.announce_type === 'replace' ? ' · <span class="atype">replacement</span>' : '';
    const replaced = i.replaced ? ` · <span class="atype">replaced ${esc(listingLabel(i.replaced))}</span>` : i.updated && i.updated - i.ts > 86400 ? ' · updated ' + dateStr(i.updated) : '';
    const kw = kwHits(i);
    const insp = inspireCache[i.id];
    return `<article class="${cls}" data-id="${esc(i.id)}"><div>
      <p class="t"><a href="${esc(i.link)}" target="_blank" rel="noopener">${hl(i.title)}</a></p>
      <div class="authors">${authors}</div>
      <div class="by">${dateStr(i.ts)}${atype}${replaced}${cats}${tags}${i.comment ? ` · <span class="cats">${esc(i.comment)}</span>` : ''}<span class="insp" data-insp="${esc(i.id)}">${insp ? inspLine(insp) : ''}</span>${kw.length ? `<span class="kw">${esc(kw.join(', '))}</span>` : ''}</div>
      <div class="abs" hidden>${i.desc ? hl(i.desc) : '<span class="meta">no abstract was fetched for this one; abs ↗ has it</span>'}</div>
      <div class="menu" hidden></div>
      <div class="acts"><button data-a="abs">abstract</button><a href="${esc(i.link)}" target="_blank" rel="noopener">abs ↗</a><a href="${esc(i.pdf)}" target="_blank" rel="noopener">pdf ↗</a>${saveBtn}${seenBtn}<button data-a="citedby" class="sec">cited by</button><button data-a="refs" class="sec">references</button></div>${discLine(i)}</div></article>`;
  }
  return '';
}

async function setMark(id, field, on) {
  const item = findItem(id);
  const set = field === 'saved' ? saved : seen;
  on ? set.add(id) : set.delete(id);
  if (field === 'saved') { if (on && item) savedItems[id] = { ...item, saved_at: Date.now() / 1000 }; else delete savedItems[id]; }
  render();
  const snap = item && snapshot(item);
  try { await api('/api/mark', { id, field, on, item: snap }); }
  catch (e) { toast('could not save: ' + e.message); }
}

/* ------------------------------------------------------------ share to Zulip */

function itemLink(i) { return i.kind === 'yt' ? 'https://www.youtube.com/watch?v=' + i.id : (i.link || i.id); }
function findItem(id) { return data.items.find(i => i.id === id) || (axResults || []).find(i => i.id === id) || (ytResults || []).find(i => i.id === id) || (data.inbox || []).find(i => i.id === id) || ((data.recs && data.recs.cards) || []).map(c => c.item).find(i => i.id === id) || savedItems[id]; }
function snapshot(item) { return Object.fromEntries(Object.entries(item).filter(([k]) => SNAP_KEYS.includes(k))); }

function openShare(art) {
  const old = art.querySelector('.share'); if (old) { old.remove(); return; }
  const it = findItem(art.dataset.id); if (!it) return;
  const z = data.settings.zulip || {};
  const box = document.createElement('div'); box.className = 'share';
  if (!z.site || !z.email || !z.api_key_set) {
    box.innerHTML = `<span class="meta">sharing goes to Zulip, and it is not set up yet — </span><button class="link" data-a="share-settings">open settings</button>`;
  } else {
    const target = (z.streams || {})[it.kind] || z.stream || 'feedel';
    const options = (z.shown_streams || z.all_streams || [target]).map(s => `<option value="${esc(s)}" ${s === target ? 'selected' : ''}>#${esc(s)}</option>`).join('');
    box.innerHTML = `<textarea rows="3" placeholder="why is this worth their time? (LaTeX works: $…$)" data-share="note"></textarea>
      <input data-share="topic" value="${esc((it.title || '').slice(0, 60))}" placeholder="topic">
      <div class="row"><button class="solid" data-a="share-send">post to</button><select data-share="stream">${options}</select>
        <button class="link" data-a="share-rec">recommend to someone instead</button><button class="link" data-a="share-cancel">cancel</button><span class="meta" data-share="status"></span></div>
      <div class="people" hidden><span class="meta">recommend to</span> <label><input type="checkbox" data-share="all" checked> everyone</label><span data-share="people" class="meta">loading friends…</span>
        <button class="solid" data-a="share-send-rec">recommend</button></div>`;
  }
  art.querySelector('.acts').after(box);
  box.querySelector('textarea')?.focus();
}
let people = null;
async function showPeople(box) {
  const row = box.querySelector('.people'); row.hidden = !row.hidden; if (row.hidden) return;
  if (!people) { try { people = await api('/api/zulip/people'); } catch (e) { box.querySelector('[data-share=people]').textContent = e.message; return; } }
  box.querySelector('[data-share=people]').innerHTML = people.map(p => `<label><input type="checkbox" data-share="to" value="${esc(p.name)}"> ${esc(p.name)}</label>`).join('') || 'nobody else here yet';
}
async function sendShare(art, recommend = false) {
  const box = art.querySelector('.share'), it = findItem(art.dataset.id);
  const note = box.querySelector('[data-share=note]').value, topic = box.querySelector('[data-share=topic]').value, st = box.querySelector('[data-share=status]');
  const stream = box.querySelector('[data-share=stream]')?.value || '';
  const to = recommend && !box.querySelector('[data-share=all]').checked ? [...box.querySelectorAll('[data-share=to]:checked')].map(c => c.value) : [];
  if (recommend && !box.querySelector('[data-share=all]').checked && !to.length) { st.textContent = 'pick someone, or everyone'; return; }
  st.textContent = 'posting…';
  try {
    const r = await api('/api/share', { item: snapshot(it), note, topic, stream, recommend, to });
    box.remove(); toast(recommend ? `recommended to ${to.length ? to.join(', ') : 'everyone'} in #${r.stream}` : `shared to #${r.stream} › ${r.topic}`, 4000);
  } catch (e) { st.textContent = e.message; }
}

/* ------------------------------------------------------------ threads of posts on X */

// the photos of a post, through the image proxy (never fetched from X by the browser); click opens the full size
function photos(list) {
  if (!list || !list.length || (data && data.settings && data.settings.hide_thumbnails)) return '';
  return `<div class="photos">${list.map(u => `<a href="/img?u=${encodeURIComponent(u)}" target="_blank" rel="noopener"><img loading="lazy" src="/img?u=${encodeURIComponent(u + (u.includes('?') ? '&' : '?') + 'name=small')}" alt=""></a>`).join('')}</div>`;
}
function twPost(t, cls = '', num = '', tag = '') {
  const vid = t.video ? `<button data-a="tw-play" data-url="${esc(t.mp4 || t.link)}">▶ play${t.dur ? ' · ' + dur(t.dur) : ''}</button>` : (t.media && !(t.photos || []).length ? '<span class="meta">has media · </span>' : '');
  const go = cls === 'cur' ? '' : `<button data-a="twgo" data-url="${esc(t.link)}" class="sec">open</button>`;
  return `<div class="twpost ${cls}"><div class="who">${num ? `<span class="n">${num}</span> ` : ''}<b>${esc(t.name || '@' + t.user)}</b> <span class="meta">@${esc(t.user)} · <a href="${esc(t.link)}" target="_blank" rel="noopener">${ago(t.ts)} ago</a>${tag ? ' · ' + tag : ''}</span></div>
    <div class="tweet">${esc(t.text)}</div>${photos(t.photos)}${vid || go ? `<div class="acts">${vid}${go}</div>` : ''}</div>`;
}
const twOpen = new Map();  // article id → {stack, cur}: open X thread boxes survive a re-render of the list
async function openTwThread(art, url, push, restore) {
  let box = art.querySelector('.twthread');
  if (box && !url) { box.remove(); twOpen.delete(art.dataset.id); return; }
  const it = findItem(art.dataset.id); if (!it) return;
  const target = url || it.link || it.id;
  if (!box) { box = document.createElement('div'); box.className = 'twthread'; box._stack = url ? [it.link || it.id] : []; art.querySelector(':scope > div > .acts, :scope > .acts').after(box); }  // opened from a context block: back returns to this post's own thread
  else if (push && box._cur) box._stack.push(box._cur);
  if (restore) box._stack = restore.slice();
  box._cur = target;
  twOpen.set(art.dataset.id, { stack: box._stack.slice(), cur: target });
  box.innerHTML = '<span class="meta">reading the thread…</span>';
  try {
    const t = await api('/api/tw/thread?url=' + encodeURIComponent(target));
    const me = t.tweet ? t.tweet.user : '';
    const parts = [];
    if (t.before.length) parts.push(`<div class="thsec">in reply to</div>` + t.before.map(p => twPost(p, 'before')).join(''));
    const posts = t.posts || [t.tweet].concat(t.after || []);
    const idx = t.index == null ? 0 : t.index;
    if (posts.length > 1) {
      parts.push(`<div class="thsec">the whole thread · ${posts.length} posts</div>` +
        posts.map((p, k) => twPost(p, k === idx ? 'cur' : 'after', `${k + 1}/`, k === idx ? 'this post' : '')).join(''));
    }
    if (t.replies.length) {
      const others = t.replies.filter(r => r.user !== me);
      if (others.length) parts.push(`<details class="threp"><summary class="meta">${others.length} repl${others.length === 1 ? 'y' : 'ies'} from others</summary>${others.map(p => twPost(p, 'reply')).join('')}</details>`);
    }
    if (!parts.length) parts.push(`<div class="meta">${t.note || 'a single post: nothing before it, no continuation by the author and no replies'}</div>`);
    else if (t.note) parts.push(`<div class="meta">${esc(t.note)}</div>`);
    const back = box._stack.length ? `<button class="link" data-a="twthread-back">← back</button>` : '';
    const here = url ? `<span class="meta">thread of <b>@${esc(me)}</b></span>` : `<span class="meta">thread</span>`;
    box.innerHTML = `<div class="row thhead">${back}${here}<span class="grow"></span><button class="link" data-a="twthread-close">close</button></div>` + parts.join('');
  } catch (e) { box.innerHTML = `<div class="row thhead">${box._stack.length ? '<button class="link" data-a="twthread-back">← back</button>' : ''}<span class="grow"></span><button class="link" data-a="twthread-close">close</button></div><span class="warn">${esc(e.message)}</span>`; }
}

/* ------------------------------------------------------------ INSPIRE-HEP: citations and references of a paper */

// what INSPIRE knows about the papers on screen, by arXiv id, asked for in one request per render and kept for the session
const inspireCache = {}, inspirePending = new Set(), inspireOpen = new Map();  // id → {rel, html} for the boxes left open across re-renders
function inspLine(c) {
  if (!c || !c.found) return '';
  const parts = [];
  if (c.journal) parts.push(esc(c.journal) + (c.year ? ' (' + c.year + ')' : ''));
  if (c.cites) parts.push(c.cites === 1 ? '1 citation' : `${c.cites} citations`);
  return parts.length ? ' · ' + parts.join(' · ') : '';
}
async function fillInspire() {
  const els = [...document.querySelectorAll('section:not([hidden]) article [data-insp]')];
  els.forEach(e => { const c = inspireCache[e.dataset.insp]; if (c) e.innerHTML = inspLine(c); });
  const ids = [...new Set(els.map(e => e.dataset.insp))].filter(id => !(id in inspireCache) && !inspirePending.has(id)).slice(0, 150);
  if (!ids.length) return;
  ids.forEach(id => inspirePending.add(id));
  try {
    Object.assign(inspireCache, await api('/api/inspire', { ids }));
    document.querySelectorAll('[data-insp]').forEach(e => { const c = inspireCache[e.dataset.insp]; if (c) e.innerHTML = inspLine(c); });
  } catch (e) { /* INSPIRE unreachable: the by-lines simply stay as they are */ }
  finally { ids.forEach(id => inspirePending.delete(id)); }
}
function inspList(r) {
  const n = r.papers.length;
  const head = `<div class="row thhead"><span class="meta">${r.rel === 'citedby' ? 'cited by' : 'references'} · ${r.total} on INSPIRE${n < r.total ? `, the newest ${n} shown` : ''}</span><span class="grow"></span><button class="link" data-a="insp-close">close</button></div>`;
  if (!n) return head + `<div class="meta">${r.rel === 'citedby' ? 'nothing cites it yet' : 'INSPIRE has no reference list for it'}</div>`;
  return head + r.papers.map(p => `<div class="insp"><div class="t2"><a href="${esc(p.link)}" target="_blank" rel="noopener">${esc(p.title)}</a></div>
    <div class="meta">${esc(p.authors.slice(0, 3).join(', '))}${p.authors.length > 3 ? ' et al.' : ''}${p.year ? ' · ' + p.year : ''}${p.journal ? ' · ' + esc(p.journal) : ''}${p.cites ? ' · ' + p.cites + (p.cites === 1 ? ' citation' : ' citations') : ''}${p.id ? ` · <a href="https://arxiv.org/abs/${esc(p.id)}" target="_blank" rel="noopener">open on arXiv ↗</a>` : ''}</div></div>`).join('');
}
async function openInspire(art, rel) {
  const id = art.dataset.id, old = art.querySelector('.inspbox');
  if (old && old.dataset.rel === rel) { old.remove(); inspireOpen.delete(id); return; }
  if (old) old.remove();
  const box = document.createElement('div'); box.className = 'inspbox'; box.dataset.rel = rel; box.innerHTML = '<span class="meta">asking INSPIRE…</span>';
  art.querySelector('.acts').after(box);
  try { box.innerHTML = inspList(await api('/api/inspire/related?' + new URLSearchParams({ id, rel }))); inspireOpen.set(id, { rel, html: box.innerHTML }); }
  catch (e) { box.innerHTML = `<div class="row thhead"><span class="warn">${esc(e.message)}</span><span class="grow"></span><button class="link" data-a="insp-close">close</button></div>`; }
}
function restoreInspire() {
  for (const [id, o] of inspireOpen) {
    const art = shownArticles().find(a => a.dataset.id === id);
    if (art && !art.querySelector('.inspbox')) { const box = document.createElement('div'); box.className = 'inspbox'; box.dataset.rel = o.rel; box.innerHTML = o.html; art.querySelector('.acts').after(box); }
  }
}

/* ------------------------------------------------------------ keyboard: j/k move a highlight, the rest acts on it */

function listRoot() { return view === 'feed' ? $('#list') : view === 'library' ? $('#liblist') : view === 'source' ? $('#srclist') : null; }
function shownArticles() { const r = listRoot(); return r ? [...r.querySelectorAll('article[data-id]')] : []; }
function setCompact(on) {
  document.body.classList.toggle('compact', on);
  const b = $('#compact'); if (b) b.textContent = on ? 'full' : 'compact';
  try { localStorage.setItem('compact', on ? '1' : '0'); } catch (e) { /* ignore */ }
}
// x: fold the current item (text clamped, abstract hidden, thread boxes closed), or unfold it when it is folded
function foldCur(art) {
  const open = art.querySelector('.tweet.open, .desc.open, .abs:not([hidden]), .twthread, .thread');
  if (open) {
    art.querySelectorAll('.tweet.open, .desc.open').forEach(t => { t.classList.remove('open'); const m = art.querySelector('button.more'); if (m) m.textContent = 'more'; });
    const abs = art.querySelector('.abs'); if (abs && !abs.hidden) art.querySelector('[data-a=abs]')?.click();
    art.querySelectorAll('.twthread, .thread').forEach(t => { if (t.classList.contains('twthread')) twOpen.delete(art.dataset.id); t.remove(); });
  } else {
    art.querySelector('button.more')?.click();
    const abs = art.querySelector('.abs'); if (abs && abs.hidden) art.querySelector('[data-a=abs]')?.click();
  }
}
function curArticle() { return curId ? shownArticles().find(a => a.dataset.id === curId) || null : null; }
function setCur(art) {
  document.querySelectorAll('article.cur').forEach(a => a.classList.remove('cur'));
  curId = art ? art.dataset.id : null; curIndex = art ? shownArticles().indexOf(art) : -1;
  if (art) { art.classList.add('cur'); art.scrollIntoView({ block: 'nearest' }); }
}
function moveCur(d) {
  const arts = shownArticles(); if (!arts.length) return;
  let n = arts.findIndex(a => a.dataset.id === curId);
  if (n < 0) n = curIndex >= 0 ? Math.min(curIndex, arts.length - 1) - (d > 0 ? 1 : 0) : (d > 0 ? -1 : arts.length);  // the item went away (marked seen and hidden): carry on from its place
  setCur(arts[Math.max(0, Math.min(arts.length - 1, n + d))]);
}
function restoreCur() {  // after a re-render: the same item, or the one now at its place
  if (!curId) return;
  const art = curArticle();
  if (art) art.classList.add('cur');
}
function searchField() { return view === 'feed' ? $('#feedsearch') : view === 'library' ? $('#libsearch') : view === 'talk' ? $('#talkq') : view === 'following' ? $('#subfilter') : null; }
function toggleHelp(on) { const h = $('#help'); h.hidden = on === undefined ? !h.hidden : !on; if (!h.hidden) window.scrollTo(0, 0); }

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
  for (const k of ['hide_thumbnails', 'calm_titles', 'hide_seen', 'hide_shorts', 'watch_clipboard', 'clipboard_any', 'window']) $('#s_' + k).checked = !!s[k];
  $('#s_window_note').textContent = (s.window_libs_missing || []).length ? '— needs pip install pywebview pystray pillow' : (s.in_window ? '' : '— from the next start');
  $('#s_min_minutes').value = s.min_minutes ?? 0;
  $('#s_max_per_channel').value = s.max_per_channel ?? 0;
  $('#s_block_words').value = (s.block_words || []).join('\n');
  $('#s_fallback_browser').value = s.fallback_browser || '';
  $('#s_fallback_auto').textContent = s.fallback_browser ? '' : (s.fallback_browser_auto ? 'currently: ' + s.fallback_browser_auto : 'no browser detected');
  const z = s.zulip || {};
  $('#z_site').value = z.site || ''; $('#z_stream').value = z.stream || 'feedel'; $('#z_email').value = z.email || '';
  $('#z_extra').value = (z.extra_streams || []).join(', ');
  $('#z_key').value = ''; $('#z_key').placeholder = z.api_key_set ? 'saved — leave empty to keep it' : 'Zulip → settings → account → API key';
  for (const k of ['yt', 'arxiv', 'rss', 'tw']) { $('#z_s_' + k).value = (z.streams_set || {})[k] || ''; $('#z_s_' + k).placeholder = z.stream || 'feedel'; }
  $('#z_recs').value = z.recs_stream || '';
  $('#z_status').textContent = '';
  fillInstagram(s.instagram);
  fillClaude();
  fillUpdate(s.update);
}
async function testZulip() {
  await saveSettings(true);
  $('#z_status').textContent = 'testing…';
  try { const r = await api('/api/zulip/test', {}); $('#z_status').textContent = `connected as ${r.name}; sharing to ${r.share_streams.map(s => '#' + s).join(', ')}` + (r.streams.length > r.share_streams.length ? `; talk also shows ${r.streams.filter(s => !r.share_streams.includes(s)).map(s => '#' + s).join(', ')}` : ''); talkTopics = {}; }
  catch (e) { $('#z_status').textContent = e.message; }
}
async function saveSettings(keepOpen) {
  const patch = {};
  for (const k of ['hide_thumbnails', 'calm_titles', 'hide_seen', 'hide_shorts', 'watch_clipboard', 'clipboard_any', 'window']) patch[k] = $('#s_' + k).checked;
  patch.min_minutes = +$('#s_min_minutes').value || 0;
  patch.max_per_channel = +$('#s_max_per_channel').value || 0;
  patch.block_words = $('#s_block_words').value;
  patch.fallback_browser = $('#s_fallback_browser').value;
  patch.auto_update = $('#s_auto_update').checked;
  if ($('#gh_token').value.trim()) patch.github_token = $('#gh_token').value.trim();
  patch.zulip = { site: $('#z_site').value, stream: $('#z_stream').value, email: $('#z_email').value, api_key: $('#z_key').value,
    streams: { yt: $('#z_s_yt').value, arxiv: $('#z_s_arxiv').value, rss: $('#z_s_rss').value, tw: $('#z_s_tw').value }, extra_streams: $('#z_extra').value, recs_stream: $('#z_recs').value };
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
  loadUnfollowed();
}

async function loadUnfollowed() {
  let list; try { list = await api('/api/unfollowed'); } catch (e) { return; }
  $('#unfpanel').hidden = !list.length;
  $('#unfcount').textContent = list.length ? `${list.length}` : '';
  $('#unf').innerHTML = list.map(u => `<div class="person"><div class="av">${{ tw: '𝕏', rss: 'rss', arxiv: 'arXiv' }[u.kind] || 'yt'}</div>
    <div><div class="nm">${esc(u.name || u.ref)}<span class="h">${esc(KIND_LABEL[u.kind] || u.kind)} · unfollowed ${ago(u.removed)} ago</span></div></div>
    <div class="pacts"><button class="link" data-a="unf-follow" data-kind="${esc(u.kind)}" data-ref="${esc(u.ref)}" data-name="${esc(u.name || '')}">follow again</button><button class="link" data-a="unf-forget" data-kind="${esc(u.kind)}" data-ref="${esc(u.ref)}">forget</button></div></div>`).join('');
}

function personRow(s) {
  const av = s.kind === 'yt' && s.avatar ? `<img class="av" src="/avatar/yt/${esc(s.id)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'av',textContent:'yt'}))">`
    : `<div class="av">${{ tw: '𝕏', rss: 'rss', arxiv: 'arXiv' }[s.kind] || 'yt'}</div>`;
  const handle = s.kind === 'tw' ? '@' + s.id : s.kind === 'rss' ? (s.site || '').replace(/^https?:\/\//, '') : s.kind === 'arxiv' ? ({ category: 'category', author: 'author', search: 'keywords' }[s.qtype] || '') : (s.handle || '');
  return `<div class="person${s.muted ? ' muted' : ''}" data-kind="${s.kind}" data-id="${esc(s.id)}">${av}
    <div><div class="nm"><button class="src" data-src="${esc(s.kind)}|${esc(s.id)}">${esc(s.name || handle || s.id)}</button><span class="h">${esc(handle)}</span></div>
    ${s.error ? `<div class="warn">last fetch failed: ${esc(s.error)}</div>` : ''}
    <div class="fields">
      <label>tags <input class="tags" data-k="tags" value="${esc((s.tags || []).join(', '))}" placeholder="math, cooking"></label>
      ${s.kind === 'yt' ? `<label>min <input class="mm" data-k="min_minutes" type="number" min="0" value="${s.min_minutes ?? ''}" placeholder="–"> min</label>` : ''}
      <label>why <input class="note" data-k="note" value="${esc(s.note || '')}" placeholder="why do I follow this?"></label>
    </div></div>
    <div class="pacts"><button class="link" data-a="mute">${s.muted ? 'unmute' : 'mute'}</button><button class="link" data-a="unfollow">unfollow</button></div></div>`;
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
      <div class="pacts">${r.following ? '<span class="meta">following</span>' : `<button class="solid" data-a="follow" data-name="${esc(r.name)}">follow</button>`}</div></div>`).join('');
  } catch (e) { $('#results').innerHTML = `<div class="warn">${esc(e.message)}</div>`; }
}

/* ------------------------------------------------------------ account sync */

let syncTimer = null;
/* ------------------------------------------------------------ friends: getting people into the group */

async function renderFriends() {
  const z = (data && data.settings && data.settings.zulip) || {};
  const connected = !!(z.site && z.email && z.api_key_set);
  $('#fr-state').textContent = connected ? `connecting to ${z.site}…` : 'not connected to any group yet. paste a setup code below, or create a group (bottom).';
  $('#fr-people').innerHTML = '';
  $('#fr-invitepanel').hidden = !connected;
  $('#fr-link').value = z.invite || ''; $('#fr-organizer').value = z.organizer || (connected ? z.email : '');
  $('#fr-subscribe').hidden = !connected; $('#fr-test').hidden = !connected;
  if (z.site && !connected) { showSteps({ site: z.site, invite: z.invite, organizer: z.organizer, realm: '' }); }
  if (!connected) return;
  try {
    const t = await api('/api/zulip/test', {});
    $('#fr-state').innerHTML = `connected as <b>${esc(t.name)}</b> (${esc(t.email)}) on <b>${esc(t.realm || z.site)}</b> · streams: ${t.streams.map(s => '#' + esc(s)).join(', ')}`;
    const people = await api('/api/zulip/people');
    $('#fr-people').innerHTML = people.length ? `<div class="meta" style="margin-top:8px">here with you:</div><div class="people-grid">${people.map(p => `<span class="pill">${esc(p.name)}</span>`).join('')}</div>` : '<div class="meta" style="margin-top:8px">nobody else here yet — invite someone below.</div>';
    const c = await api('/api/zulip/setup_code');
    $('#fr-mycode').value = c.code;
  } catch (e) { $('#fr-state').innerHTML = `<span class="warn">${esc(e.message)}</span>`; }
}
function showSteps(d) {
  $('#fr-steps').hidden = false;
  $('#fr-realm').textContent = d.realm ? `(${d.realm}, ${d.site})` : `(${d.site})`;
  const inv = $('#fr-invite'), no = $('#fr-noinvite');
  if (d.invite) { inv.href = d.invite; inv.hidden = false; no.hidden = true; }
  else { inv.hidden = true; no.hidden = false; $('#fr-ask').href = d.organizer ? `mailto:${d.organizer}?subject=${encodeURIComponent('feedel: can I join the group?')}` : (d.site + '/accounts/go/'); }
  $('#fr-keypage').href = (d.site.startsWith('http') ? d.site : 'https://' + d.site) + '/#settings/account-and-privacy';
}
async function friendsClick(b) {
  const st = id => $(id);
  if (b.id === 'fr-apply') {
    const code = $('#fr-code').value.trim(); if (!code) return;
    try { const d = await api('/api/zulip/apply_setup', { code }); toast(`pointed at ${d.realm || d.site}`); await load(); showSteps(d); $('#fr-code').value = ''; }
    catch (e) { toast(e.message, 5000); }
  }
  if (b.id === 'fr-connect') {
    st('#fr-connect-status').textContent = 'connecting…';
    try {
      await api('/api/settings', { zulip: { email: $('#fr-email').value, api_key: $('#fr-key').value } });
      const t = await api('/api/zulip/test', {});
      const s = await api('/api/zulip/subscribe', {});
      st('#fr-connect-status').textContent = `connected as ${t.name}; subscribed to ${s.streams.map(x => '#' + x).join(', ')}`;
      $('#fr-key').value = ''; await load(); renderFriends();
    } catch (e) { st('#fr-connect-status').textContent = e.message; }
  }
  if (b.id === 'fr-test') { try { const t = await api('/api/zulip/test', {}); toast(`connected as ${t.name} on ${t.realm || t.stream}`); renderFriends(); } catch (e) { toast(e.message, 5000); } }
  if (b.id === 'fr-subscribe') { try { const s = await api('/api/zulip/subscribe', {}); $('#fr-note').textContent = `subscribed: ${s.subscribed.join(', ') || 'nothing new'}${s.already.length ? ' · already: ' + s.already.join(', ') : ''}${s.unauthorized.length ? ' · not allowed: ' + s.unauthorized.join(', ') : ''}`; } catch (e) { $('#fr-note').textContent = e.message; } }
  if (b.id === 'fr-mklink') { st('#fr-invite-status').textContent = 'asking Zulip for a link…'; try { const r = await api('/api/zulip/invite/link', {}); $('#fr-link').value = r.invite; st('#fr-invite-status').textContent = 'invitation link created' + (r.missing_streams.length ? ` (streams not on the Zulip yet: ${r.missing_streams.join(', ')})` : ''); const c = await api('/api/zulip/setup_code'); $('#fr-mycode').value = c.code; } catch (e) { st('#fr-invite-status').textContent = e.message; } }
  if (b.id === 'fr-sendmail') { st('#fr-invite-status').textContent = 'sending…'; try { const r = await api('/api/zulip/invite/email', { emails: $('#fr-emails').value }); st('#fr-invite-status').textContent = `Zulip e-mailed invitations to ${r.sent.join(', ')}. send them the setup code too.`; $('#fr-emails').value = ''; } catch (e) { st('#fr-invite-status').textContent = e.message; } }
  if (b.id === 'fr-copycode') { try { await navigator.clipboard.writeText($('#fr-mycode').value); toast('setup code copied — send it to your friend'); } catch (e) { toast('select the code and copy it'); } }
}
let frTimer = null;
function queueInviteSave() { clearTimeout(frTimer); frTimer = setTimeout(async () => { try { data.settings = await api('/api/settings', { zulip: { invite: $('#fr-link').value, organizer: $('#fr-organizer').value } }); const c = await api('/api/zulip/setup_code'); $('#fr-mycode').value = c.code; } catch (e) { toast(e.message); } }, 600); }

async function loadSync() {
  let s;
  try { s = await api('/api/sync'); } catch (e) { return; }
  const when = x => x.at ? ' · ' + ago(x.at) + ' ago' : '';
  const g = s.google;
  $('#gstatus').textContent = !g.configured ? 'needs client_secret.json next to feedel (see README)'
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
  if (r.unfollowed?.length) lines.push(`left alone because you unfollowed them before (${r.unfollowed.length}): ` + r.unfollowed.slice(0, 30).join(', ') + (r.unfollowed.length > 30 ? ' …' : '') + ' — see "previously unfollowed" below to follow again');
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

/* ------------------------------------------------------------ messages (notes from Claude, direct messages from friends) */

let msgData = null, msgArchived = false, msgPeople = null;
const msgFolded = new Set(); try { JSON.parse(localStorage.getItem('msgfold') || '[]').forEach(i => msgFolded.add(i)); } catch (e) { /* ignore */ }
function saveFold() { try { localStorage.setItem('msgfold', JSON.stringify([...msgFolded])); } catch (e) { /* ignore */ } }
function foldMsg(card, on) {
  const id = +card.dataset.mid; if (on) msgFolded.add(id); else msgFolded.delete(id); saveFold();
  card.classList.toggle('folded', on); const c = card.querySelector(':scope > .who .chev'); if (c) c.textContent = on ? '▸' : '▾';
}

// a small Markdown renderer for Claude's notes: headings, emphasis, code, links, lists, task lists, quotes, paragraphs
function md(src) {
  const lines = String(src || '').replace(/\r/g, '').split('\n');
  const out = []; let para = [], list = null, code = null, quote = [], n = 0;
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*\w])\*([^*]+)\*/g, '$1<i>$2</i>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  const flushP = () => { if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; } };
  const flushL = () => { if (list) { out.push(`<${list.tag}>${list.items.join('')}</${list.tag}>`); list = null; } };
  const flushQ = () => { if (quote.length) { out.push(`<blockquote>${inline(quote.join(' '))}</blockquote>`); quote = []; } };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (code !== null) { if (/^```/.test(line)) { out.push(`<pre>${esc(code.join('\n'))}</pre>`); code = null; } else code.push(raw); n++; continue; }
    if (/^```/.test(line)) { flushP(); flushL(); flushQ(); code = []; n++; continue; }
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flushP(); flushL(); flushQ(); out.push(`<h${m[1].length + 2}>${inline(m[2])}</h${m[1].length + 2}>`); }
    else if ((m = line.match(/^\s*[-*+]\s+\[([ xX])\]\s+(.*)$/))) {
      flushP(); flushQ(); if (!list || list.tag !== 'ul') { flushL(); list = { tag: 'ul', items: [] }; }
      const on = m[1] !== ' ';
      list.items.push(`<li class="task${on ? ' done' : ''}"><label><input type="checkbox" data-line="${n}"${on ? ' checked' : ''}> <span>${inline(m[2])}</span></label></li>`);
    }
    else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) { flushP(); flushQ(); if (!list || list.tag !== 'ul') { flushL(); list = { tag: 'ul', items: [] }; } list.items.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushP(); flushQ(); if (!list || list.tag !== 'ol') { flushL(); list = { tag: 'ol', items: [] }; } list.items.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^>\s?(.*)$/))) { flushP(); flushL(); quote.push(m[1]); }
    else if (!line.trim()) { flushP(); flushL(); flushQ(); }
    else { flushL(); flushQ(); para.push(line); }
    n++;
  }
  flushP(); flushL(); flushQ();
  if (code !== null) out.push(`<pre>${esc(code.join('\n'))}</pre>`);
  return out.join('');
}

function msgFrom(m) {
  if (m.channel === 'claude') return '<span class="from claude">✦ Claude</span>';
  if (m.channel === 'ig') return `<span class="from">${esc(m.sender || 'an Instagram account')}</span> <span class="meta">· instagram</span>`;
  if (m.channel === 'me') { const to = (m.meta && m.meta.to || []).map(p => p.name || p.email).filter(Boolean).join(', '); return `<span class="from">you${to ? ' → ' + esc(to) : ''}</span>`; }
  return `<span class="from">${esc(m.sender || 'a friend')}</span>`;
}

function msgCard(m) {
  const body = (m.meta && m.meta.html) ? `<div class="mbody html">${m.body}</div>` : `<div class="mbody">${md(m.body)}</div>`;
  const links = (m.links || []).length ? `<div class="mlinks">${m.links.map(l => `<span class="mlink"><a href="${esc(l.url)}" target="_blank" rel="noopener">${l.kind ? `<span class="k">${esc(l.kind)}</span> ` : ''}${esc(l.title || l.url.replace(/^https?:\/\//, '').slice(0, 60))}</a><button data-a="msg-inbox" data-url="${esc(l.url)}" title="put it in your inbox, where it can be played">→ inbox</button></span>`).join('')}</div>` : '';
  const canReply = m.channel !== 'me';
  const acts = `<div class="acts">${canReply ? '<button data-a="msg-reply">reply</button>' : ''}${m.channel !== 'me' ? `<button data-a="msg-read">${m.read ? 'mark unread' : 'mark read'}</button>` : ''}<button data-a="msg-archive" class="faint">${m.archived ? 'unarchive' : 'archive'}</button></div>`;
  const folded = msgFolded.has(m.id);
  const preview = (m.meta && m.meta.html) ? (m.body || '').replace(/<[^>]+>/g, ' ') : (m.body || '');
  return `<div class="msgcard${m.read || m.channel === 'me' ? '' : ' unread'}${m.channel === 'me' ? ' mine' : ''}${folded ? ' folded' : ''}" data-mid="${m.id}">
    <div class="who" data-a="msg-fold" title="fold / unfold"><span class="chev">${folded ? '▸' : '▾'}</span>${msgFrom(m)}${m.title ? ` <b class="mtitle">${esc(m.title)}</b>` : ''}<span class="grow"></span><span class="meta">${ago(m.ts)} ago</span></div>
    <div class="mpreview">${esc(preview.replace(/\s+/g, ' ').trim().slice(0, 140))}</div>
    <div class="mfold">${body}${links}${acts}</div></div>`;
}

async function renderMessages() {
  const list = $('#msglist');
  $('#msg-archived').textContent = msgArchived ? 'hide archived' : 'show archived';
  try {
    msgData = await api('/api/messages?refresh=1' + (msgArchived ? '&archived=1' : ''));
  } catch (e) { list.innerHTML = `<span class="warn">${esc(e.message)}</span>`; return; }
  const z = msgData.zulip, ig = msgData.instagram || {};
  $('#msg-compose').hidden = !z;
  $('#msg-state').innerHTML = (z ? `direct messages on Zulip land here${msgData.dm_error ? ` <span class="warn">(last check failed: ${esc(msgData.dm_error)})</span>` : ''}; ` : 'connect a group in the friends tab and your friends\' direct messages land here too; ') +
    (ig.on ? `Instagram direct messages of @${esc(ig.username)} too${ig.error ? ` <span class="warn">(last check failed: ${esc(ig.error)})</span>` : ''}; ` : '') +
    'Claude leaves notes here when you connect it (settings → Claude).';
  setMsgCount(msgData.unread);
  if (!msgData.messages.length) { list.innerHTML = '<p class="meta">nothing here yet.</p>'; return; }
  // replies sit under what they answer; the rest newest first
  const byId = {}, roots = [];
  msgData.messages.forEach(m => { byId[m.id] = m; m.children = []; });
  msgData.messages.forEach(m => { if (m.reply_to && byId[m.reply_to]) byId[m.reply_to].children.push(m); else roots.push(m); });
  const tree = m => msgCard(m) + (m.children.length ? `<div class="mreplies">${m.children.sort((a, b) => a.ts - b.ts).map(tree).join('')}</div>` : '');
  list.innerHTML = roots.map(tree).join('');
}

// the settings panel shows the MCP address of this very instance (the port can differ from the default)
(function mcpAddress() { const u = location.origin + '/mcp'; const e = $('#mcp_url'), c = $('#mcp_cmd'); if (e) e.textContent = u; if (c) c.textContent = 'claude mcp add --transport http feedel ' + u; })();
function showUpdate(u) {
  const n = $('#update'); if (!n) return;
  if (!u || !u.available) { n.hidden = true; return; }
  const r = u.remote || {}, when = r.date ? ' · ' + new Date(r.date).toLocaleString() : '';
  $('#update-text').textContent = u.ready ? `a newer feedel is ready (${r.short}${when}): it goes in at the next start, or now.`
    : u.mode === 'git' ? `main moved on GitHub (${r.short}${when}): pull and restart to use it.`
    : u.mode === 'exe' ? `a newer feedel is on GitHub (${r.short}${when}).` : `a newer feedel is on GitHub (${r.short}${when}), but this copy cannot update itself.`;
  $('#update-go').textContent = u.ready ? 'restart with it' : u.mode === 'git' ? 'pull and restart' : u.mode === 'exe' ? 'download and restart' : 'ok';
  $('#update-go').hidden = u.mode === 'none';
  n.hidden = false;
}
function fillUpdate(u) {
  u = u || {}; const st = $('#upd_status'); if (!st) return;
  const l = u.local || {}, r = u.remote || {};
  st.textContent = `this copy: ${l.short || 'unknown build'}${l.date ? ' (' + new Date(l.date).toLocaleDateString() + ')' : ''}` +
    (r.short ? ` · GitHub: ${r.short}${r.date ? ' (' + new Date(r.date).toLocaleDateString() + ')' : ''}${u.available ? (u.ready ? ' · downloaded, goes in at the next start' : ' · newer') : ' · same'}` : '') +
    (u.error ? ` · ${u.error}` : '');
  $('#s_auto_update').checked = u.auto !== false && (data.settings.auto_update !== false);
  $('#gh_token_state').textContent = u.token_set ? 'a token is saved; leave the field empty to keep it' : (u.mode === 'exe' || u.mode === 'git') ? '' : 'this copy cannot update itself';
}
async function updateClick(b) {
  if (b.id === 'upd_check') { b.disabled = true; try { const u = await api('/api/update?check=1'); fillUpdate(u); showUpdate(u); } catch (e) { toast(e.message); } finally { b.disabled = false; } return; }
  if (b.id === 'update-go') {
    b.disabled = true; $('#update-status').textContent = 'working…';
    try { const r = await api('/api/update/apply', {}); $('#update-status').textContent = r.restarting ? 'restarting… the page reloads when it is back' : 'done'; if (r.restarting) setTimeout(() => location.reload(), 6000); }
    catch (e) { $('#update-status').textContent = e.message; b.disabled = false; }
  }
}
let claudeSt = null;
async function fillClaude() {
  const st = $('#cl_state'); if (!st) return;
  try { claudeSt = await api('/api/claude/status'); } catch (e) { st.textContent = e.message; return; }
  const c = claudeSt;
  st.innerHTML = (c.cli ? '' : '<span class="warn">Claude Code is not installed on this computer · </span>') +
    (c.mcp_installed ? 'connected: Claude Code knows this feedel as <b>feedel</b>' : c.mcp_other_url ? `registered with another address (${esc(c.mcp_other_url)}) · ` : 'not connected · ');
  $('#cl_mcp').textContent = c.mcp_installed ? 'reconnect' : 'connect'; $('#cl_mcp_off').hidden = !c.mcp_installed;
  const b = c.briefing || {};
  $('#br_on').checked = !!b.on; $('#br_time').value = b.time || '08:00'; $('#br_days').value = b.days || 'daily';
  $('#br_name').value = b.name || ''; $('#br_language').value = b.language || ''; $('#br_profile').value = b.profile || ''; $('#br_extra').value = b.extra || '';
  const r = c.last_run;
  $('#br_last').innerHTML = r ? `last run ${ago(r.at)} ago (${esc(r.trigger)}): ${r.ok ? 'ok' : `<span class="warn">${esc((r.output || 'failed').slice(0, 160))}</span>`}` : (b.on ? 'not run yet' : '');
  $('#br_task').textContent = c.task_file ? 'update the Claude Code scheduled task' : 'also install it as a Claude Code scheduled task';
}
async function claudeClick(b) {
  const patch = () => ({ on: $('#br_on').checked, time: $('#br_time').value, days: $('#br_days').value, name: $('#br_name').value, language: $('#br_language').value, profile: $('#br_profile').value, extra: $('#br_extra').value });
  try {
    if (b.id === 'cl_mcp') { b.disabled = true; const r = await api('/api/claude/mcp', { on: true }); toast(r.installed ? `connected (${r.how})` : 'could not confirm the connection: ' + (r.note || '')); }
    if (b.id === 'cl_mcp_off') { b.disabled = true; await api('/api/claude/mcp', { on: false }); toast('disconnected'); }
    if (b.id === 'br_save') { await api('/api/claude/briefing', patch()); toast('briefing saved'); }
    if (b.id === 'br_run') { await api('/api/claude/briefing', patch()); await api('/api/claude/briefing/run', {}); toast('running… it takes a minute or two; the note lands in messages', 6000); setTimeout(fillClaude, 90000); }
    if (b.id === 'br_prompt') { await api('/api/claude/briefing', patch()); const t = await api('/api/claude/briefing/prompt'); let box = $('#br_prompt_box'); if (!box) { box = document.createElement('pre'); box.id = 'br_prompt_box'; box.className = 'cmd'; b.closest('.row').after(box); } box.textContent = t; }
    if (b.id === 'br_task') { await api('/api/claude/briefing', patch()); const r = await api('/api/claude/briefing/task', {}); toast('written: ' + r.path + ' · set its time under Scheduled in Claude Code', 8000); }
  } catch (e) { toast(e.message, 6000); }
  finally { b.disabled = false; await fillClaude(); }
}
function fillInstagram(ig) {
  ig = ig || {};
  const st = $('#ig_status'); if (!st) return;
  st.textContent = ig.on ? `connected as @${ig.username}${ig.expires ? ' · token until ' + new Date(ig.expires * 1000).toLocaleDateString() : ''}` : 'not connected';
  $('#ig_disconnect').hidden = !ig.on; $('#ig_connect').textContent = ig.on ? 'replace token' : 'connect';
}
async function igClick(b) {
  if (b.id === 'ig_connect') {
    const token = $('#ig_token').value.trim(); if (!token) { toast('paste the token first'); return; }
    b.disabled = true; $('#ig_status').textContent = 'checking with Meta…';
    try { const r = await api('/api/ig/connect', { token }); $('#ig_token').value = ''; toast(`connected as @${r.username}${r.new ? ` · ${r.new} new message${r.new === 1 ? '' : 's'}` : ''}`); await load(); fillInstagram((data.settings || {}).instagram); if (r.error) $('#ig_status').textContent += ' · ' + r.error; }
    catch (e) { $('#ig_status').textContent = e.message; }
    finally { b.disabled = false; }
  }
  if (b.id === 'ig_disconnect') { await api('/api/ig/disconnect', {}); await load(); fillInstagram((data.settings || {}).instagram); }
}
function setMsgCount(n) { const e = $('#msgcnt'); if (e) e.textContent = n ? ` ${n}` : ''; }

async function msgClick(b) {
  const card = b.closest('.msgcard'); const mid = card ? +card.dataset.mid : null;
  const m = mid && msgData ? msgData.messages.find(x => x.id === mid) : null;
  if (b.id === 'msg-refresh') { b.disabled = true; try { const r = await api('/api/messages/refresh', {}); if (r.error) toast(r.error, 5000); } finally { b.disabled = false; } await renderMessages(); return; }
  if (b.id === 'msg-archived') { msgArchived = !msgArchived; await renderMessages(); return; }
  if (b.id === 'msg-foldall') { const cards = [...document.querySelectorAll('.msgcard')]; const on = cards.some(c => !c.classList.contains('folded')); cards.forEach(c => foldMsg(c, on)); b.textContent = on ? 'unfold all' : 'fold all'; return; }
  if (b.id === 'msg-compose') {
    $('#msg-composer').hidden = false; $('#msg-text').focus();
    if (!msgPeople) { try { msgPeople = await api('/api/zulip/people'); } catch (e) { toast(e.message); msgPeople = []; } }
    $('#msg-to').innerHTML = msgPeople.length ? msgPeople.map(p => `<option value="${esc(p.email)}">${esc(p.name)}</option>`).join('') : '<option value="">nobody else in the group yet</option>';
    return;
  }
  if (b.id === 'msg-cancel') { $('#msg-composer').hidden = true; return; }
  if (b.id === 'msg-send') {
    const to = $('#msg-to').value, text = $('#msg-text').value.trim();
    if (!to || !text) { toast(to ? 'write something first' : 'choose whom to write to'); return; }
    b.disabled = true;
    try { await api('/api/messages/send', { text, to: [to] }); $('#msg-text').value = ''; $('#msg-composer').hidden = true; toast('sent'); await renderMessages(); }
    catch (e) { toast(e.message, 5000); } finally { b.disabled = false; }
    return;
  }
  if (b.dataset.a === 'msg-inbox') { try { await api('/api/inbox', { url: b.dataset.url, via: 'message' }); toast('in your inbox'); } catch (e) { toast(e.message); } return; }
  if (!m) return;
  if (b.dataset.a === 'msg-read') { await api('/api/messages/update', { id: mid, read: !m.read }); await renderMessages(); return; }
  if (b.dataset.a === 'msg-archive') { await api('/api/messages/update', { id: mid, archived: !m.archived }); await renderMessages(); return; }
  if (b.dataset.a === 'msg-reply') {
    let box = card.querySelector('.mreply');
    if (box) { box.remove(); return; }
    box = document.createElement('div'); box.className = 'mreply';
    const who = m.channel === 'claude' ? 'Claude reads this the next time it looks at your messages' : m.channel === 'ig' ? `a direct message to ${esc(m.sender || 'them')} on Instagram (Meta allows it within 24 h of their last message)` : `a direct message to ${esc(m.sender || 'them')} on Zulip`;
    box.innerHTML = `<textarea rows="3" placeholder="${who}"></textarea><div class="row"><button class="solid" data-a="msg-reply-send">send</button><span class="meta">${who}</span></div>`;
    card.appendChild(box); box.querySelector('textarea').focus();
    return;
  }
  if (b.dataset.a === 'msg-reply-send') {
    const ta = card.querySelector('.mreply textarea'); const text = ta.value.trim();
    if (!text) return;
    b.disabled = true;
    try { await api('/api/messages/send', { text, reply_to: mid }); if (!m.read) await api('/api/messages/update', { id: mid, read: true }); await renderMessages(); }
    catch (e) { toast(e.message, 5000); b.disabled = false; }
  }
}

// ticking a task in one of Claude's notes rewrites that line of the note
document.addEventListener('change', async e => {
  const cb = e.target;
  if (!(cb instanceof HTMLInputElement) || cb.type !== 'checkbox' || cb.dataset.line === undefined) return;
  const card = cb.closest('.msgcard'); if (!card || !msgData) return;
  const m = msgData.messages.find(x => x.id === +card.dataset.mid); if (!m) return;
  const lines = m.body.replace(/\r/g, '').split('\n'); const i = +cb.dataset.line;
  lines[i] = cb.checked ? lines[i].replace(/\[ \]/, '[x]') : lines[i].replace(/\[[xX]\]/, '[ ]');
  try { const u = await api('/api/messages/update', { id: m.id, body: lines.join('\n') }); m.body = u.body; cb.closest('li').classList.toggle('done', cb.checked); }
  catch (err) { toast(err.message); cb.checked = !cb.checked; }
});

/* ------------------------------------------------------------ "take to claude": the item as the opening prompt of a new conversation */

function claudePrompt(i) {
  const L = [];
  const ctx = (c, what) => c ? [`${what} ${c.name || '@' + c.user} (@${c.user}): "${c.text}"`] : [];
  if (i.kind === 'arxiv') {
    L.push("I'd like to discuss this paper from my arXiv feed.", '', `Title: ${i.title}`, `Authors: ${(i.authors || []).join(', ')}`, `arXiv: ${i.link}`);
    if (i.primary) L.push(`Categories: ${[i.primary].concat((i.cats || []).filter(c => c !== i.primary)).join(', ')}`);
    if (i.desc) L.push('', 'Abstract:', i.desc);
    L.push('', "Read the paper if you can reach it rather than working from the abstract alone. Start with a short map: the main claim, the method, what is new relative to the obvious prior work, and the two or three things you'd want checked before trusting it. Then let's talk; I'll say what I want to dig into. Answer in the language I write in.");
  } else if (i.kind === 'tw') {
    const text = (i.title || '').replace(/^RT by @\w+: /, '').replace(/^R to @\w+: /, '');
    L.push(`A post on X by ${i.author} that I'd like to think through with you (${i.link}):`, '', `"${text}"`);
    L.push(...ctx(i.parent, i.parent && i.parent.user === (i.author || '').replace(/^@/, '').toLowerCase() ? 'Earlier post in the same thread by' : 'It replies to'));
    L.push(...ctx(i.quote, 'It quotes'));
    L.push('', "What is the claim, what is it reacting to, and what would I need to check before taking it at face value? Keep it short, then let's discuss. Answer in the language I write in.");
  } else {
    L.push(`A post I found through my feed that I'd like to discuss: "${i.title}"${i.by || i.author ? ` by ${i.by || i.author}` : ''}`, i.link);
    if (i.desc) L.push('', i.desc);
    L.push('', "Read it if you can reach it. Give me a short summary, its strongest point and its weakest one, then let's discuss. Answer in the language I write in.");
  }
  return L.filter(x => x !== undefined && x !== null).join('\n');
}
async function takeToClaude(id) {
  const it = findItem(id); if (!it) return;
  const prompt = claudePrompt(it);
  let copied = false;
  try { await navigator.clipboard.writeText(prompt); copied = true; } catch (e) { /* no clipboard in this window: the URL still carries the prompt */ }
  try { await api('/api/claude', { prompt }); toast(copied ? 'opening a conversation on claude.ai · the prompt is on your clipboard too, in case the box comes up empty' : 'opening a conversation on claude.ai', 5000); }
  catch (e) { toast(e.message, 5000); }
}

/* ------------------------------------------------------------ events */

document.addEventListener('click', async e => {
  const who = e.target.closest('.msgcard > .who[data-a="msg-fold"]');
  if (who && !e.target.closest('a, button')) { const card = who.parentElement; foldMsg(card, !card.classList.contains('folded')); return; }
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.v) { view = b.dataset.v; location.hash = view === 'feed' ? '' : view; render(); if (view === 'following') { loadSubs(); loadSync(); } else load(); return; }
  if (b.id && b.id.startsWith('fr-')) { friendsClick(b); return; }
  if (b.id === 'compact') { setCompact(!document.body.classList.contains('compact')); return; }
  if ((b.id && b.id.startsWith('msg-')) || (b.dataset.a && b.dataset.a.startsWith('msg-'))) { msgClick(b); return; }
  if (b.dataset.src !== undefined) { const [k, id] = b.dataset.src.split('|'); openSource(k, id); return; }
  if (b.id === 'srcback') { view = 'feed'; location.hash = ''; render(); load(); return; }
  if (b.dataset.f) { kindFilter = b.dataset.f; if (kindFilter !== 'arxiv') axResults = null; ytResults = null; render(); return; }
  if (b.dataset.a === 'yt-search') { searchYouTube(); return; }
  if (b.dataset.a === 'yt-clear') { ytResults = null; render(); return; }
  if (b.dataset.a === 'disc') { openThread(b.closest('article, .topic'), b.dataset.topic, b.dataset.stream); return; }
  if (b.dataset.a === 'unf-follow') { const { kind, ref, name } = b.dataset; b.disabled = true; try { await followRef(ref, kind, name); await loadUnfollowed(); } catch (err) { toast(err.message); b.disabled = false; } return; }
  if (b.dataset.a === 'unf-forget') { await api('/api/unfollowed/forget', { kind: b.dataset.kind, ref: b.dataset.ref }); await loadUnfollowed(); return; }
  if (b.dataset.a === 'reply') { sendReply(b.closest('.thread')); return; }
  if (b.dataset.a === 'thread-close') { closeThread(b.closest('article, .topic')); return; }
  if (b.dataset.a === 'thread-refresh') {
    const host = b.closest('article, .topic'), t = threads.get(hostKey(host));
    b.textContent = 'refreshing…';
    try { await api('/api/zulip/refresh', { stream: t && t.stream }); } catch (err) { toast(err.message); }
    talkTopics = {}; await refreshThread(host); return;
  }
  if (b.dataset.a === 'react') { react(b, b.dataset.emoji, b.dataset.code, b.dataset.type); return; }
  if (b.dataset.a === 'react-pick') { b.outerHTML = `<span class="pick">${EMOJI_PICK.map(([n, c]) => `<button data-a="react" data-emoji="${n}" data-code="${c}" data-type="unicode_emoji" title=":${n}:">${String.fromCodePoint(parseInt(c, 16))}</button>`).join('')}</span>`; return; }
  if (b.dataset.a === 'edit') { startEdit(b); return; }
  if (b.dataset.a === 'edit-save') { saveEdit(b); return; }
  if (b.dataset.a === 'edit-cancel') { cancelEdit(b); return; }
  if (b.closest('#streams') && b.dataset.stream !== undefined) { talkStream = b.dataset.stream; talkSearch = null; $('#talkq').value = ''; render(); return; }
  if (b.id === 'nt_open') { const f = $('#newtopic'); f.hidden = !f.hidden; if (!f.hidden) $('#nt_topic').focus(); return; }
  if (b.id === 'nt_cancel') { $('#newtopic').hidden = true; return; }
  if (b.id === 'talkq_clear') { talkSearch = null; $('#talkq').value = ''; renderTalk(); return; }
  if (b.id === 'talkrf') {
    b.textContent = 'refreshing…';
    try { await api('/api/zulip/refresh', { stream: curStream() }); } catch (err) { toast(err.message); }
    talkTopics = {}; talkSearch = null; await load(); b.textContent = 'refresh'; return;
  }
  if (b.dataset.a === 'src-follow') { const s = src.sub; b.disabled = true; b.textContent = 'adding…'; try { await followRef(s.id, s.kind, s.name); await openSource(s.kind, s.id); } catch (err) { toast(err.message); b.disabled = false; b.textContent = 'follow'; } return; }
  if (b.dataset.tag !== undefined) { tagFilter = tagFilter === b.dataset.tag ? null : b.dataset.tag; render(); return; }
  if (b.dataset.libf) { libKind = b.dataset.libf; render(); return; }
  if (b.dataset.libtag !== undefined) { libTag = libTag === b.dataset.libtag ? null : b.dataset.libtag; render(); return; }
  if (b.dataset.w !== undefined) { paperWindow = +b.dataset.w; axResults = null; render(); return; }
  if (b.dataset.pt !== undefined) { paperTypes[b.dataset.pt] = !paperTypes[b.dataset.pt]; render(); return; }
  if (b.id === 'helpbtn') { toggleHelp(); return; }
  if (b.id === 'ax_clear') { axResults = null; render(); return; }
  if (b.id === 'theme') { cycleTheme(); return; }
  if (b.id === 'restart') { restartServer(); return; }
  if (b.id === 'linkbtn') { const f = $('#linkform'); f.hidden = !f.hidden; if (!f.hidden) $('#linkq').focus(); return; }
  if (b.id === 'rf') { toast('refreshing…'); await api('/api/refresh', {}); setTimeout(() => load(true), 1500); return; }
  if (b.id === 'gear') { const p = $('#settings'); p.hidden = !p.hidden; if (!p.hidden) fillSettings(); return; }
  if (b.id === 's_save') { saveSettings(); return; }
  if (b.id === 'z_test') { testZulip(); return; }
  if (b.id === 'ig_connect' || b.id === 'ig_disconnect') { igClick(b); return; }
  if (b.id === 'cl_mcp' || b.id === 'cl_mcp_off' || (b.id && b.id.startsWith('br_'))) { claudeClick(b); return; }
  if (b.id === 'upd_check' || b.id === 'update-go') { updateClick(b); return; }
  if (b.id === 'quit') { if (confirm('stop feedel? the page will stop updating until you start it again.')) { await api('/api/quit', {}); document.body.innerHTML = '<main><p class="meta">feedel stopped. start it again to continue.</p></main>'; } return; }
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
      const s = (subs || []).find(x => x.kind === kind && x.id === id) || (src && src.sub);
      s.muted = !s.muted;
      await api('/api/subs/update', { kind, id, patch: { muted: s.muted } });
      if (view === 'source') openSource(kind, id); else renderSubs();
    }
    if (b.dataset.a === 'unfollow') {
      const s = (subs || []).find(x => x.kind === kind && x.id === id) || (src && src.sub);
      if (!confirm(`unfollow ${s.name || s.id}?`)) return;
      await api('/api/subs/remove', { kind, id }); if (subs) subs = subs.filter(x => x !== s); toast('unfollowed');
      if (view === 'source') openSource(kind, id); else renderSubs();
    }
    return;
  }
  const art = b.closest('article');
  if (!art) return;
  const id = art.dataset.id;
  if (b.dataset.a === 'papers') { kindFilter = 'arxiv'; render(); window.scrollTo(0, 0); return; }
  if (b.dataset.a === 'inbox-follow') {
    const it = findItem(id), f = it && it.follow; if (!f) return;
    b.disabled = true; b.textContent = 'adding…';
    try { await followRef(f.ref, f.kind, f.name); await load(); } catch (err) { b.disabled = false; b.textContent = 'follow'; toast(err.message); }
    return;
  }
  if (b.dataset.a === 'inbox-remove') { const it = findItem(id); await api('/api/inbox/remove', { url: it.url }); await load(); return; }
  if (b.dataset.a === 'twthread') { openTwThread(art); return; }
  if (b.dataset.a === 'twthread-close') { art.querySelector('.twthread')?.remove(); twOpen.delete(art.dataset.id); return; }
  if (b.dataset.a === 'twgo') { openTwThread(art, b.dataset.url, true); return; }
  if (b.dataset.a === 'twthread-back') { const box = art.querySelector('.twthread'); const prev = box && box._stack.pop(); if (prev) openTwThread(art, prev, false, box._stack); return; }
  if (b.dataset.a === 'tw-play') { try { toast(await api('/api/play_url', { url: b.dataset.url })); } catch (err) { toast(err.message); } return; }
  if (b.dataset.a === 'share') { openShare(art); return; }
  if (b.dataset.a === 'claude') { takeToClaude(id); return; }
  if (b.dataset.a === 'share-send') { sendShare(art); return; }
  if (b.dataset.a === 'share-rec') { showPeople(art.querySelector('.share')); return; }
  if (b.dataset.a === 'share-send-rec') { sendShare(art, true); return; }
  if (b.dataset.a === 'thanks') { b.disabled = true; try { await api('/api/recs/thanks', { msg_id: +b.dataset.msg }); b.textContent = 'thanked'; } catch (err) { b.disabled = false; toast(err.message); } return; }
  if (b.dataset.a === 'share-cancel') { art.querySelector('.share')?.remove(); return; }
  if (b.dataset.a === 'share-settings') { $('#settings').hidden = false; fillSettings(); window.scrollTo(0, 0); return; }
  if (b.dataset.a === 'abs') { const a = art.querySelector('.abs'); a.hidden = !a.hidden; b.textContent = a.hidden ? 'abstract' : 'hide abstract'; return; }
  if (b.dataset.a === 'citedby' || b.dataset.a === 'refs') { openInspire(art, b.dataset.a === 'refs' ? 'references' : 'citedby'); return; }
  if (b.dataset.a === 'insp-close') { art.querySelector('.inspbox')?.remove(); inspireOpen.delete(id); return; }
  if (b.dataset.a === 'more') { const t = art.querySelector('.tweet, .desc'); t.classList.toggle('open'); b.textContent = t.classList.contains('open') ? 'less' : 'more'; return; }
  if (b.dataset.a === 'copy') { const it = findItem(id), link = itemLink(it); try { await navigator.clipboard.writeText(link); toast('link copied'); } catch (err) { toast(link, 6000); } return; }
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
  if (b.dataset.a === 'playurl' || b.dataset.a === 'dlurl') {
    const it = findItem(id);
    try { toast(await api(b.dataset.a === 'playurl' ? '/api/play_url' : '/api/dl_url', { url: (b.dataset.a === 'playurl' && it.mp4) || it.link })); if (b.dataset.a === 'playurl') { seen.add(id); render(); } }
    catch (err) { toast(err.message); }
    return;
  }
  if (b.dataset.a === 'play' || b.dataset.a === 'dl') {
    const r = await fetch('/api/' + b.dataset.a + '?id=' + encodeURIComponent(id), { method: 'POST' });
    toast(await r.text());
    if (b.dataset.a === 'play') { seen.add(id); render(); }
  }
});

document.addEventListener('input', e => {
  const inp = e.target;
  if (inp.id === 'subfilter') return renderSubs();
  if (inp.id === 'feedsearch') { feedQuery = inp.value.trim(); ytResults = null; render(); return; }
  if (inp.id === 'fr-link' || inp.id === 'fr-organizer') { queueInviteSave(); return; }
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
$('#talksearch').addEventListener('submit', e => { e.preventDefault(); searchTalk(); });
$('#newtopic').addEventListener('submit', e => { e.preventDefault(); postTopic(); });
$('#linkform').addEventListener('submit', async e => {
  e.preventDefault();
  const u = $('#linkq').value.trim(); if (!u) return;
  const btn = $('#linkform button'); btn.disabled = true; btn.textContent = 'fetching…';
  try { await api('/api/inbox', { url: u, via: 'box' }); $('#linkq').value = ''; $('#linkform').hidden = true; kindFilter = 'inbox'; await load(); }
  catch (err) { toast(err.message, 5000); }
  btn.disabled = false; btn.textContent = 'open in feedel';
});
const drop = $('#drop');
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); importFiles(e.dataTransfer.files); });
document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const inField = e.target.matches && e.target.matches('input, select, textarea, [contenteditable]');
  if (e.key === 'Escape') {
    $('#settings').hidden = true; toggleHelp(false);
    if (inField) { if (e.target === $('#feedsearch')) { $('#feedsearch').value = ''; feedQuery = ''; ytResults = null; render(); } e.target.blur(); }
    else setCur(null);
    return;
  }
  if (inField) {
    if (e.key === 'Enter' && e.target === $('#feedsearch') && feedQuery && (kindFilter === 'all' || kindFilter === 'yt') && !document.querySelector('#list article')) searchYouTube();
    return;
  }
  const k = e.key;
  if (k === '?') { toggleHelp(); e.preventDefault(); return; }
  if (k === '/') { const f = searchField(); if (f) { f.focus(); f.select(); e.preventDefault(); } return; }
  if (k === 'j' || k === 'k') { moveCur(k === 'j' ? 1 : -1); e.preventDefault(); return; }
  const art = curArticle(); if (!art) return;
  const id = art.dataset.id, link = sel => { const l = art.querySelector(sel); if (l) window.open(l.href, '_blank', 'noopener'); };
  if (k === 'o' || k === 'Enter') (art.querySelector('[data-a=abs]') || art.querySelector('.acts button, .acts a'))?.click();  // a paper's abstract; otherwise the item's first action
  else if (k === 's' && id !== 'digest') setMark(id, 'saved', !saved.has(id));
  else if (k === 'm' && id !== 'digest') setMark(id, 'seen', !seen.has(id));
  else if (k === 'a') link('.acts a[href*="/abs/"], .acts a[href]');
  else if (k === 'p') link('.acts a[href*="/pdf/"]');
  else if (k === 'c') art.querySelector('[data-a=citedby]')?.click();
  else if (k === 'r') art.querySelector('[data-a=refs]')?.click();
  else if (k === 'x') foldCur(art);
  else return;
  e.preventDefault();
});

try { setCompact(localStorage.getItem('compact') === '1'); } catch (e) { /* ignore */ }
let theme = null; try { theme = localStorage.getItem('theme'); } catch (e) { /* ignore */ }
applyTheme(theme);
$('#addhint').textContent = HINTS.yt;
if (location.hash === '#following') view = 'following';
if (location.hash === '#library') view = 'library';
if (location.hash === '#friends') view = 'friends';
if (location.hash === '#talk') view = 'talk';
if (location.hash === '#messages') view = 'messages';
if (location.hash === '#inbox') kindFilter = 'inbox';
const srcHash = location.hash.match(/^#source\/(\w+)\/(.+)$/);
if (srcHash) setTimeout(() => openSource(srcHash[1], decodeURIComponent(srcHash[2])), 0);
load();
if (view === 'following') { loadSubs(); loadSync(); }
setInterval(() => { if (view === 'feed' && !axResults) load(); else if (view === 'talk' && !talkSearch) { talkTopics = {}; load(); } }, 60000);
