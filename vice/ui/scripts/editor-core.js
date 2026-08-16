'use strict';
// editor-core.js: timeline editor state, project ops, autosave, keyboard

// ═══════════════════════════════════════════════════════════════════
// Editor state
// ═══════════════════════════════════════════════════════════════════
let edProject   = null;      // {version, tracks, items}; null until first enter
let edSel       = null;      // selected item id
let edPlayhead  = 0;
let edPlaying   = false;
let edPps       = 12;        // timeline zoom, px per second
let edClipboard = null;
let edMissing   = new Set(); // clipIds referenced by the project but not in `clips`
let edLoaded    = false;
let edDirty     = false;
let edSaveTimer = null;
let edUndoStack = [];
let edRedoStack = [];

const ED_PPS_MIN = 4, ED_PPS_MAX = 48;
const ED_UNDO_CAP = 50;

const ED_FX = [
  { id: 'crossfade', name: 'Crossfade',     desc: 'Blend outgoing into incoming',      len: 1.0,
    glyph: '<rect x="3" y="8" width="11" height="11" rx="2"/><rect x="10" y="5" width="11" height="11" rx="2" opacity=".5"/>' },
  { id: 'fadeblack', name: 'Fade to black', desc: 'Dip through pure black',            len: 0.8,
    glyph: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M12 5h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-7z" fill="currentColor" stroke="none" opacity=".8"/>' },
  { id: 'fadewhite', name: 'Fade to white', desc: 'Bloom through white',               len: 0.8,
    glyph: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M12 5h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-7z" fill="currentColor" stroke="none" opacity=".3"/>' },
  { id: 'dipaccent', name: 'Dip to accent', desc: 'Flash the theme color',             len: 0.7,
    glyph: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="12" cy="12" r="3.5" fill="currentColor" stroke="none"/>' },
  { id: 'blurdis',   name: 'Blur dissolve', desc: 'Defocus across the cut',            len: 1.2,
    glyph: '<circle cx="9" cy="12" r="5"/><circle cx="15" cy="12" r="5" opacity=".5"/>' },
  { id: 'slide',     name: 'Slide',         desc: 'Incoming pushes in from the right', len: 0.8,
    glyph: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 12h7"/><path d="m12 9 3 3-3 3"/>' },
];

const ED_FONTS = {
  display: { label: 'Geist',          stack: "'Geist', sans-serif" },
  body:    { label: 'Inter',          stack: "'Inter', sans-serif" },
  mono:    { label: 'JetBrains Mono', stack: "'JetBrains Mono', monospace" },
};

const ED_TEXT_PRESETS = [
  { id: 'title',    name: 'Title',       font: 'display', size: 64, weight: 600, color: '#f2f5fa', sample: 'Match point',        x: 50, y: 44 },
  { id: 'subtitle', name: 'Subtitle',    font: 'body',    size: 34, weight: 500, color: '#dbe1ea', sample: 'Ranked grind',       x: 50, y: 58 },
  { id: 'caption',  name: 'Caption',     font: 'mono',    size: 22, weight: 400, color: '#b8c0cd', sample: '1920x1080 · 60 fps', x: 50, y: 88 },
  { id: 'lower',    name: 'Lower third', font: 'display', size: 40, weight: 600, color: '#f2f5fa', sample: 'Player · support',   x: 22, y: 84 },
];

const ED_SWATCHES = ['#f2f5fa', '#33adff', '#c4b5fd', '#6ee7b7', '#fdba74'];

function edCapture(el, e) {
  try { el.setPointerCapture(e.pointerId); } catch (_) {}
}

function edFx(id)   { return ED_FX.find(f => f.id === id); }
function edClipOf(it) { return clips.find(c => c.slug === it.clipId); }
function edUid()    { return 'i' + Math.random().toString(36).slice(2, 9); }
function edFmt(t)   {
  const m = Math.floor(t / 60), s = Math.floor(t % 60), d = Math.floor((t % 1) * 10);
  return `${m}:${String(s).padStart(2, '0')}.${d}`;
}
function edFmtS(t) { return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`; }
function edRound(v) { return Math.round(v * 1000) / 1000; }

function edDefaultProject() {
  return {
    version: 1,
    tracks: [
      { id: 'T1', type: 'text',  label: 'T1' },
      { id: 'V2', type: 'video', label: 'V2' },
      { id: 'V1', type: 'video', label: 'V1' },
      { id: 'A1', type: 'audio', label: 'A1' },
    ],
    items: [],
  };
}

function edEnd() {
  return edProject
    ? edProject.items.reduce((m, i) => Math.max(m, i.start + i.dur), 0)
    : 0;
}

function edItems(trackId) {
  return edProject.items.filter(i => i.trackId === trackId).sort((a, b) => a.start - b.start);
}

function edItem(id) { return edProject.items.find(i => i.id === id); }
function edSelItem() { return edSel ? edItem(edSel) : null; }
function edVideoTracks() { return edProject.tracks.filter(t => t.type === 'video'); }

// Source duration for trimming bounds. Missing clips keep their timeline
// length so the layout survives until the clip returns or is removed.
function edSourceDur(it) {
  const c = edClipOf(it);
  return c && c.duration ? c.duration : (it.offset || 0) + it.dur;
}

// ═══════════════════════════════════════════════════════════════════
// Persistence
// ═══════════════════════════════════════════════════════════════════
async function edLoad() {
  try {
    const r = await fetch('/api/editor/project');
    const d = await r.json();
    edProject = d.project && Array.isArray(d.project.tracks) && d.project.tracks.length
      ? d.project : edDefaultProject();
    edMissing = new Set(d.missing || []);
  } catch (_) {
    edProject = edProject || edDefaultProject();
  }
  edLoaded = true;
}

function edScheduleSave() {
  edDirty = true;
  clearTimeout(edSaveTimer);
  edSaveTimer = setTimeout(edSaveNow, 800);
}

async function edSaveNow() {
  if (!edDirty || !edProject) return;
  edDirty = false;
  try {
    await fetch('/api/editor/project', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(edProject),
    });
  } catch (_) { edDirty = true; }
}

function edRefreshMissing() {
  const have = new Set(clips.map(c => c.slug));
  edMissing = new Set(edProject.items
    .filter(i => i.clipId && !have.has(i.clipId))
    .map(i => i.clipId));
}

// ═══════════════════════════════════════════════════════════════════
// Undo / redo, JSON snapshots pushed on every committed edit
// ═══════════════════════════════════════════════════════════════════
function edSnapshot() {
  edUndoStack.push(JSON.stringify(edProject));
  if (edUndoStack.length > ED_UNDO_CAP) edUndoStack.shift();
  edRedoStack = [];
}

function edUndo() {
  if (!edUndoStack.length) return;
  edRedoStack.push(JSON.stringify(edProject));
  edProject = JSON.parse(edUndoStack.pop());
  edAfterRestore();
}

function edRedo() {
  if (!edRedoStack.length) return;
  edUndoStack.push(JSON.stringify(edProject));
  edProject = JSON.parse(edRedoStack.pop());
  edAfterRestore();
}

function edAfterRestore() {
  if (edSel && !edItem(edSel)) edSel = null;
  edRefreshMissing();
  edScheduleSave();
  edRenderTimeline();
  edRenderPreviewFrame(true);
}

// Commit an edit: fix up junctions, re-render, autosave. `snap` was pushed
// by the caller before mutating (edBegin).
function edCommit() {
  edPruneTransitions();
  edScheduleSave();
  edRenderTimeline();
  edRenderPreviewFrame(true);
}

function edBegin() { edSnapshot(); }

// A transition needs something to its left: keep `trans` only on items that
// either abut a left neighbor or sit at a lane position where a lead-in
// fade still makes sense (anything not at 0 has a gap, which fades from
// black anyway, so only exact duplicates of removed junctions are pruned).
function edPruneTransitions() {
  edProject.items.forEach(it => {
    if (it.trans && it.kind === 'clip') {
      const cap = Math.min(3, edRound(it.dur * 0.8));
      it.trans.len = Math.min(Math.max(0.2, it.trans.len), cap);
    }
  });
}

// ═══════════════════════════════════════════════════════════════════
// Item ops
// ═══════════════════════════════════════════════════════════════════

// Push an item into the nearest gap on its lane so lane-mates never overlap.
function edResolve(it) {
  const mates = edProject.items
    .filter(o => o.trackId === it.trackId && o.id !== it.id)
    .sort((a, b) => a.start - b.start);
  let s = Math.max(0, it.start);
  for (let pass = 0; pass < mates.length + 1; pass++) {
    const hit = mates.find(o => s < o.start + o.dur && s + it.dur > o.start);
    if (!hit) break;
    const before = hit.start - it.dur, after = hit.start + hit.dur;
    s = (before >= 0 && Math.abs(before - s) <= Math.abs(after - s)) ? before : after;
  }
  it.start = edRound(Math.max(0, s));
  return it;
}

function edInsert(it) {
  edProject.items.push(edResolve(it));
  return it;
}

function edSnapTime(t, exclId) {
  const pts = [0, edPlayhead];
  edProject.items.forEach(i => {
    if (i.id !== exclId) pts.push(i.start, i.start + i.dur);
  });
  let best = t, bd = 8 / edPps;
  pts.forEach(p => {
    const d = Math.abs(p - t);
    if (d < bd) { bd = d; best = p; }
  });
  return Math.max(0, edRound(best));
}

// Trim limits against lane neighbors.
function edLimits(it) {
  let prevEnd = 0, nextStart = Infinity;
  edProject.items.forEach(o => {
    if (o.trackId !== it.trackId || o.id === it.id) return;
    const e = o.start + o.dur;
    if (e <= it.start + 0.001 && e > prevEnd) prevEnd = e;
    if (o.start >= it.start + it.dur - 0.001 && o.start < nextStart) nextStart = o.start;
  });
  return { prevEnd, nextStart };
}

function edAddClip(slug, trackId, t, asAudio) {
  if (!edProject) return;
  const c = clips.find(x => x.slug === slug);
  if (!c || !c.duration) { toast('That clip has no readable duration yet', 'err'); return; }
  edBegin();
  const it = edInsert({
    id: edUid(), kind: asAudio ? 'audio' : 'clip', trackId,
    clipId: slug, start: edRound(Math.max(0, t)), dur: edRound(c.duration), offset: 0,
  });
  edSel = it.id;
  edCommit();
}

function edQuickAdd(slug) {
  if (!edProject) return;
  const vts = edVideoTracks();
  const vt = vts[vts.length - 1];
  const end = edItems(vt.id).reduce((m, i) => Math.max(m, i.start + i.dur), 0);
  edAddClip(slug, vt.id, end, false);
}

function edAddText(presetId, opts = {}) {
  if (!edProject) return;
  const p = ED_TEXT_PRESETS.find(x => x.id === presetId);
  const tt = edProject.tracks.find(t => t.type === 'text');
  if (!p || !tt) return;
  edBegin();
  const it = edInsert({
    id: edUid(), kind: 'text', trackId: tt.id,
    start: edRound(Math.max(0, opts.t !== undefined ? opts.t : edPlayhead)), dur: 4,
    text: p.sample, font: p.font, size: p.size, weight: p.weight, color: p.color,
    x: opts.x !== undefined ? edRound(opts.x) : p.x,
    y: opts.y !== undefined ? edRound(opts.y) : p.y,
  });
  edSel = it.id;
  edCommit();
}

function edApplyFx(itemId, fxId) {
  const it = edItem(itemId), fx = edFx(fxId);
  if (!it || !fx || it.kind !== 'clip') return;
  edBegin();
  it.trans = { fx: fxId, len: Math.min(fx.len, edRound(it.dur * 0.8)) };
  edSel = itemId;
  edCommit();
}

function edSplitSel() {
  const it = edSelItem();
  if (!it || edPlayhead <= it.start + 0.2 || edPlayhead >= it.start + it.dur - 0.2) return;
  edBegin();
  const t = edPlayhead;
  const right = Object.assign({}, it, {
    id: edUid(), start: edRound(t), dur: edRound(it.start + it.dur - t),
    offset: edRound((it.offset || 0) + (t - it.start)),
  });
  delete right.trans;
  it.dur = edRound(t - it.start);
  edProject.items.push(right);
  edCommit();
}

function edDetachAudio() {
  const it = edSelItem();
  if (!it || it.kind !== 'clip' || it.muted) return;
  const c = edClipOf(it);
  if (!c) return;
  edBegin();
  let a = edProject.tracks.find(t => t.type === 'audio');
  if (!a) {
    a = { id: edUid(), type: 'audio', label: 'A1' };
    edProject.tracks.push(a);
  }
  it.muted = true;
  const audio = edInsert({
    id: edUid(), kind: 'audio', trackId: a.id, clipId: it.clipId,
    start: it.start, dur: it.dur, offset: it.offset || 0,
  });
  edSel = audio.id;
  edCommit();
}

function edDuplicateSel() {
  const it = edSelItem();
  if (!it) return;
  edBegin();
  const copy = Object.assign({}, it, { id: edUid(), start: edRound(it.start + it.dur + 0.25) });
  delete copy.trans;
  edInsert(copy);
  edSel = copy.id;
  edCommit();
}

function edCopySel() {
  const it = edSelItem();
  if (it) edClipboard = JSON.parse(JSON.stringify(it));
}

function edPaste() {
  if (!edClipboard || !edProject.tracks.find(t => t.id === edClipboard.trackId)) return;
  edBegin();
  const copy = Object.assign({}, edClipboard, { id: edUid(), start: edRound(edPlayhead) });
  delete copy.trans;
  edInsert(copy);
  edSel = copy.id;
  edCommit();
}

function edDeleteSel() {
  if (!edSel) return;
  edBegin();
  edProject.items = edProject.items.filter(i => i.id !== edSel);
  edSel = null;
  edCommit();
}

function edAddTrack(type) {
  edBegin();
  const n = edProject.tracks.filter(t => t.type === type).length + 1;
  const nt = { id: edUid(), type, label: (type === 'video' ? 'V' : 'A') + n };
  if (type === 'audio') {
    edProject.tracks.push(nt);
  } else {
    const idx = edProject.tracks.findIndex(t => t.type === 'video');
    edProject.tracks.splice(idx === -1 ? edProject.tracks.length : idx, 0, nt);
  }
  edCommit();
}

function edRemoveTrack(id) {
  const tr = edProject.tracks.find(t => t.id === id);
  if (!tr || tr.type === 'text') return;
  if (tr.type === 'video' && edVideoTracks().length <= 1) return;
  edBegin();
  edProject.tracks = edProject.tracks.filter(t => t.id !== id);
  edProject.items = edProject.items.filter(i => i.trackId !== id);
  edSel = null;
  edCommit();
}

function edReset() {
  edBegin();
  edProject = edDefaultProject();
  edSel = null;
  edPlayhead = 0;
  edSetPlaying(false);
  edMissing = new Set();
  edCommit();
}

// A clip vanished (deleted elsewhere); the server already migrated the
// stored project, mirror it locally without spawning an undo entry.
function edOnClipDeleted(slug) {
  if (!edProject) return;
  const before = edProject.items.length;
  edProject.items = edProject.items.filter(i => i.clipId !== slug);
  if (edSel && !edItem(edSel)) edSel = null;
  edRefreshMissing();
  if (before !== edProject.items.length && currentView === 'editor') {
    edRenderTimeline();
    edRenderPreviewFrame(true);
  }
}

// The server rewrote the stored project (rename migration): reload it, but
// never while the user has unsaved local changes in flight.
async function edOnProjectChanged() {
  if (!edLoaded || edDirty) return;
  await edLoad();
  if (currentView === 'editor') {
    if (edSel && !edItem(edSel)) edSel = null;
    edRenderTimeline();
    edRenderPreviewFrame(true);
  }
}

// ═══════════════════════════════════════════════════════════════════
// View enter / leave + keyboard
// ═══════════════════════════════════════════════════════════════════
async function editorEnter() {
  document.querySelector('.stage').classList.add('stage-editor');
  document.getElementById('quit-row')?.classList.add('ed-hidden');
  edInitPanels();
  if (!edLoaded) await edLoad();
  edRefreshMissing();
  edRenderLibrary();
  edRenderTimeline();
  edRenderPreviewFrame(true);
}

function editorLeave() {
  document.querySelector('.stage').classList.remove('stage-editor');
  document.getElementById('quit-row')?.classList.remove('ed-hidden');
  edSetPlaying(false);
  edReleasePool();
  if (edDirty) edSaveNow();
}

// The clip list refreshed (startup fetch or a live save). The library and
// the missing-clip styling must follow, or an editor opened before the
// first /api/clips response stays empty forever.
function edOnClipsRefreshed() {
  if (!edLoaded || currentView !== 'editor') return;
  edRefreshMissing();
  edRenderLibrary();
  edRenderTimeline();
}

function edKeydown(e) {
  if (currentView !== 'editor' || !edProject) return;
  const tg = e.target;
  if (tg.tagName === 'INPUT' || tg.tagName === 'TEXTAREA' || tg.tagName === 'SELECT'
      || tg.isContentEditable) return;
  if (document.querySelector('.backdrop.open')) return;
  const mod = e.metaKey || e.ctrlKey;
  if (e.code === 'Space') { e.preventDefault(); edSetPlaying(!edPlaying); }
  else if (e.key === 'Delete' || e.key === 'Backspace') edDeleteSel();
  else if (mod && !e.shiftKey && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); edUndo(); }
  else if (mod && e.shiftKey && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); edRedo(); }
  else if (mod && (e.key === 'c' || e.key === 'C')) { e.preventDefault(); edCopySel(); }
  else if (mod && (e.key === 'v' || e.key === 'V')) { e.preventDefault(); edPaste(); }
  else if (mod && (e.key === 'd' || e.key === 'D')) { e.preventDefault(); edDuplicateSel(); }
  else if (!mod && (e.key === 's' || e.key === 'S')) edSplitSel();
  else if (e.key === 'ArrowLeft')  { edSeek(Math.max(0, edPlayhead - (e.shiftKey ? 0.1 : 1))); }
  else if (e.key === 'ArrowRight') { edSeek(Math.min(edEnd(), edPlayhead + (e.shiftKey ? 0.1 : 1))); }
  else if (e.key === 'Escape') { edSelect(null); }
  else if (e.key === '+' || e.key === '=') edZoom(1.3);
  else if (e.key === '-') edZoom(1 / 1.3);
}
document.addEventListener('keydown', edKeydown);

function edSelect(id) {
  edSel = id;
  edRenderTimelineSelection();
  edRenderInspector();
}

// ═══════════════════════════════════════════════════════════════════
// Panel resizers (pointer capture, like the trim handles)
// ═══════════════════════════════════════════════════════════════════
let edPanelsWired = false;

function edInitPanels() {
  if (edPanelsWired) return;
  edPanelsWired = true;
  const view = document.getElementById('view-editor');
  const state = { libW: Math.max(240, Math.min(324, Math.round(window.innerWidth * 0.22))), tlH: 300 };
  const apply = () => {
    view.style.setProperty('--ed-lib-w', state.libW + 'px');
    view.style.setProperty('--ed-tl-h', state.tlH + 'px');
  };
  apply();

  const wire = (id, horizontal) => {
    const el = document.getElementById(id);
    el.addEventListener('pointerdown', e => {
      e.preventDefault();
      edCapture(el, e);
      const s = horizontal ? e.clientX : e.clientY;
      const base = horizontal ? state.libW : state.tlH;
      const mv = ev => {
        const d = (horizontal ? ev.clientX : ev.clientY) - s;
        if (horizontal) state.libW = Math.max(232, Math.min(520, base + d));
        else state.tlH = Math.max(170, Math.min(Math.round(view.clientHeight * 0.7), base - d));
        apply();
      };
      const up = () => {
        el.removeEventListener('pointermove', mv);
        el.removeEventListener('pointerup', up);
        edRenderTimeline();
      };
      el.addEventListener('pointermove', mv);
      el.addEventListener('pointerup', up);
    });
  };
  wire('ed-rsz-h', true);
  wire('ed-rsz-v', false);
}
