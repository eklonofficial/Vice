'use strict';
// settings.js: settings form, persistence, mic/audio/tunnel toggles

// Separate-audio-track ids in order (track 1 first). Mirrors
// cfg.recording.audio_tracks; empty means "mix into one track".
let audioTracks = [];

// Field id to notifications config key, for the custom sound paths.
const CUSTOM_SOUND_FIELDS = [
  ['s-snd-clip',          'clip_sound'],
  ['s-snd-clip-failed',   'clip_failed_sound'],
  ['s-snd-session-start', 'session_start_sound'],
  ['s-snd-session-end',   'session_end_sound'],
  ['s-snd-highlight',     'highlight_sound'],
];

// ═══════════════════════════════════════════════════════════════════
// Settings: fetch + save against /api/config
// ═══════════════════════════════════════════════════════════════════
async function fetchConfig() {
  try {
    const r = await fetch('/api/config');
    cfg = await r.json();
    syncFormFromCfg();
    populateHomeFromCfg();
    await refreshDisplayOptions(cfg.recording?.backend ?? 'auto', cfg.recording?.display ?? null);
    await refreshAudioSources(cfg.recording?.gsr_audio_source ?? 'default_output');
  } catch (_) {}
}

function pick(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  const v = String(val);
  for (const o of el.options) { if (o.value === v) { el.value = v; return; } }
  if (el.tagName === 'SELECT') {
    // Keep hand-edited config values instead of blanking the select
    const o = document.createElement('option');
    o.value = v;
    o.textContent = v + ' (custom)';
    el.appendChild(o);
  }
  el.value = v;
}

function syncFormFromCfg() {
  const r = cfg.recording || {}, h = cfg.hotkeys || {}, o = cfg.output || {}, s = cfg.sharing || {}, d = cfg.discord || {};
  const u = cfg.updates || {}, n = cfg.notifications || {}, ui = cfg.ui || {};
  document.getElementById('s-update-check').checked = u.check_on_start !== false;
  document.getElementById('s-hw-decode').checked = !!ui.hardware_video_decode;
  const volNotify = Math.round((n.sound_volume ?? 1) * 100);
  document.getElementById('s-vol-notify').value = volNotify;
  setText('s-vol-notify-v', volNotify ? volNotify + '%' : 'Off');
  for (const [id, key] of CUSTOM_SOUND_FIELDS) {
    const el = document.getElementById(id);
    if (el) el.value = n[key] ?? '';
  }

  const buf = r.buffer_duration ?? 120;
  document.getElementById('s-buf').value = buf;
  setText('s-buf-v', fmtSec(buf));
  const dur = r.clip_duration ?? 20;
  document.getElementById('s-dur').value = dur;
  setText('s-dur-v', fmtSec(dur));

  pick('s-fps', String(r.fps ?? 60));
  syncResolutionFromCfg(r.resolution ?? '');
  pick('s-container', r.container ?? 'mp4');
  pick('s-replay-storage', r.gsr_replay_storage ?? 'auto');
  updateBufferNote();
  pick('s-enc', r.encoder ?? 'auto');
  pick('s-depth', r.color_depth ?? '8');
  pick('s-backend', r.backend ?? 'auto');
  document.getElementById('s-follow-mouse').checked = !!r.follow_mouse_display;
  document.getElementById('s-audio').checked = r.capture_audio !== false;
  pick('s-gsr-audio', r.gsr_audio_source ?? 'default_output');
  pick('s-mic-source', r.microphone_source ?? 'default_input');
  document.getElementById('s-mic-mono').checked = !!r.microphone_mono;
  const volDesktop = Math.round((r.desktop_volume ?? 1) * 100);
  document.getElementById('s-vol-desktop').value = volDesktop;
  setText('s-vol-desktop-v', volDesktop + '%');
  const volMic = Math.round((r.microphone_volume ?? 1) * 100);
  document.getElementById('s-vol-mic').value = volMic;
  setText('s-vol-mic-v', volMic + '%');
  syncVolumeRows();
  pick('s-wf-mic', r.wf_microphone_strategy ?? 'prompt');
  document.getElementById('s-gsr-args').value = r.gsr_args ?? '';
  const clipKey = h.clip ?? 'KEY_F9';
  document.getElementById('s-key').value = clipKey;
  document.getElementById('s-key-btn').textContent = clipKey;
  renderClipPresetRows(h.clip_presets || []);
  document.getElementById('s-hotkey-blocklist').value = (Array.isArray(h.disable_while_focused) ? h.disable_while_focused : []).join('\n');
  document.getElementById('s-dir').value  = o.directory  ?? '';
  document.getElementById('s-tag-game').checked = o.tag_clips_with_game !== false;
  document.getElementById('s-auto-playlist').checked = o.auto_playlist_by_game !== false;
  document.getElementById('s-clip-name').value = o.clip_name_template ?? '';
  updateClipNamePreview();
  audioTracks = Array.isArray(r.audio_tracks) ? [...r.audio_tracks] : [];
  const mixEl = document.getElementById('s-track-mix');
  if (mixEl) mixEl.checked = !!r.audio_tracks_mix_first;
  renderAudioTracks();
  document.getElementById('s-port').value = s.port       ?? 8765;
  document.getElementById('s-cf').checked = s.cloudflare_tunnel !== false;
  // Discord
  document.getElementById('s-discord-enabled').checked = !!d.enabled;
  document.getElementById('s-discord-client-id').value = d.client_id_override ?? '';
  document.getElementById('s-discord-custom-games').value = (Array.isArray(d.custom_games) ? d.custom_games : [])
    .map(g => `${g.name} | ${(g.matches || []).join(', ')}`)
    .join('\n');
  syncMicToggles();
}

