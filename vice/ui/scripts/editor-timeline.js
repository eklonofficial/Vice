'use strict';
// editor-timeline.js: ruler, lanes, items, drag/trim/snap, junctions, menus

const ED_RAIL = 54;
let edDropHint = null;   // {trackId, t, w} or {trackId, junctionX}
let edTlDragging = false;

function edZoom(factor) {
  edPps = Math.max(ED_PPS_MIN, Math.min(ED_PPS_MAX, edPps * factor));
  edRenderTimeline();
}

function edFit() {
  const scroll = document.getElementById('ed-tl-scroll');
  const w = scroll ? scroll.clientWidth - ED_RAIL - 20 : 900;
  edPps = Math.max(ED_PPS_MIN, Math.min(ED_PPS_MAX, w / Math.max(10, edEnd() + 2)));
  edRenderTimeline();
}

function edTotalSec() { return Math.max(edEnd() + 30, 90); }

function edSeek(t) {
  edPlayhead = Math.max(0, Math.min(edTotalSec(), t));
  edUpdatePlayheadDom();
  edUpdateTimeReadout();
  edRenderPreviewFrame(true);
}

function edUpdatePlayheadDom() {
  const ph = document.getElementById('ed-playhead');
  const cur = document.getElementById('ed-ruler-cursor');
  if (ph) ph.style.left = (ED_RAIL + edPlayhead * edPps - 0.75) + 'px';
  if (cur) cur.style.left = (edPlayhead * edPps - 4.5) + 'px';
}

// ═══════════════════════════════════════════════════════════════════
// Render
// ═══════════════════════════════════════════════════════════════════
function edWaveBars(seed, n) {
  const out = [];
  let s = seed * 9301 + 49297;
  for (let i = 0; i < n; i++) {
    s = (s * 9301 + 49297) % 233280;
    out.push(0.18 + 0.82 * (s / 233280));
  }
  return out;
}

function edWaveSeed(slug) {
  let h = 0;
  for (let i = 0; i < (slug || '').length; i++) h = (h * 31 + slug.charCodeAt(i)) % 9973;
  return h + 3;
}

function edItemHTML(it) {
  const w = Math.max(8, it.dur * edPps);
  const left = it.start * edPps;
  const sel = edSel === it.id ? ' selected' : '';
  const miss = it.clipId && edMissing.has(it.clipId) ? ' missing' : '';
  const base = `data-item="${escAttr(it.id)}" style="left:${left}px;width:${w}px"`;
  const c = it.clipId ? edClipOf(it) : null;
  const name = c ? (c.name || it.clipId) : (it.clipId || '');

  if (it.kind === 'audio') {
    const bars = edWaveBars(edWaveSeed(it.clipId), Math.max(16, Math.round(it.dur * 3)));
    const rects = bars.map((b, i) =>
      `<rect x="${i * 3}" y="${(10 - b * 8).toFixed(2)}" width="2" height="${(b * 16).toFixed(2)}" rx="1" fill="currentColor"/>`).join('');
    return `<div class="ed-item ed-item-audio${sel}${miss}" ${base}>
      <svg class="ed-wave" viewBox="0 0 ${bars.length * 3} 20" preserveAspectRatio="none">${rects}</svg>
      <span class="ed-item-name">${escHtml(name)}</span>
      <div class="ed-handle l" data-handle="l"><div class="ed-handle-bar"></div></div>
      <div class="ed-handle r" data-handle="r"><div class="ed-handle-bar"></div></div>
    </div>`;
  }
  if (it.kind === 'text') {
    return `<div class="ed-item ed-item-text${sel}" ${base}>
      <span class="ed-item-type-ic">${svgEl('type', 10)}</span>
      <span class="ed-item-name">${escHtml(it.text || '')}</span>
      <div class="ed-handle l" data-handle="l"><div class="ed-handle-bar"></div></div>
      <div class="ed-handle r" data-handle="r"><div class="ed-handle-bar"></div></div>
    </div>`;
  }
  // Lazy like the library list: a long timeline scrolls well past its own
  // width, and a thumb costs about 900 KB once decoded.
  const thumb = c && c.thumb_url ? `<img src="${escAttr(c.thumb_url)}" loading="lazy" alt="" draggable="false">` : '';
  return `<div class="ed-item ed-item-clip${sel}${miss}" ${base}>
    ${thumb}<div class="ed-item-shade"></div>
    <div class="ed-item-hd">
      <span class="ed-item-name">${escHtml(name)}</span>
      <span class="ed-item-dur">${edFmtS(it.dur)}</span>
    </div>
    ${it.muted ? `<span class="ed-item-mute" title="Audio detached">${svgEl('volumeX', 10)}</span>` : ''}
    <div class="ed-handle l" data-handle="l"><div class="ed-handle-bar"></div></div>
    <div class="ed-handle r" data-handle="r"><div class="ed-handle-bar"></div></div>
  </div>`;
}

