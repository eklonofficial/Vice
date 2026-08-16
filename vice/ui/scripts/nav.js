'use strict';
// nav.js: sidebar navigation + search

// ═══════════════════════════════════════════════════════════════════
// Navigation
// ═══════════════════════════════════════════════════════════════════
function nav(name, playlistId = null) {
  const wasEditor = currentView === 'editor';
  currentView = name;
  currentPlaylistId = name === 'clips' ? playlistId : null;
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  updateSidebarActive();
  // Stop any clip-card hover preview when switching pages
  stopActivePreview(true);
  if (wasEditor && name !== 'editor') editorLeave();
  if (name === 'editor') editorEnter();
  if (name === 'clips') renderClips();
  // The home rows size themselves to the row width, which is only
  // measurable while the view is visible.
  if (name === 'home') { renderHomeRecent(); renderMostViewed(); }
  // Audio sources change as apps start and stop playing sound, so the
  // pickers re-fetch every time settings opens (issue #98).
  if (name === 'settings') refreshAudioSources();
}

function openPlaylist(id) {
  nav('clips', id);
}

function updateSidebarActive() {
  document.querySelectorAll('.side-item').forEach(el =>
    el.classList.toggle('active', el.dataset.view === currentView && !currentPlaylistId));
  document.querySelectorAll('.side-pl-row').forEach(el =>
    el.classList.toggle('active', el.dataset.playlist === currentPlaylistId));
}

// Search filters name + game and jumps to the clips view while non-empty.
function onSearch(value) {
  searchQuery = value || '';
  if (searchQuery.trim() && currentView !== 'clips') {
    nav('clips');
  } else if (currentView === 'clips') {
    renderClips();
  }
}