function syncMicToggles() {
  const mic = !!cfg.recording?.capture_microphone;
  const audio = cfg.recording?.capture_audio !== false;
  const cf = cfg.sharing?.cloudflare_tunnel !== false;
  const hm = document.getElementById('home-mic-toggle');  if (hm) hm.checked = mic;
  const sm = document.getElementById('settings-mic-toggle'); if (sm) sm.checked = mic;
  const ha = document.getElementById('home-audio-toggle'); if (ha) ha.checked = audio;
  const hc = document.getElementById('home-cf-toggle');    if (hc) hc.checked = cf;
}

function mergeConfigState(patch) {
  cfg = cfg || {};
  for (const key of ['recording','hotkeys','output','sharing','discord']) {
    if (!patch[key]) continue;
    cfg[key] = { ...(cfg[key] || {}), ...patch[key] };
  }
}

function renderClipPresetRows(presets) {
  const list = document.getElementById('clip-preset-list');
  if (!list) return;
  list.innerHTML = '';
  const rows = Array.isArray(presets) ? presets : [];
  rows.forEach(p => appendClipPresetRow(p.key || '', p.duration || 60));
}

function appendClipPresetRow(key = '', duration = 60) {
  const list = document.getElementById('clip-preset-list');
  if (!list) return;
  const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const row = document.createElement('div');
  row.className = 'clip-preset-row';
  row.innerHTML = `
    <button id="clip-preset-btn-${id}" class="key-capture-btn mono" type="button"
            onclick="startKeyCapture('clip-preset-btn-${id}', 'clip-preset-key-${id}', false)">${escHtml(key || 'Set key')}</button>
    <input type="hidden" id="clip-preset-key-${id}" class="clip-preset-key" value="${escHtml(key)}">
    <input type="number" class="clip-preset-duration" min="5" max="600" step="5" value="${Number(duration) || 60}">
    <span class="clip-preset-unit mono">s</span>
    <button class="btn-pill btn-ghost-pill btn-sm clip-preset-remove" type="button"
            onclick="this.closest('.clip-preset-row').remove()" title="Remove hotkey" aria-label="Remove hotkey">&times;</button>
  `;
  list.appendChild(row);
}

function addClipPresetRow() {
  appendClipPresetRow('', 60);
}

function collectClipPresetRows() {
  return [...document.querySelectorAll('.clip-preset-row')].map(row => ({
    key: row.querySelector('.clip-preset-key')?.value?.trim() || '',
    duration: Number(row.querySelector('.clip-preset-duration')?.value || 60),
  })).filter(row => row.key || row.duration);
}

