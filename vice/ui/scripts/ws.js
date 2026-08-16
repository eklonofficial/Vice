'use strict';
// ws.js: WebSocket connect + message dispatch

// ═══════════════════════════════════════════════════════════════════
// WebSocket
// ═══════════════════════════════════════════════════════════════════
function connectWS() {
  try {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = e => { try { handleWS(JSON.parse(e.data)); } catch (_) {} };
    ws.onclose = () => setTimeout(connectWS, 3000);
    ws.onerror = () => {};
  } catch (_) {}
}

function handleWS(msg) {
  if (msg.type === 'clip_saved') {
    const exists = clips.find(c => c.slug === msg.clip.slug);
    if (exists) {
      Object.assign(exists, msg.clip);
    } else {
      recentNew.add(msg.clip.slug);
      clips.unshift(msg.clip);
      toast('New clip saved!', 'ok');
    }
    renderClips();
    renderHomeRecent();
    renderMostViewed();
    renderStats();
    renderPlaylists();
    if (typeof edOnClipsRefreshed === 'function') edOnClipsRefreshed();
  } else if (msg.type === 'clip_deleted') {
    clips = clips.filter(c => c.slug !== msg.slug);
    recentNew.delete(msg.slug);
    if (playerSlug === msg.slug) closePlayerBar();
    if (typeof edOnClipDeleted === 'function') edOnClipDeleted(msg.slug);
    renderClips();
    renderHomeRecent();
    renderMostViewed();
    renderStats();
    renderPlaylists();
  } else if (msg.type === 'playlists_changed') {
    playlists = msg.playlists || [];
    renderPlaylists();
    if (currentView === 'clips') renderClips();
  } else if (msg.type === 'clip_saving') {
    flashRecSaving();
  } else if (msg.type === 'clip_error') {
    toast(msg.error || 'Clip save failed', 'err');
  } else if (msg.type === 'status') {
    setRecStatus(msg.recording, msg.backend, msg.session_active);
    applyHotkeyAvailability(msg.hotkeys_available);
    applyRecorderState(msg);
  } else if (msg.type === 'tunnel_url') {
    tunnelUrl = msg.url;
    const ro = document.getElementById('tunnel-readout');
    const rt = document.getElementById('tunnel-readout-text');
    rt.textContent = msg.url;
    ro.classList.add('active');
    toast('Public share link ready!', 'ok');
  } else if (msg.type === 'tunnel_error') {
    tunnelUrl = null;
    const ro = document.getElementById('tunnel-readout');
    const rt = document.getElementById('tunnel-readout-text');
    if (rt) rt.textContent = msg.error || 'Public share tunnel unavailable';
    if (ro) ro.classList.remove('active');
    toast(msg.error || 'Public share tunnel unavailable', 'err');
  } else if (msg.type === 'session_start') {
    setRecStatus(true, runtimeBackend, true);
    toast(`Session recording started, ${hotkeyLabel()} marks highlights, double-tap to stop`, 'ok');
  } else if (msg.type === 'session_stop') {
    setRecStatus(false, runtimeBackend, false);
    toast('Session recording saved!', 'ok');
    fetchClips();
  } else if (msg.type === 'session_highlight') {
    const t = typeof msg.time === 'number' ? fmtSec(msg.time, true) : '?';
    toast(`Highlight marked at ${t}`, 'ok');
  } else if (msg.type === 'export_progress') {
    if (typeof edOnExportProgress === 'function') edOnExportProgress(msg);
  } else if (msg.type === 'export_done') {
    if (typeof edOnExportDone === 'function') edOnExportDone(msg);
  } else if (msg.type === 'export_error') {
    if (typeof edOnExportError === 'function') edOnExportError(msg);
  } else if (msg.type === 'editor_project_changed') {
    if (typeof edOnProjectChanged === 'function') edOnProjectChanged();
  } else if (msg.type === 'update_available') {
    if (typeof onUpdateAvailable === 'function') onUpdateAvailable(msg);
  }
}