// Junction markers appear where a clip with a transition abuts (or follows)
// the previous item on a video lane.
// Both the marker and its editing pill are rendered up front and toggled by
// a class. Swapping innerHTML on hover made the element resize under the
// pointer, which retriggered hover and left the buttons unclickable.
function edJunctionHTML(it) {
  const fx = edFx(it.trans.fx);
  return `<div class="ed-junction" data-junction="${escAttr(it.id)}" style="left:${it.start * edPps}px">
    <div class="ed-junction-mark" title="${escAttr(fx.name)} · ${it.trans.len.toFixed(1)}s · click to edit">
      <div class="ed-junction-stem"></div>
      <div class="ed-junction-dot">${edGlyph(fx.glyph, 8)}</div>
      <div class="ed-junction-stem"></div>
    </div>
    <div class="ed-junction-pill">
      <span class="fx-ic">${edGlyph(fx.glyph, 11)}</span>
      <span class="fx-name">${escHtml(fx.name)}</span>
      <button class="ed-junction-btn" data-bump="-0.1" title="Shorter">−</button>
      <span class="fx-len">${it.trans.len.toFixed(1)}s</span>
      <button class="ed-junction-btn" data-bump="0.1" title="Longer">+</button>
      <button class="ed-junction-btn rm" data-rm="1" title="Remove transition">×</button>
    </div>
  </div>`;
}

function edRenderTimeline() {
  if (!edProject || currentView !== 'editor') return;
  const totalSec = edTotalSec();
  const totalW = totalSec * edPps;
  const minor = edPps >= 10 ? 1 : 5;
  const labelEvery = edPps >= 10 ? 5 : 15;

  let ticks = '';
  for (let s = 0; s <= totalSec; s += minor) {
    const major = s % labelEvery === 0;
    ticks += `<div class="ed-tick${major ? ' major' : ''}" style="left:${s * edPps}px">
      <div class="ed-tick-mark"></div>
      ${major ? `<span class="ed-tick-label">${edFmtS(s)}</span>` : ''}
    </div>`;
  }

  const rows = edProject.tracks.map(tr => {
    const laneItems = edItems(tr.id);
    const items = laneItems.map(edItemHTML).join('');
    const junctions = tr.type === 'video'
      ? laneItems.filter(i => i.trans).map(edJunctionHTML).join('') : '';
    return `<div class="ed-track-row">
      <div class="ed-rail ${tr.type}" data-rail="${escAttr(tr.id)}" title="Right-click for track options">
        <span class="ed-rail-label">${escHtml(tr.label)}</span>
        <span class="ed-rail-type">${tr.type.slice(0, 3).toUpperCase()}</span>
      </div>
      <div class="ed-lane ${tr.type}" data-lane="${escAttr(tr.id)}" style="width:${totalW}px">
        ${items}${junctions}
      </div>
    </div>`;
  }).join('');

  document.getElementById('ed-tl-canvas').innerHTML = `
    <div class="ed-ruler-row">
      <div class="ed-rail-corner"></div>
      <div id="ed-ruler" style="width:${totalW}px">
        ${ticks}
        <div id="ed-ruler-cursor"></div>
      </div>
    </div>
    ${rows}
    <div id="ed-playhead"></div>`;
  document.getElementById('ed-tl-canvas').style.width = (ED_RAIL + totalW) + 'px';

  edWireTimeline();
  edUpdatePlayheadDom();
  edUpdateTimeReadout();
  edRenderToolbar();
  edRenderInspector();
}