// Parse the Discord custom-games textarea. Each non-empty line is
// `Display Name | match1, match2, ...`. Empty `name` or `matches` drops the line.
function parseDiscordCustomGames(text) {
  const out = [];
  for (const raw of (text || '').split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const [namePart, matchPart = ''] = line.split('|');
    const name = (namePart || '').trim();
    const matches = matchPart.split(',').map(s => s.trim()).filter(Boolean);
    if (!name || matches.length === 0) continue;
    out.push({ name, matches });
  }
  return out;
}

async function persistConfig(body) {
  const resp = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data.ok === false) throw new Error(data.error || 'save failed');
  mergeConfigState(body);
  return data;
}

function selectedBackend() { return document.getElementById('s-backend')?.value || 'auto'; }
function defaultDisplayNote() { return 'Choose which display to record (Auto = backend default)'; }
function setDisplayNote(message, warning = false) {
  const el = document.getElementById('s-display-note');
  if (!el) return;
  el.textContent = message || defaultDisplayNote();
  el.style.color = warning ? '#fcd34d' : 'var(--text-dim)';
}
function syncResolutionFromCfg(res) {
  const el = document.getElementById('s-res');
  const isPreset = [...el.options].some(o => o.value === res && o.value !== 'custom');
  if (isPreset) {
    el.value = res;
  } else {
    el.value = 'custom';
    document.getElementById('s-res-custom').value = res;
  }
  onResolutionChange();
}

function onResolutionChange() {
  const custom = document.getElementById('s-res').value === 'custom';
  document.getElementById('row-res-custom').style.display = custom ? '' : 'none';
}

// null = auto, false = invalid input, otherwise "WxH"
function resolvedResolution() {
  const sel = document.getElementById('s-res').value;
  if (sel !== 'custom') return sel || null;
  const raw = document.getElementById('s-res-custom').value.trim().toLowerCase().replace('×', 'x');
  if (!raw) return null;
  return /^\d{2,5}x\d{2,5}$/.test(raw) ? raw : false;
}

function updateBufferNote() {
  const el = document.getElementById('s-buf-note');
  if (!el) return;
  const buf = +document.getElementById('s-buf').value;
  const storage = document.getElementById('s-replay-storage')?.value || 'auto';
  const inRam = storage === 'ram' || (storage === 'auto' && buf <= 600);
  if (inRam && buf > 600) {
    const gb = (buf * 1.5 / 1024).toFixed(1);
    el.textContent = `A ${fmtSec(buf)} buffer in RAM uses roughly ${gb} GB at typical bitrates. Auto storage moves it to disk.`;
    el.style.color = '#fcd34d';
  } else if (!inRam) {
    el.textContent = 'Buffer is kept on disk, so long durations are fine.';
    el.style.color = 'var(--text-dim)';
  } else {
    el.textContent = 'Seconds of gameplay kept in the rolling buffer';
    el.style.color = 'var(--text-dim)';
  }
}

// Mirrors _render_clip_name in recorder.py so the preview matches what the
// daemon will actually write.
function renderClipName(template, n, game, now) {
  return template
    .replaceAll('$n', String(n))
    .replaceAll('$date', `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`)
    .replaceAll('$time', String(now.getHours()).padStart(2, '0') + String(now.getMinutes()).padStart(2, '0'))
    .replaceAll('$game', game || '')
    .replace(/[/\\\x00-\x1f]/g, '')
    .replace(/[_-]{2,}/g, m => m[0])
    .replace(/^[_\-. ]+|[_\-. ]+$/g, '');
}

function updateClipNamePreview() {
  const el = document.getElementById('s-clip-name-note');
  if (!el) return;
  const template = document.getElementById('s-clip-name').value.trim();
  if (!template) {
    el.textContent = '';
    return;
  }
  const name = renderClipName(template, 4, 'Overwatch-2', new Date());
  el.textContent = name ? `Next clip: ${name}.mp4` : 'That template is empty, default naming will be used.';
  el.style.color = name ? 'var(--ok)' : '#fcd34d';
}

