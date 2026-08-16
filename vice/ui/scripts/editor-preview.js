'use strict';
// editor-preview.js: stage playback: per-track video pool, rAF clock,
// transition approximation, draggable text overlays, inspector

let edRaf = null;
let edLastTick = 0;
let edPool = {};          // trackId -> {els: [v,v], active, itemKey, preKey}
let edPoolKey = '';
let edLastTextKey = '';
let edStageSized = false;
let edMaster = null;      // state of the visible track, drives the clock

const ED_DRIFT = 0.15;
const ED_PRELOAD = 2.0;   // seconds before an item to start warming its video
const ED_WARM_MS = 350;   // muted decode warm-up after a src change
// Only the sliding transition is actual motion; the defocus and the fades
// are not, and software compositing is no reason to hide what a transition
// will look like once rendered.
const ED_REDUCED_MOTION = () =>
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// Seek only when the element is not already mid-seek: re-issuing a seek on
// every frame while the decoder catches up is what made first plays stutter.
// While playing, the clock follows the video (see edTick), so the tolerance
// is wide and only a real desync (a scrub, a stall) triggers a correction.
function edSeekVideo(v, t, tol) {
  if (v.readyState < 1 || v.seeking) return;
  if (Math.abs(v.currentTime - t) > (tol || ED_DRIFT)) {
    try { v.currentTime = t; } catch (_) {}
  }
}

function edPlayTol() { return edPlaying ? 0.45 : ED_DRIFT; }

// A fresh src decodes its first frames noticeably late. Whenever one is
// assigned, the element gets a short muted play window so the decoder is
// rolling before the frame is needed, then parks back on its in-point.
function edWarmStart(v, parkAt) {
  v._parkAt = parkAt || 0;
  v._warmUntil = performance.now() + ED_WARM_MS;
  v.muted = true;
  const p = v.play();
  if (p) p.catch(() => {});
}

function edWarming(v) {
  return v._warmUntil && performance.now() < v._warmUntil;
}

// A warmed-but-idle element has run past its in-point; park it back so the
// cut to it does not have to seek (which is what stuttered at boundaries).
function edSettle(v) {
  if (!v._warmUntil || performance.now() < v._warmUntil) return;
  v._warmUntil = 0;
  if (!v.paused) v.pause();
  edSeekVideo(v, v._parkAt || 0);
}

// ═══════════════════════════════════════════════════════════════════
// Stage sizing (16:9 letterboxed into the panel)
// ═══════════════════════════════════════════════════════════════════
function edInitStage() {
  if (edStageSized) return;
  edStageSized = true;
  const wrap = document.querySelector('.ed-stage-wrap');
  const stage = document.getElementById('ed-stage');
  const size = () => {
    const r = wrap.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return;
    const w = Math.min(r.width, r.height * 16 / 9);
    stage.style.width = w + 'px';
    stage.style.height = (w * 9 / 16) + 'px';
    edPositionTexts();
  };
  new ResizeObserver(size).observe(wrap);
  size();
}

// ═══════════════════════════════════════════════════════════════════
// Video pool
// ═══════════════════════════════════════════════════════════════════

// Detaching an element does not free what it decoded, the source has to go
// first, and load() is what actually tears the player down.
function edDropVideo(v) {
  v.pause();
  v.removeAttribute('src');
  v._key = null;
  try { v.load(); } catch (_) {}
  v.remove();
}

// Three elements per video track, each holding a clip it has decoded, is a
// reasonable thing to keep while the editor is on screen and a poor thing to
// keep for the rest of the session once it is not. Called on the way out; the
// pool rebuilds itself on the next edRenderPreviewFrame.
function edReleasePool() {
  Object.keys(edPool).forEach(tid => {
    edPool[tid].els.forEach(edDropVideo);
    delete edPool[tid];
  });
  edPoolKey = '';
  edMaster = null;
  Object.keys(edAudioPool).forEach(id => {
    const a = edAudioPool[id];
    a.pause();
    a.removeAttribute('src');
    try { a.load(); } catch (_) {}
    delete edAudioPool[id];
  });
}