function edRenderTimelineSelection() {
  document.querySelectorAll('#ed-tl-canvas .ed-item').forEach(el =>
    el.classList.toggle('selected', el.dataset.item === edSel));
  edRenderToolbar();
}

function edRenderToolbar() {
  const it = edSelItem();
  const canSplit = it && edPlayhead > it.start + 0.2 && edPlayhead < it.start + it.dur - 0.2;
  document.getElementById('ed-btn-split').disabled = !canSplit;
  document.getElementById('ed-btn-detach').disabled = !(it && it.kind === 'clip' && !it.muted);
  document.getElementById('ed-btn-dup').disabled = !it;
  document.getElementById('ed-btn-del').disabled = !it;
  setText('ed-pps-readout', `${Math.round(edPps)} px/s`);
}

function edUpdateTimeReadout() {
  const el = document.getElementById('ed-time');
  if (el) el.innerHTML = `${edFmt(edPlayhead)} <span class="dim">/ ${edFmt(edEnd())}</span>`;
  const tb = document.getElementById('ed-btn-split');
  if (tb) edRenderToolbar();
}

// ═══════════════════════════════════════════════════════════════════
// Wiring: scrub, item drag/trim, lane drops, menus, junction pills
// ═══════════════════════════════════════════════════════════════════
function edWireTimeline() {
  const ruler = document.getElementById('ed-ruler');
  ruler.addEventListener('pointerdown', e => {
    edCapture(ruler, e);
    const rect = ruler.getBoundingClientRect();
    const upd = ev => edSeek((ev.clientX - rect.left) / edPps);
    upd(e);
    const up = () => {
      ruler.removeEventListener('pointermove', upd);
      ruler.removeEventListener('pointerup', up);
    };
    ruler.addEventListener('pointermove', upd);
    ruler.addEventListener('pointerup', up);
  });

  document.querySelectorAll('#ed-tl-canvas .ed-rail').forEach(rail => {
    rail.addEventListener('contextmenu', e => edOpenTrackMenu(e, rail.dataset.rail));
  });

  document.querySelectorAll('#ed-tl-canvas .ed-lane').forEach(lane => {
    const trackId = lane.dataset.lane;
    lane.addEventListener('pointerdown', e => {
      if (e.target === lane) edSelect(null);
    });
    lane.addEventListener('contextmenu', e => {
      if (e.target === lane) edOpenTrackMenu(e, trackId);
    });
    lane.addEventListener('dragover', e => edLaneDragOver(e, lane, trackId));
    lane.addEventListener('dragleave', () => {
      if (edDropHint && edDropHint.trackId === trackId) edClearDropHints();
    });
    lane.addEventListener('drop', e => edLaneDrop(e, lane, trackId));

    lane.querySelectorAll('.ed-item').forEach(el => {
      const id = el.dataset.item;
      el.addEventListener('pointerdown', e => edItemPointerDown(e, el, id));
      el.addEventListener('contextmenu', e => edOpenItemMenu(e, id));
      if (el.classList.contains('ed-item-clip')) {
        el.addEventListener('dragover', e => {
          if (edDrag && edDrag.kind === 'fx' && !edNearJunction(e, el, id)) {
            e.preventDefault(); e.stopPropagation();
            el.classList.add('fx-target');
          }
        });
        el.addEventListener('dragleave', () => el.classList.remove('fx-target'));
        el.addEventListener('drop', e => {
          if (edDrag && edDrag.kind === 'fx' && !edNearJunction(e, el, id)) {
            e.preventDefault(); e.stopPropagation();
            el.classList.remove('fx-target');
            edApplyFx(id, edDrag.id);
            edDragEnd();
          }
        });
      }
    });

    lane.querySelectorAll('.ed-junction').forEach(j =>
      edWireJunction(j, j.dataset.junction));
  });
}