function syncVolumeRows() {
  // Separate audio tracks keep full control, so the balance sliders only
  // apply to the default mixed-track mode.
  const hasTracks = audioTracks.length > 0;
  const desktopRow = document.getElementById('row-vol-desktop');
  const micRow = document.getElementById('row-vol-mic');
  if (desktopRow) desktopRow.style.display = hasTracks ? 'none' : '';
  if (micRow) micRow.style.display = hasTracks ? 'none' : '';
}

function setAudioSourceNote(message, warning = false) {
  const el = document.getElementById('s-gsr-audio-note');
  if (!el) return;
  el.textContent = message || 'Choose what gpu-screen-recorder captures';
  el.style.color = warning ? '#fcd34d' : 'var(--text-dim)';
}
function renderDisplayOptions(info, selectedDisplay = null) {
  const el = document.getElementById('s-display');
  if (!el) return;
  const raw = Array.isArray(info.displays) ? info.displays : [];
  // Defense-in-depth: drop any entry whose id or label looks like a GSR
  // error/diagnostic string. The backend already filters these, but if a new
  // GSR error format slips through we'd rather show "no displays" than render
  // a broken option that crashes recording.
  const looksLikeError = (s) => {
    const v = String(s || '').toLowerCase();
    return v.startsWith('gsr error')
        || v.startsWith('error:')
        || v.includes('for_each_active_monitor')
        || v.includes('failed to open');
  };
  const displays = raw.filter(d => !(looksLikeError(d.id) || looksLikeError(d.label)));
  // Same fallback order as renderAudioSources: an explicit choice, then what
  // is already picked but unsaved, then the saved config. A refresh must
  // never be the thing that forgets which display you chose.
  const desired = selectedDisplay
    ?? (el.dataset.ready ? el.value : cfg.recording?.display)
    ?? '';
  el.innerHTML = '';
  el.dataset.ready = '1';
  el.add(new Option('Auto (backend default)', ''));
  for (const item of displays) el.add(new Option(item.label || item.id, item.id));
  if (desired && displays.some(item => item.id === desired)) {
    el.value = desired; setDisplayNote(defaultDisplayNote()); return;
  }
  // A display the backend is not listing right now still has to survive a
  // save. Dropping to Auto here wrote display=null on the next save and
  // destroyed a value the user had set by hand, which is the only way to
  // reach a monitor gpu-screen-recorder will not enumerate (#160). The
  // audio source picker below already handles this the same way.
  if (desired) {
    el.add(new Option(`${desired} (saved)`, desired));
    el.value = desired;
    setDisplayNote(`Display "${desired}" is not being reported right now. It is still saved and will be used if it comes back.`, true);
    return;
  }
  el.value = '';
  if (info.warning) { setDisplayNote(`${info.warning} Auto will still work.`, true); return; }
  if (!displays.length) { setDisplayNote('No individual displays were detected. Auto will still work.', true); return; }
  setDisplayNote(defaultDisplayNote());
}
async function refreshDisplayOptions(backend = selectedBackend(), selectedDisplay = null) {
  try {
    const resp = await fetch(`/api/displays?backend=${encodeURIComponent(backend || 'auto')}`);
    displayInfo = await resp.json();
  } catch (_) {
    displayInfo = { backend: backend || 'auto', displays: [], warning: 'Could not load display options.' };
  }
  renderDisplayOptions(displayInfo, selectedDisplay);
  onFollowMouseChange();
}

// The display picker and follow-the-pointer are alternatives, so only one of
// them is live at a time.
function onFollowMouseChange() {
  const toggle = document.getElementById('s-follow-mouse');
  const note = document.getElementById('s-follow-mouse-note');
  if (!toggle) return;
  const supported = displayInfo?.follow_mouse_supported !== false;
  toggle.disabled = !supported;
  if (!supported) toggle.checked = false;
  const display = document.getElementById('s-display');
  if (display) display.disabled = toggle.checked;
  if (!note) return;
  note.textContent = supported
    ? 'Record whichever monitor the pointer is on. Switching monitors restarts the buffer, so a clip taken right after moving will be short.'
    : 'Needs X11, Hyprland or Sway. Vice cannot tell where the pointer is on this session.';
  note.style.color = supported ? 'var(--text-dim)' : '#fcd34d';
}

