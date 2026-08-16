'use strict';
// viewer.js: viewer modal + highlights (CRUD, drag, color picker)

// ═══════════════════════════════════════════════════════════════════
// Viewer modal + highlights
// ═══════════════════════════════════════════════════════════════════
let viewerSlug = null, viewerIdx = -1, viewerHighlights = [];
let draggingHighlight = null;
const HIGHLIGHT_COLORS = ['#f59e0b','#ef4444','#22c55e','#3b82f6','#ec4899','#8b5cf6','#06b6d4','#f97316'];

function seekViewerTo(seconds) {
  const vid = document.getElementById('viewer-video');
  if (!vid) return;
  const target = Math.max(0, Number(seconds) || 0);
  try {
    if (typeof vid.fastSeek === 'function') vid.fastSeek(target);
    else vid.currentTime = target;
  } catch (_) { vid.currentTime = target; }
}

function openViewer(slug) {
  stopActivePreview(true);
  if (!loadViewerClip(slug)) return;
  document.getElementById('viewer-modal').classList.add('open');
  requestAnimationFrame(floatPlayerBar);
}

// Load a clip into the shared playback element and sync every label. Used
// by the viewer modal and the mini player's prev/next controls.
function loadViewerClip(slug) {
  const idx = clips.findIndex(c => c.slug === slug);
  if (idx < 0) return false;
  viewerSlug = slug; viewerIdx = idx;
  const c = clips[idx];

  setText('viewer-clip-title', (c.name || c.slug).replace(/\.(mp4|mkv)$/i, ''));
  const parts = [
    c.duration ? fmtSec(Math.round(c.duration), true) : '',
    c.width    ? `${c.width}\u00d7${c.height}`        : '',
    c.size     ? `${(c.size / 1048576).toFixed(1)} MB` : '',
  ].filter(Boolean);
  setText('viewer-clip-meta', parts.join(' · '));
  setText('viewer-count', clips.length > 1 ? `${idx+1} / ${clips.length}` : '');

  const vid = document.getElementById('viewer-video');
  const src = playbackUrl(c);
  if (vid.getAttribute('src') !== src) {
    vid.pause();
    vid.src = src;
    vid.load();
    setVideoPreparing(vid, 'viewer-video-preparing', clipNeedsProxy(c));
    // A fresh load counts as a view; reopening the viewer from the bar for
    // the clip already playing does not.
    recordClipView(slug);
  }
  const maybe = vid.play();
  if (maybe && typeof maybe.catch === 'function') maybe.catch(() => {});
  document.getElementById('viewer-progress').style.width = '0%';
  document.getElementById('viewer-playhead').style.left  = '0%';
  document.getElementById('viewer-prev').disabled = idx === 0;
  document.getElementById('viewer-next').disabled = idx === clips.length - 1;
  loadViewerHighlights(slug);
  playerBind(slug);
  return true;
}

// The bar and the viewer live and die together: closing either one stops
// playback and fades both out.
function closeViewer() {
  closePlayerBar();
}
function onViewerBackdropClick(e) { if (e.target.id === 'viewer-modal') closeViewer(); }
function viewerPrev() { playerStep(-1); }
function viewerNext() { playerStep(1); }

async function recordClipView(slug) {
  try {
    const r = await fetch(`/api/clips/${encodeURIComponent(slug)}/view`, { method: 'POST' });
    const data = await r.json();
    if (!data.ok) return;
    const c = clips.find(x => x.slug === slug);
    if (c) c.views = data.views;
    renderMostViewed();
  } catch (_) {}
}

function viewerTrim() {
  if (!viewerSlug) return;
  const c = clips.find(x => x.slug === viewerSlug);
  document.getElementById('viewer-video').pause();
  openTrim(viewerSlug, c ? c.video_url : '');
}

async function viewerDelete() {
  if (!viewerSlug) return;
  const slug = viewerSlug;
  if (!confirm('Delete this clip? This cannot be undone.')) return;
  closePlayerBar();
  await performDeleteClip(slug);
}
function viewerTimelineClick(e) {
  const vid = document.getElementById('viewer-video');
  if (!vid.duration) return;
  const rect = document.getElementById('viewer-timeline').getBoundingClientRect();
  seekViewerTo(Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * vid.duration);
}

