'use strict';
// theme.js: accent-color swatches with localStorage persistence

// ═══════════════════════════════════════════════════════════════════
// Theme (5 swatches; persists in localStorage)
// Each theme carries the accent ramp plus the three ambient glow colors.
// ═══════════════════════════════════════════════════════════════════
const THEMES = {
  blue:   { hex: '#0099ff', hi: '#33adff', lo: '#0077e6', text: '#7fc4ff', rgb: '0,153,255',  glows: ['#123f8f', '#0d6b74', '#3b2a8f'] },
  purple: { hex: '#8b5cf6', hi: '#a78bfa', lo: '#6d28d9', text: '#c4b5fd', rgb: '139,92,246', glows: ['#3b2a8f', '#5b21b6', '#1e2a6e'] },
  green:  { hex: '#10b981', hi: '#34d399', lo: '#059669', text: '#6ee7b7', rgb: '16,185,129', glows: ['#0d5a4a', '#0d6b74', '#123f5e'] },
  red:    { hex: '#ef4444', hi: '#f87171', lo: '#dc2626', text: '#fca5a5', rgb: '239,68,68',  glows: ['#6e1a2a', '#3b2a8f', '#4a1030'] },
  orange: { hex: '#f97316', hi: '#fb923c', lo: '#ea580c', text: '#fdba74', rgb: '249,115,22', glows: ['#6e3a10', '#5a1a2a', '#3b2a4f'] },
};
function setTheme(name, persist = true) {
  const t = THEMES[name]; if (!t) return;
  const root = document.documentElement.style;
  root.setProperty('--accent', t.hex);
  root.setProperty('--accent-hi', t.hi);
  root.setProperty('--accent-lo', t.lo);
  root.setProperty('--accent-text', t.text);
  root.setProperty('--accent-rgb', t.rgb);
  root.setProperty('--glow-1', t.glows[0]);
  root.setProperty('--glow-2', t.glows[1]);
  root.setProperty('--glow-3', t.glows[2]);
  document.querySelectorAll('.swatch').forEach(s => s.classList.toggle('active', s.dataset.theme === name));
  if (persist) {
    localStorage.setItem('vice-theme', name);
    // Keep share-page embeds (Discord sidebar strip) on the same accent.
    fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sharing: { embed_color: t.hex } }),
    }).catch(() => {});
  }
}