function edSyncPool() {
  const stage = document.getElementById('ed-stage');
  const vts = edVideoTracks();
  const key = vts.map(t => t.id).join(',');
  if (key === edPoolKey) return;
  edPoolKey = key;

  Object.keys(edPool).forEach(tid => {
    if (!vts.find(t => t.id === tid)) {
      edPool[tid].els.forEach(edDropVideo);
      delete edPool[tid];
    }
  });
  // Three elements per track: the clip on screen, the one it is transitioning
  // out of, and the one coming up. With only two, the outgoing clip and the
  // preloaded one fought over the same element and reassigned its src every
  // frame, which is what froze playback through a run of transitions.
  vts.forEach((t, idx) => {
    if (!edPool[t.id]) {
      const make = () => {
        const v = document.createElement('video');
        v.playsInline = true;
        v.preload = 'auto';
        v.className = 'ed-vhide';
        v._key = null;
        v._need = 0;
        stage.insertBefore(v, stage.firstChild);
        return v;
      };
      edPool[t.id] = { els: [make(), make(), make()], itemKey: null,
                       shown: null, handover: false, seq: 0 };
    }
    // Track order is top-first. Each track owns a band of four so the roles
    // within it can stack, and every video stays below the preparing notice,
    // the fade overlay and the titles.
    edPool[t.id].zBase = 4 * (vts.length - idx);
  });
}

function edActiveItemOn(trackId, t) {
  return edItems(trackId).find(i =>
    i.kind === 'clip' && t >= i.start && t < i.start + i.dur &&
    !edMissing.has(i.clipId)) || null;
}

function edNextItemOn(trackId, t) {
  return edItems(trackId).find(i =>
    i.kind === 'clip' && i.start > t && !edMissing.has(i.clipId)) || null;
}

function edItemKey(it) {
  const clip = edClipOf(it);
  return it.id + '|' + (clip ? clip.video_url : it.clipId);
}

// The item on the lane that a transition is coming out of.
function edPrevItemOn(trackId, it) {
  return edItems(trackId).filter(i =>
    i.kind === 'clip' && i.id !== it.id && !edMissing.has(i.clipId) &&
    Math.abs(i.start + i.dur - it.start) < 0.11).pop() || null;
}

// What this track needs decoded right now, most important first: the clip on
// screen, the one it is transitioning out of, and the one coming up.
function edNeeded(trackId, t) {
  const need = [];
  const push = (it, role, parkAt) => {
    if (!it || need.some(n => n.key === edItemKey(it))) return;
    if (!edClipOf(it)) return;
    need.push({ key: edItemKey(it), it, role, parkAt });
  };
  const cur = edActiveItemOn(trackId, t);
  push(cur, 'cur', (cur ? cur.offset || 0 : 0) + (cur ? Math.max(0, t - cur.start) : 0));
  if (cur && cur.trans && t < cur.start + cur.trans.len) {
    const prev = edPrevItemOn(trackId, cur);
    if (prev) push(prev, 'prev', (prev.offset || 0) + prev.dur);
  }
  const next = edNextItemOn(trackId, t);
  if (next && next.start - t < ED_PRELOAD) push(next, 'next', next.offset || 0);
  return need;
}

// Bind elements to the needed items. An element already holding the right
// source keeps it untouched, so a run of transitions never reassigns a src
// it already has. This is the only place src is written.
function edAllocate(pool, need) {
  pool.seq++;
  const taken = new Set();
  need.forEach(n => {
    const hit = pool.els.find(v => v._key === n.key && !taken.has(v));
    if (hit) { n.el = hit; taken.add(hit); }
  });
  need.forEach(n => {
    if (n.el) return;
    const free = pool.els.filter(v => !taken.has(v));
    const el = free.find(v => !v._key)
      || free.sort((a, b) => a._need - b._need)[0];
    if (!el) return;
    el._key = n.key;
    el.src = playbackUrl(edClipOf(n.it));
    edWarmStart(el, n.parkAt);
    n.el = el;
    taken.add(el);
  });
  need.forEach(n => { if (n.el) { n.el._role = n.role; n.el._need = pool.seq; } });
  pool.els.forEach(v => {
    if (taken.has(v)) return;
    v._role = null;
    edHideVideo(v);
    if (!v.paused && !edWarming(v)) v.pause();
    edSettle(v);
  });
  return need;
}

function edShowVideo(v, z) {
  v.classList.remove('ed-vhide');
  v.style.zIndex = z;
}