document.addEventListener('keydown', e => {
  const open = document.getElementById('viewer-modal').classList.contains('open');
  if (!open) return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  // stopImmediatePropagation keeps the player bar's own Escape handler from
  // also firing and closing the freshly docked bar in the same keypress.
  if      (e.key === 'Escape')      { e.stopImmediatePropagation(); closeViewer(); }
  else if (e.key === 'ArrowLeft')  { viewerPrev(); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { viewerNext(); e.preventDefault(); }
  else if (e.key === 'h' || e.key === 'H') { addHighlight(); e.preventDefault(); }
});

function nextHighlightLabel() {
  const labels = viewerHighlights.map(h => h.label);
  if (!labels.includes('Highlight')) return 'Highlight';
  let n = 1;
  while (labels.includes(`Highlight ${n}`)) n++;
  return `Highlight ${n}`;
}

async function loadViewerHighlights(slug) {
  try {
    const r = await fetch(`/api/clips/${encodeURIComponent(slug)}/highlights`);
    viewerHighlights = (await r.json()).highlights || [];
  } catch (_) { viewerHighlights = []; }
  renderViewerHighlights();
}

function renderViewerHighlights() {
  const vid   = document.getElementById('viewer-video');
  const raw   = vid.duration;
  const total = (isFinite(raw) && raw > 0) ? raw : 0;

  const markersEl = document.getElementById('viewer-hl-markers');
  markersEl.innerHTML = '';
  viewerHighlights.forEach(hl => {
    const pct   = total ? (hl.time / total) * 100 : 0;
    const color = hl.color || '#f59e0b';
    const m = document.createElement('div');
    m.className = 'hl-marker';
    m.style.left = pct + '%';
    m.style.background = color;
    m.title = `${hl.label}, ${fmtSec(hl.time, true)} (drag to move)`;
    m.onpointerdown = ev => beginHighlightDrag(ev, hl, m);
    m.onclick = ev => { if (!draggingHighlight) { ev.stopPropagation(); seekViewerTo(hl.time); } };
    markersEl.appendChild(m);
  });

  renderTrimHighlights(total);

  const listEl = document.getElementById('viewer-hl-list');
  if (!viewerHighlights.length) {
    listEl.innerHTML = '<div class="viewer-hl-empty">No highlights yet, press <strong>H</strong> or click "Highlight" to mark the current timestamp</div>';
    return;
  }
  listEl.innerHTML = '';
  viewerHighlights.forEach(hl => {
    const item = document.createElement('div');
    item.className = 'hl-item';
    const color = hl.color || '#f59e0b';

    const swatchBtn = document.createElement('button');
    swatchBtn.className = 'hl-color-swatch';
    swatchBtn.style.background = color;
    swatchBtn.title = 'Change colour';
    swatchBtn.onclick = ev => { ev.stopPropagation(); showColorPicker(hl.id, color, swatchBtn); };

    const timeEl = document.createElement('span');
    timeEl.className = 'hl-time';
    timeEl.textContent = fmtSec(hl.time, true);
    timeEl.onclick = ev => { ev.stopPropagation(); seekViewerTo(hl.time); };

    const labelWrap = document.createElement('span');
    labelWrap.className = 'hl-label-wrap';
    const labelEl = document.createElement('span');
    labelEl.className = 'hl-label';
    labelEl.textContent = hl.label;
    labelEl.ondblclick = () => startHlRename(hl.id, labelEl, hl.label);
    labelWrap.appendChild(labelEl);

    const delBtn = document.createElement('button');
    delBtn.className = 'hl-del';
    delBtn.title = 'Remove';
    delBtn.innerHTML = svgEl('x', 12);
    delBtn.onclick = ev => { ev.stopPropagation(); deleteHighlight(hl.id); };

    item.append(swatchBtn, timeEl, labelWrap, delBtn);
    item.onclick = () => seekViewerTo(hl.time);
    listEl.appendChild(item);
  });
}

function dragTimeFromPointer(ev) {
  const vid = document.getElementById('viewer-video');
  const tl  = document.getElementById('viewer-timeline');
  if (!vid || !tl || !vid.duration) return null;
  const rect = tl.getBoundingClientRect();
  const x = Math.max(rect.left, Math.min(rect.right, ev.clientX));
  const ratio = Math.max(0, Math.min(1, (x - rect.left) / rect.width));
  return ratio * vid.duration;
}

function beginHighlightDrag(ev, hl, markerEl) {
  if (ev.button !== undefined && ev.button !== 0) return;
  ev.preventDefault(); ev.stopPropagation();
  const t = dragTimeFromPointer(ev);
  draggingHighlight = { id: hl.id, markerEl, startTime: hl.time, time: t ?? hl.time };
  markerEl.classList.add('dragging');

  const onMove = moveEv => {
    if (!draggingHighlight) return;
    const next = dragTimeFromPointer(moveEv);
    if (next == null) return;
    draggingHighlight.time = next;
    const item = viewerHighlights.find(h => h.id === draggingHighlight.id);
    if (item) {
      item.time = Number(next.toFixed(3));
      viewerHighlights.sort((a, b) => a.time - b.time);
      renderViewerHighlights();
    }
  };
  const onUp = async upEv => {
    window.removeEventListener('pointermove', onMove, true);
    window.removeEventListener('pointerup', onUp, true);
    window.removeEventListener('pointercancel', onUp, true);
    const cur = draggingHighlight;
    draggingHighlight = null;
    if (cur?.markerEl) cur.markerEl.classList.remove('dragging');
    if (!cur) return;
    const finalTime = dragTimeFromPointer(upEv);
    const applied = Number((finalTime ?? cur.time).toFixed(3));
    const item = viewerHighlights.find(h => h.id === cur.id);
    if (item) item.time = applied;
    viewerHighlights.sort((a, b) => a.time - b.time);
    renderViewerHighlights();
    if (!viewerSlug) return;
    try {
      await fetch(`/api/clips/${encodeURIComponent(viewerSlug)}/highlights/${encodeURIComponent(cur.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ time: applied }),
      });
      toast(`Highlight moved to ${fmtSec(applied, true)}`, 'ok');
    } catch (_) {
      const old = viewerHighlights.find(h => h.id === cur.id);
      if (old) old.time = cur.startTime;
      viewerHighlights.sort((a, b) => a.time - b.time);
      renderViewerHighlights();
      toast('Failed to move highlight', 'err');
    }
  };
  window.addEventListener('pointermove', onMove, true);
  window.addEventListener('pointerup', onUp, true);
  window.addEventListener('pointercancel', onUp, true);
}

function showColorPicker(hlId, currentColor, anchorEl) {
  document.getElementById('hl-color-picker-popup')?.remove();
  const popup = document.createElement('div');
  popup.id = 'hl-color-picker-popup';
  HIGHLIGHT_COLORS.forEach(c => {
    const dot = document.createElement('button');
    dot.className = 'hl-palette-dot';
    dot.style.background = c;
    dot.style.outline = c === currentColor ? '2px solid #fff' : 'none';
    dot.title = c;
    dot.onclick = async ev => {
      ev.stopPropagation();
      popup.remove();
      await patchHighlightColor(hlId, c);
    };
    popup.appendChild(dot);
  });
  document.body.appendChild(popup);
  const rect = anchorEl.getBoundingClientRect();
  popup.style.top  = (rect.bottom + 6) + 'px';
  popup.style.left = rect.left + 'px';
  const dismiss = ev => {
    if (!popup.contains(ev.target) && ev.target !== anchorEl) {
      popup.remove();
      document.removeEventListener('click', dismiss, true);
    }
  };
  setTimeout(() => document.addEventListener('click', dismiss, true), 0);
}

async function patchHighlightColor(id, color) {
  if (!viewerSlug) return;
  try {
    await fetch(`/api/clips/${encodeURIComponent(viewerSlug)}/highlights/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ color }),
    });
    const hl = viewerHighlights.find(h => h.id === id);
    if (hl) hl.color = color;
    renderViewerHighlights();
  } catch (_) { toast('Failed to update highlight colour', 'err'); }
}

