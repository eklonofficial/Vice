'use strict';
// perf.js: visual effects mode: full glass, reduced, or measured

// ═══════════════════════════════════════════════════════════════════
// Visual effects
// ═══════════════════════════════════════════════════════════════════
// Chromium can fall back to software compositing without reporting it: no
// error, no black window, the UI simply draws every frame on the CPU. The
// glass surfaces and the drifting ambient glows are what makes that ruinous,
// costing a whole core on an idle window. The one signal that survives every
// fallback path is how fast frames actually arrive, so 'auto' measures it.
// ?sw=1 (vice-app relaunched itself into software mode) is the same verdict
// reached before the first paint.
//
// The choice lives in app-state rather than localStorage: the native window's
// localStorage does not survive restarts on every QtWebEngine build, which is
// the same reason the tutorial flag is stored server-side.

const EFFECTS_MODES = ['auto', 'full', 'reduced'];

// A median frame interval above this means the compositor is not keeping up.
// It clears a 30 Hz panel's 33ms, so a slow display is never mistaken for a
// slow compositor, and sits well under the 50ms+ that software-composited
// glass produces.
const SLOW_FRAME_MS = 42;
const PROBE_SAMPLES = 48;
const PROBE_WARMUP  = 4;

let effectsMode = 'auto';
let effectsMeasured = null;   // 'full' | 'reduced' once the probe has finished
let effectsProbing = false;

function effectsReduced() {
  if (effectsMode === 'reduced') return true;
  if (effectsMode === 'full')    return false;
  return IS_SOFTWARE_RENDER || effectsMeasured === 'reduced';
}

function effectsNote() {
  if (effectsMode !== 'auto') return '';
  if (IS_SOFTWARE_RENDER) return 'This window is compositing in software, so effects are reduced.';
  if (effectsMeasured === 'reduced') return 'Frames are arriving too slowly here, so effects are reduced.';
  if (effectsMeasured === 'full') return 'Frames are keeping up, so the full glass is on.';
  return 'Measuring.';
}

function applyEffects() {
  document.documentElement.classList.toggle('perf-low', effectsReduced());
  setText('s-effects-note', effectsNote());
}

function setEffectsMode(mode, persist = true) {
  effectsMode = EFFECTS_MODES.includes(mode) ? mode : 'auto';
  const select = document.getElementById('s-effects');
  if (select) select.value = effectsMode;
  applyEffects();
  if (effectsMode === 'auto' && effectsMeasured === null) probeEffects();
  if (persist) {
    fetch('/api/app-state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ effects_mode: effectsMode }),
    }).catch(() => {});
  }
}

// Sample how fast frames actually reach the screen. Chromium drives
// requestAnimationFrame from the compositor's frame production, so a
// compositor stuck at 15fps reports itself here directly. The median absorbs
// the outliers that a hidden window, a garbage collection or a clip decode
// would otherwise contribute.
function probeEffects() {
  if (effectsProbing || IS_SOFTWARE_RENDER) return;
  effectsProbing = true;
  // Always measure the expensive UI. Probing while .perf-low is on would
  // measure the cheap one, conclude the machine is fast and turn the glass
  // back on, which is how a mode that flips every launch is built.
  document.documentElement.classList.remove('perf-low');

  const gaps = [];
  let prev = 0, warmup = PROBE_WARMUP;
  const step = now => {
    if (warmup > 0) { warmup--; prev = now; requestAnimationFrame(step); return; }
    if (prev) gaps.push(now - prev);
    prev = now;
    if (gaps.length < PROBE_SAMPLES) { requestAnimationFrame(step); return; }
    gaps.sort((a, b) => a - b);
    effectsMeasured = gaps[gaps.length >> 1] > SLOW_FRAME_MS ? 'reduced' : 'full';
    effectsProbing = false;
    nativeLog(`compositor probe: median frame ${gaps[gaps.length >> 1].toFixed(1)}ms -> ${effectsMeasured}`);
    applyEffects();
  };
  requestAnimationFrame(step);
}

// Called once the boot splash is out of the way, so the probe never measures
// the splash animation or the first render of the clip grid.
function initEffects() {
  fetch('/api/app-state')
    .then(r => r.json())
    .then(s => setEffectsMode(s.effects_mode, /*persist*/false))
    .catch(() => setEffectsMode('auto', /*persist*/false));
}
