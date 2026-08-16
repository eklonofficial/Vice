'use strict';
// trim.js: trim modal: video + draggable timeline handles

// ═══════════════════════════════════════════════════════════════════
// Trim modal
// ═══════════════════════════════════════════════════════════════════
let trimPreviewing = false;

function openTrim(slug, videoUrl) {
  trimSlug = slug;
  document.getElementById('trim-title').textContent = `Trim · ${slug}`;
  document.getElementById('trim-status').textContent = 'Loops the selection · cut bounds are lossless';
  const btn = document.getElementById('trim-save-btn');
  btn.disabled = false;
  btn.innerHTML = `${svgEl('scissors')} Save trim`;
  setTrimPreview(false);
  const vid = document.getElementById('trim-video');
  // Prefer the H.264 proxy for H.265 clips so scrubbing works; the actual cut
  // still runs on the original file. Fall back to the passed URL.
  const clip = clips.find(c => c.slug === slug);
  vid.src = clip ? playbackUrl(clip) : videoUrl;
  setVideoPreparing(vid, 'trim-video-preparing', clip ? clipNeedsProxy(clip) : false);
  document.getElementById('trim-modal').classList.add('open');
}
function onTrimVideoMeta() {
  const v = document.getElementById('trim-video');
  trimTotal = v.duration || 0;
  trimS = 0; trimE = trimTotal;
  renderTrimHandles(); refreshTrimUI();
}
function closeTrim() {
  setTrimPreview(false);
  document.getElementById('trim-modal').classList.remove('open');
  const v = document.getElementById('trim-video');
  v.pause(); v.src = '';
  trimSlug = null;
}

function setTrimPreview(on) {
  trimPreviewing = on;
  const btn = document.getElementById('trim-preview-btn');
  if (btn) {
    btn.classList.toggle('btn-primary-pill', on);
    btn.classList.toggle('btn-ghost-pill', !on);
    btn.innerHTML = on ? `${svgEl('square')} Stop` : `${svgEl('play')} Preview`;
  }
}

function toggleTrimPreview() {
  if (!trimTotal) return;
  const v = document.getElementById('trim-video');
  if (trimPreviewing) {
    setTrimPreview(false);
    v.pause();
    return;
  }
  setTrimPreview(true);
  v.currentTime = trimS;
  v.play();
}

function onTrimTimeUpdate() {
  syncTrimPlayhead();
  if (!trimPreviewing || !trimTotal) return;
  const v = document.getElementById('trim-video');
  // Loop the selection: past the out point (or scrubbed before the in
  // point), jump back to the start handle.
  if (v.currentTime >= trimE - 0.05 || v.currentTime < trimS - 0.3) {
    v.currentTime = trimS;
    if (v.paused) v.play();
  }
}

function onTrimVideoEnded() {
  if (!trimPreviewing) return;
  const v = document.getElementById('trim-video');
  v.currentTime = trimS;
  v.play();
}
function onBackdropClick(e) {
  if (e.target.id === 'trim-modal') closeTrim();
}
function renderTrimHandles() {
  if (!trimTotal) return;
  const sp = (trimS/trimTotal)*100, ep = (trimE/trimTotal)*100;
  document.getElementById('tl-h-start').style.left = sp + '%';
  document.getElementById('tl-h-end').style.left   = ep + '%';
  document.getElementById('tl-sel').style.left  = sp + '%';
  document.getElementById('tl-sel').style.width = (ep - sp) + '%';
}
function refreshTrimUI() {
  setText('t-in',  fmtSec(trimS, true));
  setText('t-out', fmtSec(trimE, true));
  setText('t-dur', fmtSec(Math.max(0, trimE - trimS), true));
  setText('tc-start', toTC(trimS));
  setText('tc-end',   toTC(trimE));
}
function syncTrimPlayhead() {
  if (!trimTotal) return;
  const p = (document.getElementById('trim-video').currentTime / trimTotal) * 100;
  document.getElementById('tl-playhead').style.left = p + '%';
}
function onTrimDragMove(cx) {
  if (!dragging || !trimTotal) return;
  if (trimPreviewing) { setTrimPreview(false); document.getElementById('trim-video').pause(); }
  const tl = document.getElementById('timeline');
  const rect = tl.getBoundingClientRect();
  const x = Math.max(0, Math.min(rect.width, cx - rect.left));
  const t = (x / rect.width) * trimTotal;
  const MIN = 0.5;
  const vid = document.getElementById('trim-video');
  if (dragging === 'start') { trimS = Math.max(0, Math.min(t, trimE - MIN)); vid.currentTime = trimS; }
  else                       { trimE = Math.min(trimTotal, Math.max(t, trimS + MIN)); vid.currentTime = trimE; }
  renderTrimHandles(); refreshTrimUI();
}
async function saveTrim() {
  if (!trimSlug) return;
  if (trimPreviewing) { setTrimPreview(false); document.getElementById('trim-video').pause(); }
  const btn = document.getElementById('trim-save-btn');
  const sta = document.getElementById('trim-status');
  btn.disabled = true; btn.textContent = 'Saving…'; sta.textContent = '';
  try {
    const r = await fetch(`/api/clips/${encodeURIComponent(trimSlug)}/trim`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start: trimS, end: trimE }),
    });
    const data = await r.json();
    if (data.ok) {
      toast('Clip trimmed and saved!', 'ok');
      closeTrim();
      await fetchClips();
    } else {
      sta.textContent = data.error || 'Trim failed';
      toast(data.error || 'Trim failed', 'err');
      btn.disabled = false;
      btn.innerHTML = `${svgEl('scissors')} Save trim`;
    }
  } catch (_) {
    toast('Failed to save trim', 'err');
    btn.disabled = false;
    btn.innerHTML = `${svgEl('scissors')} Save trim`;
  }
}
