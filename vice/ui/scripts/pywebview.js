'use strict';
// pywebview.js: native window bridge (Quit / Minimize)

// ═══════════════════════════════════════════════════════════════════
// pywebview bridge
// ═══════════════════════════════════════════════════════════════════
function keepRunning() {
  if (IS_NATIVE) window.pywebview.api.keep_running();
}
function quitVice() {
  if (!confirm('Stop Vice and quit? The recording daemon will shut down.')) return;
  if (IS_NATIVE) {
    window.pywebview.api.quit_app();
  } else {
    fetch('/api/quit', { method: 'POST' }).catch(() => {});
  }
}