// An fx drop close to the boundary between two abutting clips targets the
// junction (transition INTO the right clip) rather than the hovered clip.
function edNearJunction(e, el, itemId) {
  const it = edItem(itemId);
  if (!it) return false;
  const rect = el.getBoundingClientRect();
  const px = e.clientX - rect.left;
  const { prevEnd, nextStart } = edLimits(it);
  return (px < 16 && Math.abs(prevEnd - it.start) < 0.11 && it.start > 0.05)
      || (px > rect.width - 16 && Math.abs(nextStart - (it.start + it.dur)) < 0.11);
}

function edItemPointerDown(e, el, id) {
  if (e.button !== 0) return;
  const it = edItem(id);
  if (!it) return;
  e.stopPropagation();
  edSelect(id);

  const handle = e.target.closest('[data-handle]');
  edCapture(el, e);
  edTlDragging = true;

  if (handle) {
    edTrimDrag(e, el, it, handle.dataset.handle);
    return;
  }

  const sx = e.clientX, sy = e.clientY, os = it.start;
  const tr = edProject.tracks.find(t => t.id === it.trackId);
  let moved = false, targetTrack = it.trackId, targetStart = it.start;

  const lanes = [...document.querySelectorAll('#ed-tl-canvas .ed-lane')]
    .filter(l => {
      const lt = edProject.tracks.find(t => t.id === l.dataset.lane);
      return lt && lt.type === tr.type;
    });

  const mv = ev => {
    if (Math.abs(ev.clientX - sx) > 3 || Math.abs(ev.clientY - sy) > 8) moved = true;
    if (!moved) return;
    targetStart = edSnapTime(Math.max(0, os + (ev.clientX - sx) / edPps), id);
    for (const l of lanes) {
      const r = l.getBoundingClientRect();
      if (ev.clientY >= r.top - 3 && ev.clientY <= r.bottom + 3) targetTrack = l.dataset.lane;
    }
    el.style.left = (targetStart * edPps) + 'px';
    if (targetTrack !== it.trackId) el.style.opacity = '0.65';
    else el.style.opacity = '';
  };
  const up = () => {
    el.removeEventListener('pointermove', mv);
    el.removeEventListener('pointerup', up);
    edTlDragging = false;
    if (!moved) return;
    edBegin();
    it.start = targetStart;
    it.trackId = targetTrack;
    edResolve(it);
    edCommit();
  };
  el.addEventListener('pointermove', mv);
  el.addEventListener('pointerup', up);
}

function edTrimDrag(e, el, it, side) {
  const sx = e.clientX, os = it.start, od = it.dur, oo = it.offset || 0;
  const { prevEnd, nextStart } = edLimits(it);
  const srcDur = it.clipId ? edSourceDur(it) : Infinity;
  const maxD = Math.min(srcDur - oo, nextStart - os);
  let final = null;

  const mv = ev => {
    const dt = (ev.clientX - sx) / edPps;
    if (side === 'l') {
      let d = edSnapTime(os + dt, it.id) - os;
      d = Math.max(Math.max(-oo, prevEnd - os), Math.min(od - 0.5, d));
      final = { start: edRound(os + d), dur: edRound(od - d), offset: edRound(oo + d) };
      el.style.left = (final.start * edPps) + 'px';
      el.style.width = Math.max(8, final.dur * edPps) + 'px';
    } else {
      let nd = edSnapTime(os + od + dt, it.id) - os;
      nd = Math.max(0.5, Math.min(maxD, nd));
      final = { dur: edRound(nd) };
      el.style.width = Math.max(8, final.dur * edPps) + 'px';
    }
  };
  const up = () => {
    el.removeEventListener('pointermove', mv);
    el.removeEventListener('pointerup', up);
    edTlDragging = false;
    if (!final) return;
    edBegin();
    Object.assign(it, final);
    edCommit();
  };
  el.addEventListener('pointermove', mv);
  el.addEventListener('pointerup', up);
}