async function onBackendChange() {
  const preferred = document.getElementById('s-display')?.value || cfg.recording?.display || null;
  await refreshDisplayOptions(selectedBackend(), preferred);
}

// Friendly name for a source id, falling back to the raw id when the source
// is not in the last fetched list (e.g. an app that stopped playing audio).
function sourceLabel(id) {
  const hit = (audioSourceInfo?.sources || []).find(s => s.id === id);
  return hit?.label || id;
}

function sourceKind(source) {
  if (source.kind) return source.kind;
  const id = source.id || '';
  if (id === 'default_input' || (id.startsWith('device:') && !id.endsWith('.monitor'))) return 'input';
  if (id.startsWith('app:') || id.startsWith('app-inverse:')) return 'app';
  return 'monitor';
}

function onDesktopSourceChange() {
  const el = document.getElementById('s-gsr-audio');
  if (!el) return;
  const src = (audioSourceInfo?.sources || []).find(s => s.id === el.value);
  if (src && sourceKind(src) === 'input') {
    setAudioSourceNote('This is a microphone input, so clips would have no desktop audio. Pick a source under Desktop audio instead.', true);
  } else {
    setAudioSourceNote('Choose what gpu-screen-recorder captures');
  }
}

function renderAudioSources(info, selectedSource = null) {
  const el = document.getElementById('s-gsr-audio');
  if (!el) return;
  const sources = Array.isArray(info.sources) && info.sources.length
    ? info.sources
    : [{ id: 'default_output', label: 'Default output' }];
  // On refresh (selectedSource null) keep whatever the user has picked but
  // not yet saved; before the first populate the live value is meaningless,
  // so fall back to the saved config value.
  const desired = selectedSource
    ?? (el.dataset.ready ? el.value : cfg.recording?.gsr_audio_source)
    ?? 'default_output';
  el.innerHTML = '';
  const kindGroups = [['monitor', 'Desktop audio'], ['input', 'Microphones'], ['app', 'Applications']];
  for (const [kind, label] of kindGroups) {
    const members = sources.filter(s => sourceKind(s) === kind);
    if (!members.length) continue;
    const group = document.createElement('optgroup');
    group.label = label;
    for (const source of members) group.appendChild(new Option(source.label || source.id, source.id));
    el.appendChild(group);
  }
  const grouped = new Set(kindGroups.map(([kind]) => kind));
  for (const source of sources.filter(s => !grouped.has(sourceKind(s)))) {
    el.add(new Option(source.label || source.id, source.id));
  }
  el.dataset.ready = '1';
  const pickEl = document.getElementById('s-track-pick');
  if (pickEl) {
    const prevPick = pickEl.value;
    pickEl.innerHTML = '';
    for (const source of sources) pickEl.add(new Option(source.label || source.id, source.id));
    if (prevPick && sources.some(source => source.id === prevPick)) pickEl.value = prevPick;
  }
  renderMicSources(sources);
  renderAudioTracks();  // chip labels come from the refreshed list
  if (sources.some(source => source.id === desired)) {
    el.value = desired;
    onDesktopSourceChange();
    return;
  }
  el.add(new Option(`${desired} (saved)`, desired));
  el.value = desired;
  setAudioSourceNote(info.warning || 'Saved source was not listed right now, but it will still be passed to gpu-screen-recorder.', true);
}

// Microphone picker: the system default plus real capture devices. Monitor
// sources are desktop audio and app:* sources are not microphones.
function renderMicSources(sources) {
  const el = document.getElementById('s-mic-source');
  if (!el) return;
  const inputs = sources.filter(s => sourceKind(s) === 'input');
  if (!inputs.some(s => s.id === 'default_input')) {
    inputs.unshift({ id: 'default_input', label: 'Default input' });
  }
  const desired = (el.dataset.ready ? el.value : cfg.recording?.microphone_source) || 'default_input';
  el.innerHTML = '';
  for (const source of inputs) el.add(new Option(source.label || source.id, source.id));
  el.dataset.ready = '1';
  if (inputs.some(s => s.id === desired)) { el.value = desired; return; }
  el.add(new Option(`${desired} (saved)`, desired));
  el.value = desired;
}