function edHideVideo(v) {
  v.classList.add('ed-vhide');
  v.style.opacity = '';
}

function edSyncTrack(trackId, t, isMaster) {
  const pool = edPool[trackId];
  if (!pool) return null;
  const need = edAllocate(pool, edNeeded(trackId, t));
  const curN = need.find(n => n.role === 'cur');
  const prevN = need.find(n => n.role === 'prev');

  if (!curN || !curN.el) {
    pool.els.forEach(edHideVideo);
    pool.itemKey = null;
    pool.shown = null;
    return null;
  }

  const it = curN.it;
  const cur = curN.el;
  const prev = prevN ? prevN.el : null;
  const fresh = pool.itemKey !== curN.key;
  // A track playing under the master is only kept loosely in sync, so it
  // needs one correction at the moment it becomes the master itself.
  const promoted = isMaster && !pool.wasMaster;
  pool.wasMaster = isMaster;
  if (fresh) {
    pool.itemKey = curN.key;
    // Hold the previous frame on screen until the new element can paint, so
    // a cut never flashes black while the decoder spins up.
    pool.handover = pool.shown && pool.shown !== cur;
  }
  if (pool.handover && cur.readyState >= 2 && !cur.seeking) pool.handover = false;

  const inTrans = it.trans && t < it.start + it.trans.len;
  pool.els.forEach(v => {
    if (v === cur) edShowVideo(v, pool.zBase + 2);
    else if (v === prev && inTrans) edShowVideo(v, pool.zBase + 1);
    else if (v === pool.shown && pool.handover) edShowVideo(v, pool.zBase + 1);
    else edHideVideo(v);
  });

  const desired = (it.offset || 0) + (t - it.start);
  if (edPlaying) {
    cur._warmUntil = 0;
    cur.muted = !!it.muted;
    if (cur.paused) { const p = cur.play(); if (p) p.catch(() => {}); }
    // The clock reads its time from the master element, so correcting that
    // element is chasing its own tail. Only a fresh cut, or a track playing
    // underneath the master, needs a correction.
    if (fresh || promoted) edSeekVideo(cur, desired, ED_DRIFT);
    else if (!isMaster) edSeekVideo(cur, desired, edPlayTol());
  } else if (edWarming(cur)) {
    cur._parkAt = desired;
  } else {
    if (!cur.paused) cur.pause();
    cur.muted = !!it.muted;
    edSeekVideo(cur, desired);
  }

  if (!pool.handover) pool.shown = cur;
  return { it, cur, prev, pool };
}

// ═══════════════════════════════════════════════════════════════════
// Transition approximation on the topmost active track
// ═══════════════════════════════════════════════════════════════════
function edApplyTransitionStyles(states, t) {
  const overlay = document.getElementById('ed-fade-overlay');
  let fadeOpacity = 0, fadeColor = '#02040a';

  states.forEach(st => {
    if (!st) return;
    const { it, cur, prev } = st;
    cur.style.opacity = '';
    cur.style.filter = '';
    cur.style.transform = '';
    if (prev) { prev.style.transform = ''; prev.style.filter = ''; }
    if (!it.trans) return;
    const len = it.trans.len;
    if (t < it.start || t >= it.start + len) return;
    const p = Math.max(0, Math.min(1, (t - it.start) / len));
    const fx = it.trans.fx;
    if (fx === 'crossfade' || fx === 'blurdis' || fx === 'slide') {
      if (fx === 'crossfade') cur.style.opacity = String(p);
      else if (fx === 'blurdis') {
        // Matches xfade hblur: both sides defocus, peaking at the midpoint,
        // while they cross over. Scaled to the stage so it reads the same at
        // any preview size.
        cur.style.opacity = String(p);
        const stage = document.getElementById('ed-stage');
        const peak = Math.sin(p * Math.PI) * Math.max(6, (stage.clientWidth || 640) * 0.022);
        cur.style.filter = `blur(${peak.toFixed(1)}px)`;
        if (prev) prev.style.filter = `blur(${peak.toFixed(1)}px)`;
      } else if (ED_REDUCED_MOTION()) {
        cur.style.opacity = String(p);
      } else {
        // Matches xfade slideleft: incoming enters from the right while the
        // outgoing is pushed off to the left.
        cur.style.transform = `translateX(${((1 - p) * 100).toFixed(2)}%)`;
        if (prev) prev.style.transform = `translateX(${(-p * 100).toFixed(2)}%)`;
      }
    } else {
      const color = fx === 'fadewhite' ? '#f2f5fa'
        : fx === 'dipaccent' ? `rgb(var(--accent-rgb))` : '#02040a';
      fadeOpacity = Math.max(fadeOpacity, 1 - p);
      fadeColor = color;
    }
  });

  overlay.style.background = fadeColor;
  overlay.style.opacity = String(fadeOpacity);
}

