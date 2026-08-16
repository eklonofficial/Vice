'use strict';
// editor-library.js: editor left panel: clip library, effects, text presets

let edTab = 'library';
let edLibQuery = '';
let edDrag = null;       // {kind: 'clip'|'fx'|'text', id} while dragging from the panel
let edDragGhost = null;

const ED_LIB_HINTS = {
  library: 'Drag a clip onto the timeline · double-click to append',
  effects: 'Drag an effect onto a clip, or between two clips',
  text:    'Drag a title onto the preview or the T1 lane',
};

function edSetTab(tab) {
  edTab = tab;
  edRenderLibrary();
}

function edLibSearch(v) {
  edLibQuery = v || '';
  edRenderLibraryList();
}

function edRenderLibrary() {
  const tabs = ['library', 'effects', 'text'];
  document.querySelectorAll('.ed-tab').forEach(el =>
    el.classList.toggle('active', el.dataset.tab === edTab));
  const glider = document.getElementById('ed-tab-glider');
  glider.style.left = (tabs.indexOf(edTab) * 33.333) + '%';
  document.getElementById('ed-lib-searchwrap').hidden = edTab !== 'library';
  setText('ed-lib-hint', ED_LIB_HINTS[edTab]);
  edRenderLibraryList();
}

function edRenderLibraryList() {
  const scroll = document.getElementById('ed-lib-scroll');
  if (edTab === 'library') {
    const q = edLibQuery.trim().toLowerCase();
    let list = clips.filter(c => c.duration > 0);
    if (q) list = list.filter(c => `${c.name} ${c.game || ''}`.toLowerCase().includes(q));
    const cards = list.map(c => {
      const slug = escArg(c.slug);
      const media = c.thumb_url
        ? `<img src="${escAttr(c.thumb_url)}" loading="lazy" alt="" draggable="false">`
        : `<div class="ed-lib-ph">${svgEl('film', 24)}</div>`;
      const meta = [c.width ? `${c.width}×${c.height}` : '',
                    c.size ? `${(c.size / 1048576).toFixed(1)} MB` : ''].filter(Boolean).join(' · ');
      return `
      <div class="ed-lib-clip" draggable="true" title="Drag to the timeline · double-click to append"
           ondragstart="edDragStart(event, 'clip', '${slug}')" ondragend="edDragEnd()"
           ondblclick="edQuickAdd('${slug}')">
        <div class="ed-lib-thumb">
          ${media}
          ${c.game ? `<span class="ed-lib-game">${escHtml(c.game.toUpperCase())}</span>` : ''}
          <span class="ed-lib-dur">${edFmtS(c.duration)}</span>
        </div>
        <div class="ed-lib-name">${escHtml(c.name || c.slug)}</div>
        <div class="ed-lib-meta">${escHtml(meta)}</div>
      </div>`;
    }).join('');
    scroll.innerHTML = `<div class="ed-lib-grid">${cards ||
      `<div class="ed-lib-empty">${q ? 'No clips match<br><span>Try a different search.</span>'
                                     : 'No clips yet<br><span>Save some gameplay first.</span>'}</div>`}</div>`;
  } else if (edTab === 'effects') {
    scroll.innerHTML = ED_FX.map(fx => `
      <div class="ed-fx-row" draggable="true"
           ondragstart="edDragStart(event, 'fx', '${fx.id}')" ondragend="edDragEnd()">
        <span class="ed-fx-icon">${edGlyph(fx.glyph, 16)}</span>
        <div class="ed-fx-copy">
          <div class="ed-fx-name">${fx.name}</div>
          <div class="ed-fx-desc">${fx.desc}</div>
        </div>
        <span class="ed-fx-len">${fx.len.toFixed(1)}s</span>
      </div>`).join('');
  } else {
    scroll.innerHTML = ED_TEXT_PRESETS.map(p => `
      <div class="ed-text-row" draggable="true"
           ondragstart="edDragStart(event, 'text', '${p.id}')" ondragend="edDragEnd()">
        <div class="ed-text-hd">
          <span class="ed-text-kind">${p.name.toUpperCase()}</span>
          <span>${ED_FONTS[p.font].label}</span>
        </div>
        <div class="ed-text-sample" style="font-family:${ED_FONTS[p.font].stack};font-weight:${p.weight};font-size:${Math.min(20, p.size / 2.8)}px;color:${p.color};${p.font === 'display' ? 'letter-spacing:-.02em;' : ''}">${escHtml(p.sample)}</div>
      </div>`).join('');
  }
}

function edGlyph(paths, size = 14) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
}

function edDragStart(ev, kind, id) {
  edDrag = { kind, id };
  ev.dataTransfer.effectAllowed = 'copy';
  ev.dataTransfer.setData('text/plain', `vice-ed:${kind}:${id}`);

  const ghost = document.createElement('div');
  if (kind === 'clip') {
    const c = clips.find(x => x.slug === id);
    ghost.className = 'clip-drag-ghost';
    ghost.innerHTML = c && c.thumb_url
      ? `<img src="${escAttr(c.thumb_url)}" alt=""><span class="clip-drag-ghost-name">${escHtml(c.name || id)}</span>`
      : `<span class="clip-drag-ghost-ph">${svgEl('film', 14)}</span><span class="clip-drag-ghost-name">${escHtml(id)}</span>`;
  } else {
    const label = kind === 'fx' ? edFx(id).name : ED_TEXT_PRESETS.find(x => x.id === id).name;
    ghost.className = 'clip-drag-ghost';
    ghost.innerHTML = `<span class="clip-drag-ghost-name">${escHtml(label)}</span>`;
  }
  document.body.appendChild(ghost);
  edDragGhost = ghost;
  ev.dataTransfer.setDragImage(ghost, 24, 20);
}

function edDragEnd() {
  edDrag = null;
  if (edDragGhost) { edDragGhost.remove(); edDragGhost = null; }
  edClearDropHints();
}
