import {useEffect, useRef, useState} from 'react';

import {t} from '../lib/i18n';

export type CaptureMode = 'active_game' | 'desktop';

function modes() {
  return [
    {
      id: 'active_game' as const,
      short: t('home.captureModeGameShort'),
      whole: t('home.gameCapture'),
      detail: t('home.captureModeGameDetail'),
    },
    {
      id: 'desktop' as const,
      short: t('home.captureModeDesktopShort'),
      whole: t('home.captureModeDesktopWhole'),
      detail: t('home.captureModeDesktopDetail'),
    },
  ];
}

/**
 * A tile that splits in place into Game/Desktop/App buttons on click, instead
 * of opening a floating menu, then folds back into a single button labelled
 * with whatever got picked. "App" (pin to a chosen window rather than a
 * detected game) has no backend yet, so it always shows disabled.
 */
export function CaptureModeTile({
  mode,
  supported,
  busy,
  onSelect,
}: {
  mode: CaptureMode;
  supported: boolean;
  busy: boolean;
  onSelect: (mode: CaptureMode) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: Event) => {
      if (e.type === 'pointerdown' && ref.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('pointerdown', close, true);
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('pointerdown', close, true);
      document.removeEventListener('keydown', onKey, true);
    };
  }, [open]);

  const pick = (id: CaptureMode) => {
    if (!supported) return;
    if (!open) {
      setOpen(true);
      return;
    }
    if (id !== mode) onSelect(id);
    setOpen(false);
  };

  return (
    <div
      className="tile capture-tile"
      ref={ref}
      data-open={open || undefined}
      data-active={(!open && mode === 'active_game') || undefined}
      aria-busy={busy || undefined}>
      {modes().map(m => (
        <button
          key={m.id}
          type="button"
          className="capture-seg"
          data-current={m.id === mode || undefined}
          aria-pressed={m.id === mode}
          disabled={busy}
          onClick={() => pick(m.id)}>
          <span className="capture-seg-badge" aria-hidden="true">
            {m.id === 'active_game' ? <GameControllerIcon /> : <MonitorIcon />}
          </span>
          <span className="capture-seg-text">
            <b>{open ? m.short : m.whole}</b>
            {!open ? <span>{supported ? m.detail : t('home.gameCaptureUnsupported')}</span> : null}
          </span>
        </button>
      ))}
      <button
        type="button"
        className="capture-seg capture-seg-stub"
        disabled
        title={t('home.captureModeAppHelp')}>
        <span className="capture-seg-text">
          <b>{t('home.captureModeAppShort')}</b>
        </span>
      </button>
    </div>
  );
}

const stroke = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
};

const GameControllerIcon = () => (
  <svg width="19" height="19" viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M6 8h12a4 4 0 0 1 4 4.5l-1 5a2.5 2.5 0 0 1-4.4 1.4L15 17H9l-1.6 1.9A2.5 2.5 0 0 1 3 17.5l-1-5A4 4 0 0 1 6 8z" />
    <path d="M8 11v3M6.5 12.5h3M15.5 11.5h.01M18 13.5h.01" />
  </svg>
);

const MonitorIcon = () => (
  <svg width="19" height="19" viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <rect x="3" y="4" width="18" height="12" rx="2" />
    <path d="M8 20h8M12 16v4" />
  </svg>
);
