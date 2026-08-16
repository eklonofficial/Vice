'use strict';
// init.js: DOMContentLoaded bootstrap

// The splash covers the first paint so the grid, stats and playlists are not
// watched popping into place. It is dismissed once the first round of data
// has rendered, and on a timer regardless so a slow daemon can never leave
// the window stuck behind it.
function hideBoot() {
  const boot = document.getElementById('boot');
  if (!boot || boot.classList.contains('done')) return;
  // Two frames: the render triggered by the data has to paint underneath
  // before the cover comes off, or the pop-in just happens in view.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    boot.classList.add('done');
    setTimeout(() => {
      boot.remove();
      // Only once the splash is gone and the grid has settled, so the
      // compositor probe measures the real UI and not the boot animation.
      initEffects();
    }, 600);
  }));
}

// ═══════════════════════════════════════════════════════════════════
// Bootstrap
// ═══════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  // vice-app already knows compositing failed when it passes sw=1, so drop the
  // effects before the first paint. Everything else waits for initEffects().
  if (IS_SOFTWARE_RENDER) document.documentElement.classList.add('perf-low');

  // Theme: load before first paint of swatches/colors stick
  const savedTheme = localStorage.getItem('vice-theme') || 'blue';
  setTheme(savedTheme, /*persist*/false);

  renderGreeting();
  populateHomeFromCfg();
  syncFormFromCfg();
  renderClips();
  renderHomeRecent();
  renderMostViewed();
  renderStats();
  renderPlaylists();

  if (IS_NATIVE) document.getElementById('quit-row').style.display = 'flex';

  // Trim modal video event hooks
  const tvid = document.getElementById('trim-video');
  tvid.addEventListener('loadedmetadata', onTrimVideoMeta);
  tvid.addEventListener('timeupdate', onTrimTimeUpdate);
  tvid.addEventListener('ended', onTrimVideoEnded);

  // Trim handle dragging (mouse + touch)
  document.getElementById('tl-h-start').addEventListener('mousedown',  e => { dragging = 'start'; e.preventDefault(); });
  document.getElementById('tl-h-end').addEventListener('mousedown',    e => { dragging = 'end';   e.preventDefault(); });
  document.getElementById('tl-h-start').addEventListener('touchstart', e => { dragging = 'start'; e.preventDefault(); }, {passive:false});
  document.getElementById('tl-h-end').addEventListener('touchstart',   e => { dragging = 'end';   e.preventDefault(); }, {passive:false});
  document.addEventListener('mousemove', e => onTrimDragMove(e.clientX));
  document.addEventListener('touchmove', e => { if (e.touches[0]) onTrimDragMove(e.touches[0].clientX); }, {passive:true});
  document.addEventListener('mouseup',  () => { dragging = null; });
  document.addEventListener('touchend', () => { dragging = null; });
  document.getElementById('timeline').addEventListener('click', e => {
    if (dragging || !trimTotal) return;
    const rect = e.currentTarget.getBoundingClientRect();
    document.getElementById('trim-video').currentTime =
      ((e.clientX - rect.left) / rect.width) * trimTotal;
  });

  // Shared playback element (viewer modal + mini player bar)
  initPlayer();

  // New playlist modal: live tile preview + Enter to create
  document.getElementById('npl-emoji').addEventListener('input', renderNplPicker);
  document.getElementById('npl-name').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); submitPlaylistModal(); }
  });

  // Playback failure surfacing (issue #79): decode errors used to leave a
  // silent grey rectangle. The H.264 probe tells apart "this clip broke"
  // from "this WebEngine build can never play clips" (PyPI wheels ship
  // without proprietary codecs).
  nativeLog(`h264 decode supported: ${H264_SUPPORTED}`);
  if (!H264_SUPPORTED && IS_NATIVE) {
    document.getElementById('codec-banner').hidden = false;
  }
  wireVideoErrorOverlay('viewer-video', 'viewer-video-error', 'viewer-video-error-msg');
  wireVideoErrorOverlay('trim-video',   'trim-video-error',   'trim-video-error-msg');

  // Card hover preview failure handling lives in clips.js
  // (attachPreviewFailureHandlers): media events have no capture/bubble
  // path to ancestors, so per-element handlers are attached on render.

  // Settings rail navigation: scroll target into view
  document.addEventListener('click', e => {
    const rail = e.target.closest('.rail-item');
    if (!rail) return;
    document.querySelectorAll('.rail-item').forEach(r => r.classList.remove('active'));
    rail.classList.add('active');
    const target = document.querySelector(`[data-section="${rail.dataset.rail}"]`);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // Initial backend data, chained because order matters
  fetchConfig()
    .then(() => Promise.all([fetchClips(), fetchPlaylists(), fetchStatus()]))
    .catch(() => {})
    .finally(hideBoot);
  setTimeout(hideBoot, 8000);
  connectWS();

  // First-run tutorial. The seen flag is stored server-side because the
  // native window's localStorage does not survive restarts on every
  // QtWebEngine build, which made the tutorial reappear every launch.
  if (!localStorage.getItem('vice_tutorial_shown')) {
    fetch('/api/app-state')
      .then(r => r.json())
      .then(s => {
        if (s.tutorial_seen) {
          localStorage.setItem('vice_tutorial_shown', '1');
        } else {
          document.getElementById('tutorial-modal').classList.remove('hidden');
        }
      })
      .catch(() => document.getElementById('tutorial-modal').classList.remove('hidden'));
  }
});
