import {useCallback, useEffect, useMemo, useRef, useState} from 'react';

import {Modal} from './Modal';
import {deriveAccent, normalizeHex} from '../theme/deriveAccent';
import type {AccentRamp} from '../theme/accents';
import {t} from '../lib/i18n';

/**
 * Pick any colour, and see the Material 3 scheme it turns into before
 * committing to it.
 *
 * A saturation/value square over a hue rail, which is the arrangement people
 * already know from every other picker. The M3 part is what happens
 * underneath: the seed contributes its hue and nothing else, and the tones
 * come from the same Expressive scheme the five shipped accents are built
 * from, so a custom theme is the design system rather than a colour pasted
 * over it.
 *
 * The scheme is derived live for the preview, which is cheap, and only
 * applied on confirm.
 */

const SQUARE_H = 168;

function hsvToHex(h: number, s: number, v: number): string {
  const f = (n: number) => {
    const k = (n + h / 60) % 6;
    const value = v - v * s * Math.max(0, Math.min(k, 4 - k, 1));
    return Math.round(value * 255);
  };
  return `#${[f(5), f(3), f(1)].map(c => c.toString(16).padStart(2, '0')).join('')}`;
}

function hexToHsv(hex: string): {h: number; s: number; v: number} {
  const [r, g, b] = [0, 2, 4].map(i => parseInt(hex.slice(1 + i, 3 + i), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  return {h, s: max ? d / max : 0, v: max};
}

const clamp01 = (n: number) => Math.max(0, Math.min(1, n));

export function AccentPicker({
  open,
  initial,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  /** The colour to open on, so reopening does not start from scratch. */
  initial: string | null;
  onCancel: () => void;
  onConfirm: (seed: string) => void;
}) {
  const [hsv, setHsv] = useState(() => hexToHsv(initial ?? '#0099ff'));
  const [typed, setTyped] = useState<string | null>(null);
  const squareRef = useRef<HTMLDivElement>(null);
  const hueRef = useRef<HTMLDivElement>(null);

  // Reopening starts from whatever is applied now, not from the last drag.
  useEffect(() => {
    if (open) {
      setHsv(hexToHsv(initial ?? '#0099ff'));
      setTyped(null);
    }
  }, [open, initial]);

  const seed = useMemo(() => hsvToHex(hsv.h, hsv.s, hsv.v), [hsv]);

  // Deriving a full scheme is a few milliseconds, so the preview can follow
  // the drag rather than waiting for confirm. Only the apply waits.
  const preview = useMemo(() => deriveAccent(seed), [seed]);

  const drag = useCallback(
    (
      ref: React.RefObject<HTMLDivElement | null>,
      onMove: (x: number, y: number) => void,
    ) =>
      (event: React.PointerEvent) => {
        if (event.button !== 0) return;
        event.preventDefault();
        const apply = (clientX: number, clientY: number) => {
          const rect = ref.current?.getBoundingClientRect();
          if (!rect?.width) return;
          onMove(clamp01((clientX - rect.left) / rect.width), clamp01((clientY - rect.top) / rect.height));
        };
        apply(event.clientX, event.clientY);
        const move = (e: PointerEvent) => apply(e.clientX, e.clientY);
        const up = () => {
          window.removeEventListener('pointermove', move, true);
          window.removeEventListener('pointerup', up, true);
          window.removeEventListener('pointercancel', up, true);
        };
        window.addEventListener('pointermove', move, true);
        window.addEventListener('pointerup', up, true);
        window.addEventListener('pointercancel', up, true);
      },
    [],
  );

  const onSquare = drag(squareRef, (x, y) => {
    setTyped(null);
    setHsv(c => ({...c, s: x, v: 1 - y}));
  });
  const onHue = drag(hueRef, x => {
    setTyped(null);
    setHsv(c => ({...c, h: x * 360}));
  });

  const nudge = (event: React.KeyboardEvent, axis: 'h' | 'sv') => {
    const step = event.shiftKey ? 0.1 : 0.02;
    const map: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1],
    };
    const delta = map[event.key];
    if (!delta) return;
    event.preventDefault();
    setTyped(null);
    setHsv(c =>
      axis === 'h'
        ? {...c, h: (c.h + delta[0] * (event.shiftKey ? 30 : 6) + 360) % 360}
        : {...c, s: clamp01(c.s + delta[0] * step), v: clamp01(c.v + delta[1] * step)},
    );
  };

  const hueOnly = hsvToHex(hsv.h, 1, 1);
  const field = typed ?? seed.toUpperCase();

  return (
    <Modal open={open} title={t('accents.customTitle')} onClose={onCancel} footer={
      <>
        <button type="button" className="btn btn-quiet" onClick={onCancel}>
          {t('common.cancel')}
        </button>
        <button type="button" className="btn" onClick={() => onConfirm(seed)}>
          {t('accents.useThis')}
        </button>
      </>
    }>

      <div
        className="ap-square"
        ref={squareRef}
        style={{background: hueOnly, height: SQUARE_H}}
        role="slider"
        tabIndex={0}
        aria-label={t('accents.saturation')}
        aria-valuetext={field}
        onKeyDown={e => nudge(e, 'sv')}
        onPointerDown={onSquare}>
        <div className="ap-square-white" />
        <div className="ap-square-black" />
        <div
          className="ap-knob"
          style={{left: `${hsv.s * 100}%`, top: `${(1 - hsv.v) * 100}%`, background: seed}}
        />
      </div>

      <div
        className="ap-hue"
        ref={hueRef}
        role="slider"
        tabIndex={0}
        aria-label={t('accents.hue')}
        aria-valuemin={0}
        aria-valuemax={360}
        aria-valuenow={Math.round(hsv.h)}
        onKeyDown={e => nudge(e, 'h')}
        onPointerDown={onHue}>
        <div className="ap-knob ap-hue-knob" style={{left: `${(hsv.h / 360) * 100}%`, background: hueOnly}} />
      </div>

      <div className="ap-row">
        <span className="ap-chip" style={{background: seed}} aria-hidden="true" />
        <input
          className="ap-hex mono"
          value={field}
          aria-label={t('accents.hexLabel')}
          spellCheck={false}
          onChange={e => {
            setTyped(e.target.value);
            const parsed = normalizeHex(e.target.value);
            if (parsed) setHsv(hexToHsv(parsed));
          }}
          onBlur={() => setTyped(null)}
        />
      </div>

      <SchemePreview ramp={preview.ramp} />

      {preview.failures.length ? (
        <p className="ap-warning">{t('accents.lowContrast')}</p>
      ) : null}
    </Modal>
  );
}