let audioSourcesRefreshing = false;
async function refreshAudioSources(selectedSource = null) {
  if (audioSourcesRefreshing) return;
  audioSourcesRefreshing = true;
  try {
    const resp = await fetch('/api/audio-sources');
    audioSourceInfo = await resp.json();
  } catch (_) {
    audioSourceInfo = { sources: [{ id: 'default_output', label: 'Default output' }], warning: 'Could not load audio sources.' };
  } finally {
    audioSourcesRefreshing = false;
  }
  renderAudioSources(audioSourceInfo, selectedSource);
}

async function refreshSourcesClicked(btn) {
  btn.disabled = true;
  try { await refreshAudioSources(); } finally { btn.disabled = false; }
  toast('Audio sources refreshed', 'ok');
}

function selectedWfMicStrategy() { return cfg.recording?.wf_microphone_strategy || 'prompt'; }
function micToggleNeedsWfChoice(targetChecked) {
  if (!targetChecked) return false;
  if (cfg.recording?.capture_audio === false) return false;
  if (selectedWfMicStrategy() !== 'prompt') return false;
  const configuredBackend = cfg.recording?.backend || 'auto';
  return configuredBackend === 'wf-recorder' || runtimeBackend === 'wf-recorder';
}
async function onClipMicToggleChange(el) {
  const targetChecked = !!el.checked;
  if (micToggleNeedsWfChoice(targetChecked)) {
    pendingMicToggle = true;
    el.checked = false;
    syncMicToggles();
    openWfMicModal();
    return;
  }
  await saveClipMicToggle(targetChecked);
}
async function saveClipMicToggle(enabled, strategyOverride = null) {
  const prev = !!cfg.recording?.capture_microphone;
  const body = { recording: { capture_microphone: enabled } };
  if (strategyOverride !== null) body.recording.wf_microphone_strategy = strategyOverride;
  try {
    const data = await persistConfig(body);
    if (data.applied === false && data.warning) toast(data.warning, 'err');
    else toast(enabled ? 'Microphone audio enabled for new clips' : 'Microphone audio removed from new clips', 'ok');
    document.getElementById('s-wf-mic').value = selectedWfMicStrategy();
    syncMicToggles();
  } catch (_) {
    cfg.recording = cfg.recording || {};
    cfg.recording.capture_microphone = prev;
    syncMicToggles();
    toast('Failed to update microphone setting', 'err');
  }
}

async function onHomeAudioToggle(el) {
  const target = !!el.checked;
  try {
    await persistConfig({ recording: { capture_audio: target } });
    document.getElementById('s-audio').checked = target;
    toast(target ? 'Desktop audio on' : 'Desktop audio off', 'ok');
  } catch (_) { el.checked = !target; toast('Failed to update audio', 'err'); }
}

async function onHomeTunnelToggle(el) {
  const target = !!el.checked;
  const ro = document.getElementById('tunnel-readout');
  const rt = document.getElementById('tunnel-readout-text');
  try {
    const data = await persistConfig({ sharing: { cloudflare_tunnel: target } });
    document.getElementById('s-cf').checked = target;
    if (target) {
      ro.classList.add('active');
      rt.textContent = tunnelUrl || 'Tunnel starting…';
      toast('Public tunnel starting…', 'ok');
    } else {
      ro.classList.remove('active');
      rt.textContent = 'Tunnel inactive';
      tunnelUrl = null;
      toast('Tunnel stopped', 'ok');
    }
    if (data.restart_required) showRestartModal();
  } catch (_) { el.checked = !target; toast('Failed to toggle tunnel', 'err'); }
}

function copyTunnelUrl() {
  if (!tunnelUrl) { toast('Enable the tunnel first', 'err'); return; }
  copyToClipboard(tunnelUrl).then(ok => {
    if (ok) toast('Public URL copied!', 'ok');
    else showManualCopyModal(tunnelUrl);
  });
}