// Park the outgoing clip on its tail so a transition blends real frames.
// The allocator owns which element that is, so this only seeks it.
function edSyncOutgoing(st, t) {
  if (!st || !st.prev || !st.it.trans) return;
  const { it, prev: el } = st;
  if (t >= it.start + it.trans.len) return;
  const prev = edPrevItemOn(it.trackId, it);
  if (!prev) return;
  el.muted = true;
  const pd = Math.min((prev.offset || 0) + prev.dur + (t - it.start), edSourceDur(prev) - 0.05);
  edSeekVideo(el, pd, edPlayTol());
  if (edPlaying && el.paused) { const p = el.play(); if (p) p.catch(() => {}); }
  else if (!edPlaying && !el.paused) el.pause();
}

// ═══════════════════════════════════════════════════════════════════
// Audio items
// ═══════════════════════════════════════════════════════════════════
let edAudioPool = {};   // itemId -> Audio

function edSyncAudio(t) {
  const audioTracks = edProject.tracks.filter(tr => tr.type === 'audio').map(tr => tr.id);
  const active = new Set();
  edProject.items.forEach(it => {
    if (it.kind !== 'audio' || !audioTracks.includes(it.trackId)) return;
    if (t < it.start || t >= it.start + it.dur || edMissing.has(it.clipId)) return;
    active.add(it.id);
    let a = edAudioPool[it.id];
    const clip = edClipOf(it);
    if (!clip) return;
    if (!a) {
      a = new Audio();
      a.preload = 'auto';
      edAudioPool[it.id] = a;
    }
    const url = playbackUrl(clip);
    if (!a.src || !a.src.endsWith(url.slice(-40))) a.src = url;
    const desired = (it.offset || 0) + (t - it.start);
    edSeekVideo(a, desired, edPlayTol());
    if (edPlaying && a.paused) { const p = a.play(); if (p) p.catch(() => {}); }
    else if (!edPlaying && !a.paused) a.pause();
  });
  Object.keys(edAudioPool).forEach(id => {
    if (!active.has(id)) {
      edAudioPool[id].pause();
      if (!edItem(id)) { edAudioPool[id].src = ''; delete edAudioPool[id]; }
    }
  });
}

// ═══════════════════════════════════════════════════════════════════
// Text overlays
// ═══════════════════════════════════════════════════════════════════
function edVisibleTexts(t) {
  return edProject.items.filter(i =>
    i.kind === 'text' && t >= i.start && t < i.start + i.dur);
}

function edRenderTexts(t) {
  const stage = document.getElementById('ed-stage');
  const texts = edVisibleTexts(t);
  const key = texts.map(i => i.id).join(',') + '|' + edSel;
  if (key === edLastTextKey) { edPositionTexts(); return; }
  edLastTextKey = key;

  stage.querySelectorAll('.ed-text-overlay').forEach(el => el.remove());
  texts.forEach(it => {
    const el = document.createElement('div');
    el.className = 'ed-text-overlay' + (edSel === it.id ? ' selected' : '');
    el.dataset.text = it.id;
    const inner = document.createElement('span');
    inner.textContent = it.text || '';
    el.appendChild(inner);
    if (edSel === it.id) {
      ['tl', 'tr', 'bl', 'br'].forEach(corner => {
        const h = document.createElement('span');
        h.className = 'ed-th ' + corner;
        h.addEventListener('pointerdown', e => edTextResize(e, el, it.id));
        el.appendChild(h);
      });
    }
    stage.appendChild(el);
    el.addEventListener('pointerdown', e => edTextDrag(e, el, it.id));
  });
  edPositionTexts();
}