/**
 * What the seed actually becomes.
 *
 * Showing the derived roles rather than the raw colour is the point: the
 * accent is nearly always a lighter, less saturated version of what was
 * picked, because M3 puts primary at tone 80 on a dark scheme. Without this
 * the confirm button looks like it ignored the choice.
 */
function SchemePreview({ramp}: {ramp: AccentRamp}) {
  return (
    <div className="ap-preview" style={{background: ramp.bg}}>
      <div className="ap-preview-card" style={{background: ramp.surfaceLow}}>
        <span className="ap-preview-title" style={{color: ramp.onSurface}}>
          {t('accents.previewTitle')}
        </span>
        <span className="ap-preview-meta" style={{color: ramp.onSurfaceVariant}}>
          {t('accents.previewMeta')}
        </span>
        <div className="ap-preview-row">
          <span className="ap-preview-btn" style={{background: ramp.base, color: ramp.onBase}}>
            {t('accents.previewPrimary')}
          </span>
          <span
            className="ap-preview-btn"
            style={{background: ramp.secondaryContainer, color: ramp.onSecondaryContainer}}>
            {t('accents.previewQuiet')}
          </span>
          <span
            className="ap-preview-chip"
            style={{background: ramp.primaryContainer, color: ramp.onPrimaryContainer}}>
            {t('accents.previewChip')}
          </span>
        </div>
      </div>
    </div>
  );
}