function addAudioTrack() {
  const pickEl = document.getElementById('s-track-pick');
  const id = pickEl ? pickEl.value : '';
  if (!id) return;
  if (audioTracks.includes(id)) { toast('That source is already a track', 'err'); return; }
  audioTracks.push(id);
  renderAudioTracks();
}

function removeAudioTrack(index) {
  audioTracks.splice(index, 1);
  renderAudioTracks();
}

function moveAudioTrack(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= audioTracks.length) return;
  [audioTracks[index], audioTracks[target]] = [audioTracks[target], audioTracks[index]];
  renderAudioTracks();
}

// Mirrors _classify_gsr_source in recorder.py. A track id can be several
// sources joined with "|", so classification is per part.
function trackPartKind(id) {
  const value = (id || '').trim();
  if (value === 'default_output') return 'monitor';
  if (value === 'default_input') return 'input';
  if (value.startsWith('app:') || value.startsWith('app-inverse:')) return 'app';
  if (value.startsWith('device:')) return value.endsWith('.monitor') ? 'monitor' : 'input';
  return 'unknown';
}

// With desktop audio off the recorder keeps only microphone sources, so a
// game track vanishes and the user is left with just their voice with
// nothing anywhere saying why (#137). Work out what would go, so the UI can.
function tracksLostWithoutDesktopAudio() {
  const dropped = [];
  const trimmed = [];
  for (const id of audioTracks) {
    const parts = String(id).split('|').filter(Boolean);
    const kept = parts.filter(p => trackPartKind(p) === 'input');
    if (!kept.length) dropped.push(id);
    else if (kept.length !== parts.length) trimmed.push(id);
  }
  return { dropped, trimmed };
}

function syncTrackWarning() {
  const el = document.getElementById('s-track-warning');
  if (!el) return;
  const desktopOn = document.getElementById('s-audio')?.checked !== false;
  const { dropped, trimmed } = tracksLostWithoutDesktopAudio();
  if (desktopOn || (!dropped.length && !trimmed.length)) { el.hidden = true; el.textContent = ''; return; }
  const names = dropped.map(sourceLabel).join(', ');
  const parts = [];
  if (dropped.length) {
    parts.push(`"Capture desktop audio" is off, so ${dropped.length === 1 ? 'this track will not be recorded' : 'these tracks will not be recorded'}: ${names}.`);
  }
  if (trimmed.length) {
    parts.push(`${trimmed.length} more will keep only their microphone sources.`);
  }
  parts.push('Turn desktop audio back on above to keep them.');
  el.textContent = parts.join(' ');
  el.hidden = false;
}

function renderAudioTracks() {
  const list = document.getElementById('s-track-list');
  if (!list) return;
  syncVolumeRows();
  syncTrackWarning();
  list.innerHTML = '';
  // Mirror the recorder: the combined track is only added when there are at
  // least two tracks to mix, and it always becomes track 1.
  const mixFirst = !!document.getElementById('s-track-mix')?.checked && audioTracks.length > 1;
  if (mixFirst) {
    const chip = document.createElement('span');
    chip.className = 'track-chip track-chip-mix';
    chip.innerHTML = '<span class="track-num">1</span> <span class="track-id">Mix of all tracks</span>';
    list.appendChild(chip);
  }
  const base = mixFirst ? 2 : 1;
  audioTracks.forEach((id, i) => {
    const chip = document.createElement('span');
    chip.className = 'track-chip';
    chip.title = id;
    chip.innerHTML =
      `<span class="track-num">${i + base}</span> <span class="track-id">${escHtml(sourceLabel(id))}</span>` +
      `<button type="button" class="track-move" title="Move up" ${i === 0 ? 'disabled' : ''} onclick="moveAudioTrack(${i}, -1)">↑</button>` +
      `<button type="button" class="track-move" title="Move down" ${i === audioTracks.length - 1 ? 'disabled' : ''} onclick="moveAudioTrack(${i}, 1)">↓</button>` +
      `<button type="button" title="Remove track" onclick="removeAudioTrack(${i})">×</button>`;
    list.appendChild(chip);
  });
}

