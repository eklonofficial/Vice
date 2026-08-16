'use strict';
// updates.js: "a new version is out" notice

// Set from /api/status at boot or pushed over the WebSocket when the daily
// check finds something. Null means there is nothing to say.
let updateInfo = null;

function openExternal(url) {
  if (!url) return;
  nativeLog(`openExternal: ${url}`);
  try {
    if (IS_NATIVE && window.pywebview && window.pywebview.api && window.pywebview.api.open_url) {
      window.pywebview.api.open_url(String(url));
      return;
    }
  } catch (err) {
    nativeLog(`openExternal bridge threw ${err && err.message ? err.message : err}`);
  }
  window.open(url, '_blank', 'noopener');
}

function renderUpdate() {
  if (!updateInfo) return;
  const cmd = (updateInfo.install || {}).command || '';
  setText('update-title', `Vice ${updateInfo.version} is available`);
  setText('update-sub', cmd
    ? `You are on ${viceVersion}. Update with:`
    : `You are on ${viceVersion}.`);
  const cmdEl = document.getElementById('update-cmd');
  cmdEl.textContent = cmd;
  cmdEl.hidden = !cmd;
  document.getElementById('update-copy-btn').hidden = !cmd;
  document.getElementById('update-notes').innerHTML =
    (updateInfo.notes || []).map(n => `<li>${escHtml(n)}</li>`).join('');
}

function showUpdate() {
  if (!updateInfo) return;
  renderUpdate();
  document.getElementById('update-modal').classList.remove('hidden');
}

// "Later" hides the card for this version for good. The chip stays so the
// update is still findable, but nothing pops up again.
function dismissUpdate() {
  document.getElementById('update-modal').classList.add('hidden');
  if (!updateInfo) return;
  localStorage.setItem('vice_update_dismissed', updateInfo.version);
  fetch('/api/app-state', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ update_dismissed_version: updateInfo.version }),
  }).catch(() => {});
}

function openReleaseNotes() {
  if (updateInfo) openExternal(updateInfo.url);
}

function copyUpdateCmd() {
  const cmd = updateInfo && (updateInfo.install || {}).command;
  if (!cmd) return;
  copyToClipboard(cmd).then(ok => {
    if (ok) toast('Command copied, paste it into a terminal', 'ok');
    else showManualCopyModal(cmd);
  });
}

function showUpdateChip() {
  const chip = document.getElementById('update-chip');
  if (chip) chip.hidden = !updateInfo;
}

// Called with the payload from /api/status at boot and from the WebSocket.
// The card only appears for a version the user has not already waved away.
function onUpdateAvailable(info, opts = {}) {
  if (!info || !info.version) return;
  updateInfo = info;
  showUpdateChip();
  if (localStorage.getItem('vice_update_dismissed') === info.version) return;
  fetch('/api/app-state')
    .then(r => r.json())
    .then(s => {
      if (s.update_dismissed_version === info.version) {
        localStorage.setItem('vice_update_dismissed', info.version);
        return;
      }
      setTimeout(showUpdate, opts.delay || 0);
    })
    .catch(() => setTimeout(showUpdate, opts.delay || 0));
}

// Settings "Check now": reports the result either way so the button never
// looks like it did nothing.
async function checkForUpdatesNow() {
  const btn = document.getElementById("s-update-check-btn");
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/update/check', { method: 'POST' });
    const d = await r.json();
    if (d.update) {
      localStorage.removeItem('vice_update_dismissed');
      updateInfo = d.update;
      showUpdateChip();
      showUpdate();
    } else {
      toast(`Vice ${viceVersion} is up to date`, 'ok');
    }
  } catch (_) {
    toast('Could not reach GitHub', 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}
