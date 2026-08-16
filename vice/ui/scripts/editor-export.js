'use strict';
// editor-export.js: export modal (progress over WS) + reset confirm

let edExportJob = null;      // job_id while a render runs
let edExportPhase = 'form';  // form | busy | done

function edOpenExport() {
  if (edEnd() <= 0) { toast('The timeline is empty', 'err'); return; }
  edSetPlaying(false);
  edExportPhase = 'form';
  document.getElementById('ed-export-name').value = '';
  document.getElementById('ed-export-loc').value = 'library';
  document.getElementById('ed-export-custom').value = '';
  document.getElementById('ed-export-add').checked = true;
  edExportLocChanged();
  edExportShowPhase();
  document.getElementById('ed-export-modal').classList.add('open');
}

function edCloseExport() {
  if (edExportPhase === 'busy') return;
  document.getElementById('ed-export-modal').classList.remove('open');
}

function edExportBackdrop(e) {
  if (e.target === e.currentTarget) edCloseExport();
}

function edExportLocChanged() {
  const loc = document.getElementById('ed-export-loc').value;
  document.getElementById('ed-export-custom-field').hidden = loc !== 'custom';
  document.getElementById('ed-export-also').hidden = loc === 'library';
  edExportSummary();
}

function edExportSummary() {
  const loc = document.getElementById('ed-export-loc').value;
  const name = document.getElementById('ed-export-name').value.trim() || 'Vice_Edit_N';
  const dir = loc === 'library' ? (cfg.output?.directory || '~/Videos/Vice')
    : loc === 'videos' ? '~/Videos'
    : (document.getElementById('ed-export-custom').value.trim() || '…');
  setText('ed-export-summary',
    `${edFmtS(edEnd())} · H.264 + AAC → ${dir}/${name.replace(/\.mp4$/i, '')}.mp4`);
  const warnEl = document.getElementById('ed-export-warn');
  const status = document.getElementById('rec-lbl');
  warnEl.hidden = !(status && /recording|session/i.test(status.textContent || ''));
}

function edExportShowPhase() {
  document.getElementById('ed-export-form').hidden = edExportPhase !== 'form';
  document.getElementById('ed-export-busy').hidden = edExportPhase !== 'busy';
  document.getElementById('ed-export-done').hidden = edExportPhase !== 'done';
  document.getElementById('ed-export-ft-form').hidden = edExportPhase !== 'form';
  document.getElementById('ed-export-ft-busy').hidden = edExportPhase !== 'busy';
  document.getElementById('ed-export-ft-done').hidden = edExportPhase !== 'done';
}

async function edStartExport() {
  const loc = document.getElementById('ed-export-loc').value;
  const name = document.getElementById('ed-export-name').value.trim();
  const custom = document.getElementById('ed-export-custom').value.trim();
  if (loc === 'custom' && !custom) { toast('Pick a folder for the export', 'err'); return; }
  if (edDirty) await edSaveNow();

  const body = {
    project: edProject,
    location: loc,
    add_to_library: document.getElementById('ed-export-add').checked,
    accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#0099ff',
  };
  if (name) body.filename = name;
  if (loc === 'custom') body.path = custom;

  try {
    const r = await fetch('/api/editor/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.ok) {
      const msg = d.error || (d.errors && d.errors[0]) || 'Export failed';
      toast(msg, 'err');
      return;
    }
    edExportJob = d.job_id;
    edExportPhase = 'busy';
    document.getElementById('ed-progress-fill').style.width = '0%';
    setText('ed-progress-label', '0% · encoding H.264');
    setText('ed-export-done-path', d.path);
    edExportShowPhase();
  } catch (_) { toast('Export failed, daemon unreachable', 'err'); }
}

async function edCancelExport() {
  if (!edExportJob) return;
  try {
    await fetch(`/api/editor/export/${encodeURIComponent(edExportJob)}/cancel`, { method: 'POST' });
  } catch (_) {}
}

function edOnExportProgress(msg) {
  if (msg.job_id !== edExportJob) return;
  const pct = Math.min(100, Math.round((msg.progress || 0) * 100));
  document.getElementById('ed-progress-fill').style.width = pct + '%';
  setText('ed-progress-label', `${pct}% · encoding H.264`);
}

function edOnExportDone(msg) {
  if (msg.job_id !== edExportJob) return;
  edExportJob = null;
  edExportPhase = 'done';
  setText('ed-export-done-path', msg.path || '');
  edExportShowPhase();
}

function edOnExportError(msg) {
  if (msg.job_id !== edExportJob) return;
  edExportJob = null;
  edExportPhase = 'form';
  edExportShowPhase();
  toast(msg.canceled ? 'Export canceled' : (msg.error || 'Export failed'), msg.canceled ? 'ok' : 'err');
}

// ── Reset confirm ──
function edOpenReset() {
  edSetPlaying(false);
  document.getElementById('ed-reset-modal').classList.add('open');
}

function edCloseReset() {
  document.getElementById('ed-reset-modal').classList.remove('open');
}

function edConfirmReset() {
  edCloseReset();
  edReset();
}