async function saveSettings() {
  const resolution = resolvedResolution();
  if (resolution === false) {
    toast('Custom resolution must look like 1600x900', 'err');
    return;
  }
  const clipPresets = collectClipPresetRows();
  const maxClipDuration = Math.max(
    +document.getElementById('s-dur').value,
    ...clipPresets.map(p => Number(p.duration) || 0),
  );
  const bufferDuration = Math.max(+document.getElementById('s-buf').value, maxClipDuration);
  if (+document.getElementById('s-buf').value !== bufferDuration) {
    document.getElementById('s-buf').value = bufferDuration;
    setText('s-buf-v', fmtSec(bufferDuration));
  }
  const body = {
    recording: {
      buffer_duration: bufferDuration,
      clip_duration:   +document.getElementById('s-dur').value,
      fps:             +document.getElementById('s-fps').value,
      display:         document.getElementById('s-display').value || null,
      follow_mouse_display: document.getElementById('s-follow-mouse').checked,
      resolution:      resolution,
      container:       document.getElementById('s-container').value,
      encoder:         document.getElementById('s-enc').value,
      color_depth:     document.getElementById('s-depth').value,
      backend:         document.getElementById('s-backend').value,
      capture_audio:   document.getElementById('s-audio').checked,
      gsr_replay_storage: document.getElementById('s-replay-storage').value,
      capture_microphone: document.getElementById('settings-mic-toggle').checked,
      microphone_source: document.getElementById('s-mic-source').value || 'default_input',
      microphone_mono:   document.getElementById('s-mic-mono').checked,
      desktop_volume:    (+document.getElementById('s-vol-desktop').value) / 100,
      microphone_volume: (+document.getElementById('s-vol-mic').value) / 100,
      wf_microphone_strategy: document.getElementById('s-wf-mic').value,
      gsr_audio_source: document.getElementById('s-gsr-audio').value || 'default_output',
      audio_tracks:    [...audioTracks],
      audio_tracks_mix_first: document.getElementById('s-track-mix').checked,
      gsr_args:        document.getElementById('s-gsr-args').value.trim(),
    },
    hotkeys: {
      clip: document.getElementById('s-key').value,
      clip_presets: clipPresets,
      disable_while_focused: document.getElementById('s-hotkey-blocklist').value
        .split('\n').map(s => s.trim()).filter(Boolean),
    },
    output:  {
      directory: document.getElementById('s-dir').value,
      tag_clips_with_game: document.getElementById('s-tag-game').checked,
      auto_playlist_by_game: document.getElementById('s-auto-playlist').checked,
      clip_name_template: document.getElementById('s-clip-name').value.trim(),
    },
    sharing: {
      port:              +document.getElementById('s-port').value,
      cloudflare_tunnel: document.getElementById('s-cf').checked,
    },
    updates: {
      check_on_start: document.getElementById('s-update-check').checked,
    },
    notifications: {
      sound_volume: (+document.getElementById('s-vol-notify').value) / 100,
      ...Object.fromEntries(CUSTOM_SOUND_FIELDS.map(([id, key]) =>
        [key, document.getElementById(id)?.value.trim() || null])),
    },
    ui: {
      hardware_video_decode: document.getElementById('s-hw-decode').checked,
    },
    discord: {
      enabled: document.getElementById('s-discord-enabled').checked,
      client_id_override: document.getElementById('s-discord-client-id').value.trim() || null,
      custom_games: parseDiscordCustomGames(document.getElementById('s-discord-custom-games').value),
    },
  };
  try {
    const currentSharing = cfg.sharing || {};
    const sharingChanged =
      Number(body.sharing.port) !== Number(currentSharing.port ?? 8765)
      || Boolean(body.sharing.cloudflare_tunnel) !== Boolean(currentSharing.cloudflare_tunnel !== false);

    const data = await persistConfig(body);
    if (data.applied === false && data.warning) toast(data.warning, 'err');
    if (data.restart_required && sharingChanged) showRestartModal();

    populateHomeFromCfg();
    renderStats();
    renderClips();
    renderHomeRecent();
    const m = document.getElementById('saved-msg');
    m.classList.add('show'); setTimeout(() => m.classList.remove('show'), 2400);
  } catch (err) { toast(err?.message || 'Failed to save settings', 'err'); }
}