// ── Library drops onto lanes ──
function edLaneDragOver(e, lane, trackId) {
  if (!edDrag) return;
  const tr = edProject.tracks.find(t => t.id === trackId);
  const rect = lane.getBoundingClientRect();
  const px = e.clientX - rect.left;

  if (edDrag.kind === 'fx') {
    if (tr.type !== 'video') return;
    const j = edJunctionAt(trackId, px);
    if (j) {
      e.preventDefault();
      edSetDropHint({ trackId, junctionX: j.x });
    } else if (edDropHint && edDropHint.junctionX !== undefined) {
      edClearDropHints();
    }
    return;
  }
  if (edDrag.kind === 'text') {
    // Titles land on the text track wherever they are dropped.
    e.preventDefault();
    const tt = edProject.tracks.find(x => x.type === 'text');
    edSetDropHint({ trackId: tt.id, t: edSnapTime(px / edPps), w: 4 * edPps });
    return;
  }
  if (tr.type === 'text') return;
  e.preventDefault();
  const dur = (clips.find(c => c.slug === edDrag.id) || { duration: 4 }).duration;
  edSetDropHint({ trackId, t: edSnapTime(px / edPps), w: dur * edPps });
}

function edLaneDrop(e, lane, trackId) {
  const d = edDrag;
  if (!d) return;
  const tr = edProject.tracks.find(t => t.id === trackId);
  const rect = lane.getBoundingClientRect();
  const px = e.clientX - rect.left;

  if (d.kind === 'fx') {
    if (tr.type !== 'video') return;
    const j = edJunctionAt(trackId, px);
    if (j) { e.preventDefault(); edApplyFx(j.id, d.id); }
    edDragEnd();
    return;
  }
  const t = edSnapTime(px / edPps);
  if (d.kind === 'text') {
    e.preventDefault();
    edAddText(d.id, { t });
    edDragEnd();
    return;
  }
  if (tr.type === 'text') return;
  e.preventDefault();
  edAddClip(d.id, trackId, t, tr.type === 'audio');
  edDragEnd();
}

function edJunctionAt(trackId, px) {
  const sorted = edItems(trackId).filter(i => i.kind === 'clip');
  let best = null;
  for (let k = 1; k < sorted.length; k++) {
    const a = sorted[k - 1], b = sorted[k];
    if (Math.abs(a.start + a.dur - b.start) < 0.11) {
      const jx = b.start * edPps;
      if (Math.abs(px - jx) < 16 && (!best || Math.abs(px - jx) < Math.abs(px - best.x))) {
        best = { x: jx, id: b.id };
      }
    }
  }
  return best;
}

function edSetDropHint(hint) {
  edClearDropHints();
  edDropHint = hint;
  const lane = document.querySelector(`.ed-lane[data-lane="${CSS.escape(hint.trackId)}"]`);
  if (!lane) return;
  const el = document.createElement('div');
  if (hint.junctionX !== undefined) {
    el.className = 'ed-junction-hint';
    el.style.left = (hint.junctionX - 3) + 'px';
  } else {
    el.className = 'ed-drop-hint';
    el.style.left = (hint.t * edPps) + 'px';
    el.style.width = hint.w + 'px';
  }
  lane.appendChild(el);
}

function edClearDropHints() {
  edDropHint = null;
  document.querySelectorAll('.ed-drop-hint, .ed-junction-hint').forEach(el => el.remove());
  document.querySelectorAll('.ed-item.fx-target').forEach(el => el.classList.remove('fx-target'));
}

// ── Junction pill ──
// The pill is a child of the marker, so moving the pointer onto it keeps the
// marker hovered and nothing is re-rendered mid-click.
function edWireJunction(el, itemId) {
  const close = () => el.classList.remove('open');
  el.addEventListener('pointerdown', e => {
    e.stopPropagation();
    el.classList.add('open');
  });
  el.addEventListener('contextmenu', e => { e.preventDefault(); e.stopPropagation(); });
  el.addEventListener('mouseenter', () => el.classList.add('open'));
  el.addEventListener('mouseleave', close);

  el.querySelectorAll('[data-bump]').forEach(btn => btn.onclick = ev => {
    ev.preventDefault();
    ev.stopPropagation();
    const it = edItem(itemId);
    if (!it || !it.trans) return;
    const cap = Math.min(3, edRound(it.dur * 0.8));
    it.trans.len = edRound(Math.max(0.2, Math.min(cap, it.trans.len + parseFloat(btn.dataset.bump))));
    el.querySelector('.fx-len').textContent = it.trans.len.toFixed(1) + 's';
    edScheduleSave();
    edRenderPreviewFrame(true);
  });
  el.querySelector('[data-rm]').onclick = ev => {
    ev.preventDefault();
    ev.stopPropagation();
    const it = edItem(itemId);
    if (!it) return;
    close();
    edBegin();
    delete it.trans;
    edCommit();
  };
}

