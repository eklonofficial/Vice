'use strict';
// hotkeys.js: rebind clip key (e.code -> evdev KEY_*)

// ═══════════════════════════════════════════════════════════════════
// Hotkey capture (e.code → evdev KEY_* names)
// ═══════════════════════════════════════════════════════════════════
function codeToEvdev(code) {
  const fm = code.match(/^F(\d+)$/);
  if (fm) return `KEY_F${fm[1]}`;
  const km = code.match(/^Key([A-Z])$/);
  if (km) return `KEY_${km[1]}`;
  const dm = code.match(/^Digit(\d)$/);
  if (dm) return `KEY_${dm[1]}`;
  const np = {
    Numpad0:'KEY_KP0', Numpad1:'KEY_KP1', Numpad2:'KEY_KP2', Numpad3:'KEY_KP3',
    Numpad4:'KEY_KP4', Numpad5:'KEY_KP5', Numpad6:'KEY_KP6', Numpad7:'KEY_KP7',
    Numpad8:'KEY_KP8', Numpad9:'KEY_KP9', NumpadAdd:'KEY_KPPLUS',
    NumpadSubtract:'KEY_KPMINUS', NumpadMultiply:'KEY_KPASTERISK',
    NumpadDivide:'KEY_KPSLASH', NumpadEnter:'KEY_KPENTER', NumpadDecimal:'KEY_KPDOT',
  };
  if (np[code]) return np[code];
  const named = {
    Insert:'KEY_INSERT', Delete:'KEY_DELETE', Home:'KEY_HOME', End:'KEY_END',
    PageUp:'KEY_PAGEUP', PageDown:'KEY_PAGEDOWN', ScrollLock:'KEY_SCROLLLOCK',
    Pause:'KEY_PAUSE', PrintScreen:'KEY_SYSRQ', Tab:'KEY_TAB',
    CapsLock:'KEY_CAPSLOCK', NumLock:'KEY_NUMLOCK',
    ArrowUp:'KEY_UP', ArrowDown:'KEY_DOWN', ArrowLeft:'KEY_LEFT', ArrowRight:'KEY_RIGHT',
    Backspace:'KEY_BACKSPACE', Enter:'KEY_ENTER', Space:'KEY_SPACE',
    Minus:'KEY_MINUS', Equal:'KEY_EQUAL', BracketLeft:'KEY_LEFTBRACE',
    BracketRight:'KEY_RIGHTBRACE', Backslash:'KEY_BACKSLASH',
    Semicolon:'KEY_SEMICOLON', Quote:'KEY_APOSTROPHE', Backquote:'KEY_GRAVE',
    Comma:'KEY_COMMA', Period:'KEY_DOT', Slash:'KEY_SLASH',
  };
  return named[code] || null;
}

// Modifiers: in the canonical Ctrl/Alt/Shift/Meta order the backend stores.
// Left names are canonical; either physical key produces the same combo.
const MOD_ORDER = [
  ['ctrlKey', 'KEY_LEFTCTRL'],
  ['altKey', 'KEY_LEFTALT'],
  ['shiftKey', 'KEY_LEFTSHIFT'],
  ['metaKey', 'KEY_LEFTMETA'],
];
const MOD_CODES = new Set([
  'ShiftLeft','ShiftRight','ControlLeft','ControlRight',
  'AltLeft','AltRight','MetaLeft','MetaRight',
]);

// Build "KEY_LEFTALT+KEY_F9" from the held modifiers plus the main key.
function comboFromEvent(e, mainEvdev) {
  const mods = MOD_ORDER.filter(([flag]) => e[flag]).map(([, name]) => name);
  return [...mods, mainEvdev].join('+');
}

function startKeyCapture(buttonId = 's-key-btn', inputId = 's-key', persistPrimary = true) {
  const btn = document.getElementById(buttonId);
  const input = document.getElementById(inputId);
  if (!btn || !input) return;
  if (btn.classList.contains('listening')) return;
  const prev = input.value || 'KEY_F9';
  btn.classList.add('listening');
  btn.textContent = 'Press a key…';

  const onKey = e => {
    e.preventDefault(); e.stopPropagation();
    // Wait for a real key: a lone modifier just arms the combo.
    if (MOD_CODES.has(e.code)) return;
    if (e.code === 'Escape') {
      btn.textContent = prev;
      btn.classList.remove('listening');
      document.removeEventListener('keydown', onKey, true);
      return;
    }
    const main = codeToEvdev(e.code);
    if (!main) { toast('Unsupported key, try another', 'err'); return; }
    const evdev = comboFromEvent(e, main);
    input.value = evdev;
    btn.textContent = evdev;
    btn.classList.remove('listening');
    document.removeEventListener('keydown', onKey, true);

    if (persistPrimary) {
      persistConfig({ hotkeys: { clip: evdev } })
        .then(() => { toast(`Hotkey saved: ${evdev}`, 'ok'); populateHomeFromCfg(); })
        .catch(() => toast('Key captured but save failed, press Save Settings', 'err'));
    }
  };
  document.addEventListener('keydown', onKey, true);
}
