'use strict';
// state.js — shared mutable state + IS_NATIVE detection

// ───── State (defaults; overwritten by /api/config on init)
let cfg = {
  recording: { buffer_duration: 120, clip_duration: 20, fps: 60, encoder: 'auto',
               backend: 'auto', display: null, capture_source: null, resolution: null, capture_audio: true,
               capture_microphone: false, wf_microphone_strategy: 'prompt',
               gsr_args: '', gsr_audio_source: 'default_output' },
  hotkeys:  { clip: 'KEY_F9', clip_presets: [] },
  output:   { directory: '~/Videos/Vice' },
  sharing:  { port: 8765, cloudflare_tunnel: true },
  discord:  { enabled: true, client_id_override: null, custom_games: [] },
};
let clips     = [];
let ws        = null;
let recentNew = new Set();
let tunnelUrl = null;
let hotkeysAvailable = true;
let runtimeBackend = 'auto';
let pendingMicToggle = null;
let displayInfo = { backend: 'auto', displays: [], warning: null };
let audioSourceInfo = { sources: [], warning: null };
let currentView = 'home';
// Filled from /api/status; only used for "you are on X" copy.
let viceVersion = '';

// Playlists (server store; auto playlists from game detection + custom)
let playlists = [];
let currentPlaylistId = null;
let searchQuery = '';

// Trim state
let trimSlug  = null;
let trimS     = 0;
let trimE     = 0;
let trimTotal = 0;
let dragging  = null;

// Detect the pywebview native window. vice-app passes ?native=1 in the URL
// so we know this at module-load time — pywebview's own window.pywebview is
// only injected after DOMContentLoaded, too late to show the Quit pill on
// the first paint.
// Software-compositing mode: vice-app appends sw=1 when GPU compositing
// failed and the window relaunched in software mode. The UI drops its
// backdrop blurs and ambient effects (see .perf-low rules) because they
// are what makes software rendering feel laggy. This only covers the
// failure vice-app can see; perf.js catches the silent one by timing
// frames.
const IS_SOFTWARE_RENDER = (() => {
  try { return new URLSearchParams(location.search).get('sw') === '1'; }
  catch (_) { return false; }
})();

const IS_NATIVE = (() => {
  try {
    if (new URLSearchParams(location.search).get('native') === '1') return true;
  } catch (_) {}
  return typeof window.pywebview !== 'undefined';
})();

// Whether this engine can decode H.264 at all. Every Vice clip is H.264, but
// QtWebEngine builds without proprietary codecs (the PyPI PyQt6-WebEngine
// wheels) silently render <video> as a blank rectangle. Probed once so the
// UI can say what is wrong instead of showing grey (issue #79).
const H264_SUPPORTED = (() => {
  try {
    return document.createElement('video')
      .canPlayType('video/mp4; codecs="avc1.640028"') !== '';
  } catch (_) { return true; }
})();

// H.265/HEVC clips (NVENC/VAAPI H.265) usually can't decode in the native
// WebEngine. When they can't, the UI asks the daemon for an H.264 preview
// proxy instead of the raw file (#hevc).
const HEVC_SUPPORTED = (() => {
  try {
    const v = document.createElement('video');
    return v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"') !== '' ||
           v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') !== '';
  } catch (_) { return false; }
})();