// ── Context menus ──
function edMenu(x, y, entries) {
  document.getElementById('ed-menu')?.remove();
  const menu = document.createElement('div');
  menu.id = 'ed-menu';
  menu.className = 'ed-menu';
  menu.innerHTML = entries.map((en, i) => en === '-'
    ? '<div class="ed-menu-sep"></div>'
    : `<button class="ed-menu-item${en.danger ? ' danger' : ''}" data-i="${i}" ${en.dis ? 'disabled' : ''}>
        <span>${escHtml(en.label)}</span>${en.kbd ? `<span class="kbd-hint">${escHtml(en.kbd)}</span>` : ''}
      </button>`).join('');
  document.body.appendChild(menu);

  menu.querySelectorAll('.ed-menu-item').forEach(btn => {
    btn.onclick = e => {
      e.stopPropagation();
      const en = entries[parseInt(btn.dataset.i, 10)];
      menu.remove();
      if (en && !en.dis) en.fn();
    };
  });

  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - 200)) + 'px';
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8)) + 'px';
  const dismiss = e => {
    if (!menu.contains(e.target)) {
      menu.remove();
      document.removeEventListener('pointerdown', dismiss, true);
    }
  };
  setTimeout(() => document.addEventListener('pointerdown', dismiss, true), 0);
}

function edOpenItemMenu(e, id) {
  e.preventDefault();
  e.stopPropagation();
  edSelect(id);
  const it = edItem(id);
  if (!it) return;
  const canSplit = edPlayhead > it.start + 0.2 && edPlayhead < it.start + it.dur - 0.2;
  edMenu(e.clientX, e.clientY, [
    { label: 'Split at playhead', kbd: 'S', fn: edSplitSel, dis: !canSplit },
    { label: 'Detach audio', fn: edDetachAudio, dis: it.kind !== 'clip' || !!it.muted },
    '-',
    { label: 'Copy', kbd: 'Ctrl C', fn: edCopySel },
    { label: 'Paste', kbd: 'Ctrl V', fn: edPaste, dis: !edClipboard },
    { label: 'Duplicate', kbd: 'Ctrl D', fn: edDuplicateSel },
    '-',
    { label: 'Delete', kbd: 'Del', fn: edDeleteSel, danger: true },
  ]);
}

function edOpenTrackMenu(e, trackId) {
  e.preventDefault();
  e.stopPropagation();
  const tr = edProject.tracks.find(t => t.id === trackId);
  if (!tr) return;
  const removable = tr.type !== 'text'
    && !(tr.type === 'video' && edVideoTracks().length <= 1);
  edMenu(e.clientX, e.clientY, [
    { label: 'Add video track', fn: () => edAddTrack('video') },
    { label: 'Add audio track', fn: () => edAddTrack('audio') },
    { label: 'Paste', kbd: 'Ctrl V', fn: edPaste, dis: !edClipboard },
    '-',
    { label: `Remove track ${tr.label}`, fn: () => edRemoveTrack(trackId),
      danger: true, dis: !removable },
  ]);
}

function edToggleAddTrackMenu() {
  const menu = document.getElementById('ed-add-track-menu');
  menu.hidden = !menu.hidden;
  if (!menu.hidden) {
    const dismiss = e => {
      if (!menu.contains(e.target)) {
        menu.hidden = true;
        document.removeEventListener('pointerdown', dismiss, true);
      }
    };
    setTimeout(() => document.addEventListener('pointerdown', dismiss, true), 0);
  }
}