function renderTrimHighlights(total) {
  const tl = document.getElementById('timeline');
  if (!tl) return;
  tl.querySelectorAll('.tl-hl-marker').forEach(el => el.remove());
  if (!total || viewerSlug !== trimSlug) return;
  viewerHighlights.forEach(hl => {
    const m = document.createElement('div');
    m.className = 'tl-hl-marker';
    m.style.left = ((hl.time / total) * 100) + '%';
    m.style.background = hl.color || '#f59e0b';
    m.title = hl.label;
    tl.appendChild(m);
  });
}

function startHlRename(id, labelEl, current) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'hl-rename-input';
  input.value = current;
  labelEl.replaceWith(input);
  input.focus(); input.select();
  let done = false;
  const submit = async () => {
    if (done) return; done = true;
    const val = input.value.trim() || 'Highlight';
    if (val !== current) await patchHighlight(id, val);
    else { input.replaceWith(labelEl); }
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); submit(); }
    if (e.key === 'Escape') { done = true; input.replaceWith(labelEl); }
  });
  input.addEventListener('blur', submit);
}

async function addHighlight() {
  if (!viewerSlug) return;
  const vid = document.getElementById('viewer-video');
  const time = vid.currentTime;
  const label = nextHighlightLabel();
  const color = HIGHLIGHT_COLORS[viewerHighlights.length % HIGHLIGHT_COLORS.length];
  try {
    const r = await fetch(`/api/clips/${encodeURIComponent(viewerSlug)}/highlights`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ time, label, color }),
    });
    const data = await r.json();
    if (data.ok) {
      viewerHighlights.push(data.highlight);
      viewerHighlights.sort((a, b) => a.time - b.time);
      renderViewerHighlights();
      toast(`${label} at ${fmtSec(time, true)}`, 'ok');
    }
  } catch (_) { toast('Failed to add highlight', 'err'); }
}

async function patchHighlight(id, label) {
  if (!viewerSlug) return;
  try {
    await fetch(`/api/clips/${encodeURIComponent(viewerSlug)}/highlights/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    });
    const hl = viewerHighlights.find(h => h.id === id);
    if (hl) hl.label = label;
    renderViewerHighlights();
  } catch (_) { toast('Failed to rename highlight', 'err'); }
}

async function deleteHighlight(id) {
  if (!viewerSlug) return;
  try {
    await fetch(`/api/clips/${encodeURIComponent(viewerSlug)}/highlights/${encodeURIComponent(id)}`, { method: 'DELETE' });
    viewerHighlights = viewerHighlights.filter(h => h.id !== id);
    renderViewerHighlights();
  } catch (_) { toast('Failed to delete highlight', 'err'); }
}