// Corner handles scale the title with the pointer's distance from its
// center, uncapped well past the slider range.
function edTextResize(e, el, id) {
  e.stopPropagation();
  e.preventDefault();
  const it = edItem(id);
  if (!it) return;
  edBegin();
  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const d0 = Math.max(12, Math.hypot(e.clientX - cx, e.clientY - cy));
  const s0 = it.size;
  const h = e.currentTarget;
  edCapture(h, e);
  const mv = ev => {
    const d = Math.hypot(ev.clientX - cx, ev.clientY - cy);
    it.size = Math.round(Math.min(1000, Math.max(8, s0 * d / d0)));
    edPositionTexts();
    setText('ed-insp-size-val', it.size + 'px');
  };
  const up = () => {
    h.removeEventListener('pointermove', mv);
    h.removeEventListener('pointerup', up);
    edScheduleSave();
    edRenderInspector();
  };
  h.addEventListener('pointermove', mv);
  h.addEventListener('pointerup', up);
}

function edPositionTexts() {
  const stage = document.getElementById('ed-stage');
  const w = stage.clientWidth || 1;
  stage.querySelectorAll('.ed-text-overlay').forEach(el => {
    const it = edItem(el.dataset.text);
    if (!it) return;
    el.style.left = it.x + '%';
    el.style.top = it.y + '%';
    el.style.fontFamily = ED_FONTS[it.font] ? ED_FONTS[it.font].stack : ED_FONTS.display.stack;
    el.style.fontWeight = it.weight;
    el.style.fontSize = (it.size * w / 1920) + 'px';
    el.style.color = it.color;
    el.style.letterSpacing = it.font === 'display' ? '-.02em' : '0';
  });
}

function edTextDrag(e, el, id) {
  e.stopPropagation();
  e.preventDefault();
  edSelect(id);
  const it = edItem(id);
  if (!it) return;
  edBegin();
  const stage = document.getElementById('ed-stage').getBoundingClientRect();
  const ox = it.x, oy = it.y, sx = e.clientX, sy = e.clientY;
  edCapture(el, e);
  const mv = ev => {
    it.x = edRound(Math.min(97, Math.max(3, ox + (ev.clientX - sx) / stage.width * 100)));
    it.y = edRound(Math.min(95, Math.max(4, oy + (ev.clientY - sy) / stage.height * 100)));
    edPositionTexts();
    edUpdateInspectorPos();
  };
  const up = () => {
    el.removeEventListener('pointermove', mv);
    el.removeEventListener('pointerup', up);
    edScheduleSave();
    edRenderTimeline();
  };
  el.addEventListener('pointermove', mv);
  el.addEventListener('pointerup', up);
}

// ═══════════════════════════════════════════════════════════════════
// Inspector (selected text item)
// ═══════════════════════════════════════════════════════════════════
function edRenderInspector() {
  const box = document.getElementById('ed-inspector');
  const it = edSelItem();
  if (!it || it.kind !== 'text') { box.hidden = true; return; }
  box.hidden = false;
  document.getElementById('ed-insp-text').value = it.text || '';
  document.getElementById('ed-insp-font').value = it.font;
  document.getElementById('ed-insp-size').value = it.size;
  setText('ed-insp-size-val', it.size + 'px');
  document.querySelectorAll('.ed-swatch').forEach(sw =>
    sw.classList.toggle('selected', sw.dataset.color === it.color));
  edUpdateInspectorPos();
}

function edUpdateInspectorPos() {
  const it = edSelItem();
  if (it && it.kind === 'text') {
    setText('ed-insp-pos', `x ${Math.round(it.x)}% · y ${Math.round(it.y)}% · drag on the preview`);
  }
}

function edInspectorChange(field, value) {
  const it = edSelItem();
  if (!it || it.kind !== 'text') return;
  if (field === 'size') value = parseInt(value, 10);
  edBegin();
  it[field] = value;
  edScheduleSave();
  edLastTextKey = '';
  edRenderTexts(edPlayhead);
  if (field === 'size') setText('ed-insp-size-val', value + 'px');
  if (field === 'color') document.querySelectorAll('.ed-swatch').forEach(sw =>
    sw.classList.toggle('selected', sw.dataset.color === value));
  if (field === 'text') edRenderTimeline();
}

function edInspectorRemove() {
  edDeleteSel();
}

