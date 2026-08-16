'use strict';
// helpers.js: toast, fmt helpers, escape, copy uninstall cmd

// ═══════════════════════════════════════════════════════════════════
// Toasts / helpers
// ═══════════════════════════════════════════════════════════════════
function toast(msg, type = 'ok') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `${svgEl(type === 'ok' ? 'checkCircle2' : 'alertCircle', 14)} ${escHtml(msg)}`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}
function fmtSec(s, short = false) {
  if (s < 60) return short ? `${s.toFixed(s % 1 ? 1 : 0)}s` : `${s} s`;
  const m = Math.floor(s/60), r = s % 60;
  if (short) return `${m}:${String(Math.floor(r)).padStart(2,'0')}`;
  return r === 0 ? `${m} min` : `${m}:${String(r).padStart(2,'0')} min`;
}
function toTC(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), se = s%60;
  const ms = String(Math.round((se%1)*1000)).padStart(3,'0');
  return `${pad(h)}:${pad(m)}:${pad(Math.floor(se))}.${ms}`;
}
function pad(n) { return String(n).padStart(2,'0'); }
function escAttr(s) { return String(s).replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;'); }
function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
// For a value going into a single-quoted JS string inside an inline handler
// attribute. escAttr alone is not enough: the parser decodes &#39; back to an
// apostrophe before the JS is parsed, so a clip named "Bob's clip" closed the
// string and killed every handler on the card (#138). Escape for JS first.
function escArg(s) { return escAttr(String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'")); }
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }

// Forward a debug message to both the browser console and (in native mode)
// the Python log, so vice-app --debug captures JS events in one timeline.
function nativeLog(msg) {
  try { console.log('[vice]', msg); } catch (_) {}
  try {
    if (IS_NATIVE && window.pywebview && window.pywebview.api && window.pywebview.api.log_debug) {
      window.pywebview.api.log_debug(String(msg));
    }
  } catch (_) {}
}

// Map a MediaError to something a log reader can act on.
function mediaErrorName(err) {
  const names = { 1: 'ABORTED', 2: 'NETWORK', 3: 'DECODE', 4: 'SRC_NOT_SUPPORTED' };
  return err ? (names[err.code] || `code ${err.code}`) : 'unknown';
}

function videoFailureMessage() {
  return !H264_SUPPORTED
    ? 'This Qt WebEngine build has no H.264 decoder, so clips cannot play inside Vice. The file itself is fine.'
    : 'Vice could not play this clip in the app window. The file itself is most likely fine.';
}

// True when a clip is H.265 and this engine can't decode it, so playback needs
// the daemon's H.264 preview proxy.
function clipNeedsProxy(clip) {
  const vc = ((clip && clip.vcodec) || '').toLowerCase();
  return (vc === 'hevc' || vc === 'h265') && !HEVC_SUPPORTED;
}

// Source URL to feed a <video> for a clip: the raw file, or the proxy when the
// codec can't play in-app. video_url already carries a ?v= token.
function playbackUrl(clip) {
  if (!clip || !clip.video_url) return (clip && clip.video_url) || '';
  return clipNeedsProxy(clip) ? clip.video_url + '&proxy=1' : clip.video_url;
}

// Show a "preparing preview" overlay while the daemon transcodes the H.264
// proxy on first open, so a proxied clip doesn't look frozen.
function setVideoPreparing(videoEl, overlayId, needsProxy) {
  const ov = document.getElementById(overlayId);
  if (!ov) return;
  if (!needsProxy) { ov.hidden = true; return; }
  ov.hidden = false;
  const hide = () => { ov.hidden = true; };
  videoEl.addEventListener('loadeddata', hide, { once: true });
  videoEl.addEventListener('canplay', hide, { once: true });
  videoEl.addEventListener('error', hide, { once: true });
}

// Wire a player <video> to an error overlay. Decode failures used to be
// completely silent: the element just stayed a grey rectangle (issue #79).
// Two failure shapes exist. A broken file fires `error`. A WebEngine build
// without the needed video codec does NOT: the audio track plays normally
// and the video track is silently dropped, leaving videoWidth at 0. Every
// Vice clip has a video track, so videoWidth 0 after load means failure.
function wireVideoErrorOverlay(videoId, overlayId, msgId) {
  const vid = document.getElementById(videoId);
  const overlay = document.getElementById(overlayId);
  const fail = why => {
    nativeLog(`video error: ${videoId} ${why} h264=${H264_SUPPORTED} src=${(vid.currentSrc || '').slice(-60)}`);
    setText(msgId, videoFailureMessage());
    overlay.hidden = false;
  };
  vid.addEventListener('error', () => {
    // Clearing src on modal close also fires `error`; not a failure.
    if (!vid.getAttribute('src')) return;
    fail(mediaErrorName(vid.error));
  });
  vid.addEventListener('loadeddata', () => {
    if (!vid.getAttribute('src')) return;
    if (vid.videoWidth === 0) fail('NO_VIDEO_TRACK');
    else overlay.hidden = true;
  });
  vid.addEventListener('loadstart', () => { if (vid.getAttribute('src')) overlay.hidden = true; });
}

// Copy `text` to the clipboard. Resolves to true on success, false on failure.
// In the native (QtWebEngine) window we go through the pywebview bridge into
// wl-copy/xclip/xsel, the in-page Clipboard API has crashed the render
// process on http:// origins. In browser mode we try the async API first
// and fall back to execCommand. Every branch is logged via nativeLog.
async function copyToClipboard(text) {
  const len = (text || '').length;
  const bridge = !!(IS_NATIVE && window.pywebview && window.pywebview.api && window.pywebview.api.copy_to_clipboard);
  nativeLog(`copyToClipboard: len=${len} IS_NATIVE=${IS_NATIVE} bridge=${bridge}`);
  if (!text) return false;
  if (bridge) {
    try {
      const ok = await window.pywebview.api.copy_to_clipboard(String(text));
      nativeLog(`copyToClipboard: bridge returned ${ok}`);
      return !!ok;
    } catch (err) {
      nativeLog(`copyToClipboard: bridge threw ${err && err.message ? err.message : err}`);
    }
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      nativeLog('copyToClipboard: async API ok');
      return true;
    }
  } catch (err) {
    nativeLog(`copyToClipboard: async API threw ${err && err.message ? err.message : err}`);
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand('copy');
    ta.remove();
    nativeLog(`copyToClipboard: execCommand ok=${ok}`);
    return ok;
  } catch (err) {
    nativeLog(`copyToClipboard: execCommand threw ${err && err.message ? err.message : err}`);
    return false;
  }
}

// Global renderer-error listeners so a page-level crash leaves a breadcrumb.
window.addEventListener('error', ev => {
  nativeLog(`window.error: ${ev.message} at ${ev.filename}:${ev.lineno}:${ev.colno}`);
});
window.addEventListener('unhandledrejection', ev => {
  const r = ev.reason;
  nativeLog(`unhandledrejection: ${r && r.message ? r.message : r}`);
});

function copyUninstallCmd() {
  copyToClipboard('vice uninstall').then(ok => {
    if (ok) toast('Command copied, paste it into a terminal', 'ok');
    else showManualCopyModal('vice uninstall');
  });
}
