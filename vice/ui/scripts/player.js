'use strict';
// player.js: mini player bar paired with the viewer modal

// One playback element (#viewer-video) drives both surfaces: the viewer
// modal shows it, the bar mirrors it. The bar enters at the bottom of the
// window and rises to sit just below the modal, matching its width; closing
// either surface closes both and stops playback.
let playerSlug = null;
let playerCloseTimer = null;

function playerVideo() { return document.getElementById('viewer-video'); }
function playerBarEl() { return document.getElementById('player-bar'); }
function viewerModalBox() { return document.querySelector('#viewer-modal .viewer-modal'); }

const PLAYER_ICON_PLAY  = '<svg viewBox="0 0 24 24" fill="#000" style="margin-left:2px"><path d="M7 4l13 8-13 8z"/></svg>';
const PLAYER_ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="#000"><path d="M7 4h3.5v16H7zM13.5 4H17v16h-3.5z"/></svg>';

// Fill the bar for a clip and make it visible.
function playerBind(slug) {
  const c = clips.find(x => x.slug === slug);
  if (!c) return;
  clearTimeout(playerCloseTimer);
  playerSlug = slug;
  const bar = playerBarEl();
  bar.classList.remove('closing');
  bar.style.display = 'flex';
  const img = document.getElementById('player-thumb');
  if (c.thumb_url) { img.src = c.thumb_url; img.style.visibility = ''; }
  else { img.removeAttribute('src'); img.style.visibility = 'hidden'; }
  setText('player-title', (c.name || c.slug).replace(/\.(mp4|mkv)$/i, ''));
  setText('player-game', c.game || '');
  setText('player-dur', c.duration ? fmtSec(Math.round(c.duration), true) : '0:00');
  updatePlayerUI();
  updatePlayPauseIcons();
}

function floatPlayerBar() {
  playerBarEl().classList.add('float');
  positionFloatingBar();
}

// Pin the floating bar just below the modal at the modal's width. offsetLeft
// and offsetTop are read instead of getBoundingClientRect because the modal
// animates in with a scale transform, which would skew the measured rect.
function positionFloatingBar() {
  const bar = playerBarEl();
  const modal = viewerModalBox();
  if (!modal || !bar.classList.contains('float')) return;
  bar.style.left = modal.offsetLeft + 'px';
  bar.style.right = Math.max(0, window.innerWidth - modal.offsetLeft - modal.offsetWidth) + 'px';
  const below = window.innerHeight - (modal.offsetTop + modal.offsetHeight) - 12 - bar.offsetHeight;
  bar.style.bottom = Math.max(8, below) + 'px';
}

function playerOpenViewer() {
  if (playerSlug) openViewer(playerSlug);
}

function playerToggle() {
  const vid = playerVideo();
  if (!playerSlug || !vid.getAttribute('src')) return;
  if (vid.paused) { const p = vid.play(); if (p && p.catch) p.catch(() => {}); }
  else vid.pause();
}

function playerStep(dir) {
  const i = clips.findIndex(c => c.slug === playerSlug);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= clips.length) return;
  loadViewerClip(clips[j].slug);
}
function playerPrev() { playerStep(-1); }
function playerNext() { playerStep(1); }

function playerSeek(ev) {
  const vid = playerVideo();
  if (!vid.duration) return;
  const rect = document.getElementById('player-track').getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
  vid.currentTime = ratio * vid.duration;
}

function playerShare() {
  const c = clips.find(x => x.slug === playerSlug);
  copyLink(null, c?.share_url, c?.share_is_public !== false);
}

function closePlayerBar() {
  const bar = playerBarEl();
  if (bar.style.display === 'none') return;
  const vid = playerVideo();
  vid.pause();
  vid.removeAttribute('src');
  vid.load();
  document.getElementById('viewer-modal').classList.remove('open');
  bar.classList.add('closing');
  playerCloseTimer = setTimeout(() => {
    bar.style.display = 'none';
    bar.classList.remove('closing', 'float');
    bar.style.left = bar.style.right = bar.style.bottom = '';
  }, 420);
  playerSlug = null;
  viewerSlug = null; viewerIdx = -1; viewerHighlights = [];
}

function updatePlayerUI() {
  const vid = playerVideo();
  const d = vid.duration;
  const pct = (isFinite(d) && d > 0) ? (vid.currentTime / d) * 100 : 0;
  document.getElementById('player-fill').style.width = pct + '%';
  document.getElementById('player-knob').style.left = pct + '%';
  setText('player-time', fmtSec(vid.currentTime || 0, true));
  if (isFinite(d) && d > 0) setText('player-dur', fmtSec(d, true));
  document.getElementById('viewer-playhead').style.left = pct + '%';
  document.getElementById('viewer-progress').style.width = pct + '%';
  setText('viewer-timebadge', `${fmtSec(vid.currentTime || 0, true)} / ${fmtSec(isFinite(d) ? d : 0, true)}`);
}

function updatePlayPauseIcons() {
  const paused = playerVideo().paused;
  document.getElementById('player-toggle-btn').innerHTML = paused ? PLAYER_ICON_PLAY : PLAYER_ICON_PAUSE;
  document.getElementById('viewer-play-btn').innerHTML = paused ? PLAYER_ICON_PLAY : PLAYER_ICON_PAUSE;
  document.getElementById('viewer-play-btn').classList.toggle('paused', paused);
}

function initPlayer() {
  const vid = playerVideo();
  vid.addEventListener('timeupdate', updatePlayerUI);
  vid.addEventListener('loadedmetadata', () => { updatePlayerUI(); renderViewerHighlights(); });
  vid.addEventListener('play', updatePlayPauseIcons);
  vid.addEventListener('pause', updatePlayPauseIcons);
  updatePlayPauseIcons();

  // The modal's height shifts as metadata and highlights load; keep the
  // floating bar glued to its bottom edge.
  new ResizeObserver(positionFloatingBar).observe(viewerModalBox());
  window.addEventListener('resize', positionFloatingBar);

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    const npl = document.getElementById('new-playlist-modal');
    if (!npl.classList.contains('hidden')) { closeNewPlaylistModal(); return; }
    if (document.getElementById('viewer-modal').classList.contains('open')) return;
    if (document.getElementById('trim-modal').classList.contains('open')) { closeTrim(); return; }
    if (playerSlug) closePlayerBar();
  });
}