// ═══════════════════════════════════════════════════════════════════
// Clock + frame sync
// ═══════════════════════════════════════════════════════════════════
function edSetPlaying(playing) {
  if (playing && edEnd() <= 0) return;
  if (playing && edPlayhead >= edEnd() - 0.01) edPlayhead = 0;
  edPlaying = playing;
  const btn = document.getElementById('ed-btn-play');
  if (btn) btn.innerHTML = svgEl(playing ? 'pause' : 'play', 13);
  if (!playing) { edRenderPreviewFrame(false); return; }
  if (edRaf) return;

  // Let the decoders spin up before the clock starts, otherwise the first
  // half second runs ahead of the picture and looks like a stutter.
  edRenderPreviewFrame(true);
  const start = () => {
    if (!edPlaying || edRaf) return;
    edLastTick = performance.now();
    edRaf = requestAnimationFrame(edTick);
  };
  const lead = edMaster && edMaster.cur;
  if (lead && lead.readyState < 3) {
    let fired = false;
    const go = () => { if (fired) return; fired = true; start(); };
    lead.addEventListener('playing', go, { once: true });
    lead.addEventListener('canplaythrough', go, { once: true });
    setTimeout(go, 400);
  } else {
    start();
  }
}

function edTick(now) {
  edRaf = null;
  if (!edPlaying || currentView !== 'editor') { edPlaying = false; edRenderPreviewFrame(false); return; }
  const end = edEnd();
  const dt = Math.min(0.25, (now - edLastTick) / 1000);
  edLastTick = now;
  edPlayhead = Math.min(end, edPlayhead + dt);
  edRenderPreviewFrame(false);

  // The video, not the wall clock, is the timebase while a clip is on
  // screen: chasing wall time forced a corrective seek every few frames,
  // which is what stuttered. Wall time only carries across gaps and cuts.
  const m = edMaster;
  if (m && !m.cur.paused && !m.cur.seeking && m.cur.readyState >= 2) {
    const vt = m.it.start + (m.cur.currentTime - (m.it.offset || 0));
    if (vt >= m.it.start && vt <= m.it.start + m.it.dur
        && Math.abs(vt - edPlayhead) < 0.5) {
      edPlayhead = Math.min(end, vt);
    }
  }

  edUpdatePlayheadDom();
  edUpdateTimeReadout();
  if (edPlayhead >= end) { edSetPlaying(false); return; }
  if (edPlaying) edRaf = requestAnimationFrame(edTick);
}

function edRenderPreviewFrame(structural) {
  if (!edProject || currentView !== 'editor') return;
  edInitStage();
  edSyncPool();
  const t = edPlayhead;
  const vts = edVideoTracks();
  // Tracks are listed top first, so the first active one is what the viewer
  // is actually looking at. The clock has to follow that, not a lower track
  // hidden underneath it.
  let master = null;
  const states = vts.map(tr => {
    const st = edSyncTrack(tr.id, t, !master);
    if (st && !master) master = st;
    return st;
  });
  edMaster = master;
  states.forEach(st => edSyncOutgoing(st, t));
  edApplyTransitionStyles(states, t);
  edSyncAudio(t);
  edRenderTexts(t);

  const anyVideo = states.some(st => st);
  document.getElementById('ed-stage-empty').style.display = anyVideo ? 'none' : 'flex';
  const preparing = states.some(st => {
    if (!st) return false;
    const c = edClipOf(st.it);
    return c && clipNeedsProxy(c) && st.cur.readyState < 2;
  });
  document.getElementById('ed-stage-preparing').style.display = preparing ? 'flex' : 'none';

  if (structural) edUpdatePlayheadDom();
}

// Text presets dropped straight onto the preview land at the pointer.
function edWireStageDrop() {
  const stage = document.getElementById('ed-stage');
  stage.addEventListener('dragover', e => {
    if (edDrag && edDrag.kind === 'text') e.preventDefault();
  });
  stage.addEventListener('drop', e => {
    if (!edDrag || edDrag.kind !== 'text') return;
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    edAddText(edDrag.id, {
      t: edPlayhead,
      x: (e.clientX - r.left) / r.width * 100,
      y: (e.clientY - r.top) / r.height * 100,
    });
    edDragEnd();
  });
  stage.addEventListener('pointerdown', e => {
    if (e.target === stage || e.target.tagName === 'VIDEO') edSelect(null);
  });
}
document.addEventListener('DOMContentLoaded', edWireStageDrop);
