'use strict';
// modals.js: tutorial / restart / wf-mic modal open/close

// ═══════════════════════════════════════════════════════════════════
// Tutorial / restart / wf-mic modals
// ═══════════════════════════════════════════════════════════════════
function showTutorial() {
  syncDynamicCopy();
  setTutorialPage(1);
  document.getElementById('tutorial-modal').classList.remove('hidden');
}
function setTutorialPage(n) {
  document.getElementById('tut-page-1').hidden = n !== 1;
  document.getElementById('tut-page-2').hidden = n !== 2;
  document.getElementById('tut-back-btn').hidden = n === 1;
  document.getElementById('tut-next-btn').hidden = n === 2;
  document.getElementById('tut-done-btn').hidden = n !== 2;
}
function closeTutorial() {
  localStorage.setItem('vice_tutorial_shown', '1');
  // Persist server-side too so a webview storage reset does not bring it back.
  fetch('/api/app-state', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tutorial_seen: true }),
  }).catch(() => {});
  document.getElementById('tutorial-modal').classList.add('hidden');
}
function showManualCopyModal(text) {
  const input = document.getElementById('manual-copy-text');
  input.value = text;
  document.getElementById('manual-copy-modal').classList.remove('hidden');
  input.focus();
  input.select();
}
function closeManualCopyModal() {
  document.getElementById('manual-copy-modal').classList.add('hidden');
}
function showRestartModal() {
  document.getElementById('restart-modal').classList.remove('hidden');
}
function closeRestartModal() {
  document.getElementById('restart-modal').classList.add('hidden');
}
function openWfMicModal() {
  document.getElementById('wf-mic-modal').classList.remove('hidden');
}
function closeWfMicModal() {
  pendingMicToggle = null;
  document.getElementById('wf-mic-modal').classList.add('hidden');
  syncMicToggles();
}
async function chooseWfMicStrategy(strategy) {
  if (pendingMicToggle !== true) { closeWfMicModal(); return; }
  document.getElementById('wf-mic-modal').classList.add('hidden');
  pendingMicToggle = null;
  await saveClipMicToggle(true, strategy);
}
