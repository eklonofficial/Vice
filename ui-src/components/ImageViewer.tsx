import {useCallback, useEffect, useLayoutEffect, useRef, useState} from 'react';

import {api} from '../lib/api';
import {useEscape} from '../lib/escape';
import {useExitTransition} from '../lib/exit';
import {formatBytes} from '../lib/format';
import {DEFAULT_MARK_COLOR, MARK_COLORS} from '../lib/palette';
import {imageTitle, type Image} from '../lib/types';
import {IconClose, IconExpand} from './Icons';
import {InlineRename} from './InlineRename';
import {t} from '../lib/i18n';

/** Pen width in image pixels, so a stroke is the same weight on any monitor. */
const PEN_WIDTH = 6;

/** The video viewer's expand timing, so both viewers grow the same way. */
const MORPH = {duration: 280, easing: 'cubic-bezier(0.2, 0, 0, 1)'};

interface MorphFrom {
  dialog: DOMRect;
  dialogRadius: string;
  stage: DOMRect;
  stageRadius: string;
  frame: DOMRect;
}

interface MorphRun {
  scrim: HTMLElement;
  ghosts: HTMLElement[];
  animations: Animation[];
}

interface Stroke {
  color: string;
  /** Image-space points, in pairs. */
  points: {x: number; y: number}[];
}

export interface ImageViewerProps {
  image: Image | null;
  /** Every image in the grid behind, in order, which is what prev and next walk. */
  images: Image[];
  onSelect: (slug: string) => void;
  onClose: () => void;
  shortcutsBlocked?: boolean;
  onRename: (image: Image, name: string) => void;
  onCopy: (image: Image) => void;
  onReveal: (image: Image) => void;
  onDelete: (image: Image) => void;
  notify: (title: string, detail?: string, tone?: 'accent' | 'error') => void;
}

/**
 * A picture, and a pen over it.
 *
 * The canvas sits at the file's natural resolution and is scaled down by CSS,
 * so a stroke drawn on a 3440 wide screenshot is saved at 3440 wide rather than
 * at whatever size the window happened to be. Strokes are kept as points and
 * redrawn from scratch on every change, which is what makes undo a pop rather
 * than a second layer of bookkeeping.
 */
export function ImageViewer(props: ImageViewerProps) {
  const {images, onSelect, onClose} = props;

  const {mounted, closing} = useExitTransition(props.image !== null, 320);
  const last = useRef(props.image);
  if (props.image) last.current = props.image;
  const image = props.image ?? last.current;

  const imgRef = useRef<HTMLImageElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const drawing = useRef<Stroke | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const expandRef = useRef<HTMLButtonElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const morphFrom = useRef<MorphFrom | null>(null);
  const expandedRef = useRef(false);
  const morphRun = useRef<MorphRun | null>(null);

  const [strokes, setStrokes] = useState<Stroke[]>([]);
  const [color, setColor] = useState(DEFAULT_MARK_COLOR);
  const [pen, setPen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [renamingTitle, setRenamingTitle] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const index = image ? images.findIndex(i => i.slug === image.slug) : -1;
  const open = props.image !== null;
  const dirty = strokes.length > 0;

  /*
   * The enlarged picture's box is written straight onto the element rather than
   * derived in CSS. Container units resolve to zero for the first frame after
   * the stage becomes a container, which collapsed the picture to a point and,
   * because the morph measures that box, skipped the animation with it.
   */
  const sizeFrame = useCallback(() => {
    const stage = stageRef.current;
    const frame = frameRef.current;
    if (!stage || !frame) return;
    if (!expandedRef.current) {
      frame.style.width = '';
      frame.style.height = '';
      return;
    }
    const natural = imgRef.current;
    const width = natural?.naturalWidth || image?.width || 0;
    const height = natural?.naturalHeight || image?.height || 0;
    const box = stage.getBoundingClientRect();
    if (!width || !height || !box.width || !box.height) return;
    const scale = Math.min(box.width / width, box.height / height);
    frame.style.width = `${width * scale}px`;
    frame.style.height = `${height * scale}px`;
  }, [image?.width, image?.height]);

  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (!stage || !open) return;
    const observer = new ResizeObserver(sizeFrame);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [open, sizeFrame]);

  const stopMorph = useCallback(() => {
    const run = morphRun.current;
    morphRun.current = null;
    if (!run) return;
    for (const animation of run.animations) animation.cancel();
    for (const ghost of run.ghosts) ghost.remove();
    delete run.scrim.dataset.morphing;
  }, []);

  useEffect(() => stopMorph, [stopMorph]);

  // Read while everything is at rest, or mid-morph from the ghosts, so a
  // second toggle reverses from where the surface actually is.
  const toggleExpanded = useCallback(() => {
    const dialog = dialogRef.current;
    const stage = stageRef.current;
    const frame = frameRef.current;
    const [surfaceGhost, stageGhost] = morphRun.current?.ghosts ?? [];
    const surface = surfaceGhost ?? dialog;
    const backdrop = stageGhost ?? stage;
    morphFrom.current = surface && backdrop && frame ? {
      dialog: surface.getBoundingClientRect(),
      dialogRadius: getComputedStyle(surface).borderTopLeftRadius,
      stage: backdrop.getBoundingClientRect(),
      stageRadius: getComputedStyle(backdrop).borderTopLeftRadius,
      frame: frame.getBoundingClientRect(),
    } : null;
    setExpanded(value => !value);
  }, []);

  /*
   * A container transform rather than the video viewer's single FLIP: that one
   * moves only the stage, but here the header and tools come along, and scaling
   * them would stretch the text. So the two surfaces are stood in for by plain
   * boxes that animate their real size, the picture and its ink move as one
   * unit so strokes stay registered, and the controls fade in once most of the
   * distance is covered.
   */
  useLayoutEffect(() => {
    const from = morphFrom.current;
    morphFrom.current = null;
    expandedRef.current = expanded;
    sizeFrame();
    stopMorph();
    const dialog = dialogRef.current;
    const stage = stageRef.current;
    const frame = frameRef.current;
    const scrim = dialog?.closest<HTMLElement>('.scrim');
    const stack = dialog?.parentElement;
    if (!from || !dialog || !stage || !frame || !scrim || !stack || typeof frame.animate !== 'function') return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      || document.documentElement.classList.contains('perf-low')) return;

    const to = frame.getBoundingClientRect();
    if (!from.frame.width || !to.width) return;
    const box = (rect: DOMRect, radius: string): Keyframe => ({
      left: `${rect.left}px`, top: `${rect.top}px`,
      width: `${rect.width}px`, height: `${rect.height}px`, borderRadius: radius,
    });
    const run: MorphRun = {scrim, ghosts: [], animations: []};
    const ghost = (kind: string, a: DOMRect, ra: string, target: HTMLElement) => {
      const element = document.createElement('div');
      element.className = `image-morph image-morph-${kind}`;
      element.setAttribute('aria-hidden', 'true');
      scrim.insertBefore(element, stack);
      run.ghosts.push(element);
      const b = target.getBoundingClientRect();
      run.animations.push(element.animate(
        [box(a, ra), box(b, getComputedStyle(target).borderTopLeftRadius)], {...MORPH, fill: 'forwards'}));
    };
    ghost('surface', from.dialog, from.dialogRadius, dialog);
    ghost('stage', from.stage, from.stageRadius, stage);
    scrim.dataset.morphing = '';

    // Growing, the real dialog already has its window-sized layout, so it is
    // clipped to the surface or its header would sit out past the edges of a
    // box still on its way there. Shrinking is left unclipped: the dialog is
    // always inside the surface, and a clip would crop the picture on its way in.
    if (expanded) {
      const d = dialog.getBoundingClientRect();
      const inset = (r: DOMRect, radius: string) => `inset(${r.top - d.top}px ${d.right - r.right}px `
        + `${d.bottom - r.bottom}px ${r.left - d.left}px round ${radius})`;
      run.animations.push(dialog.animate([
        {clipPath: inset(from.dialog, from.dialogRadius)},
        {clipPath: inset(d, getComputedStyle(dialog).borderTopLeftRadius)},
      ], MORPH));
    }

    run.animations.push(frame.animate([
      {transformOrigin: '0 0', transform: `translate(${from.frame.left - to.left}px, ${from.frame.top - to.top}px) `
        + `scale(${from.frame.width / to.width}, ${from.frame.height / to.height})`},
      {transformOrigin: '0 0', transform: 'none'},
    ], MORPH));
    // Shrinking, the controls are already where they end up, under a picture
    // still larger than its stage, so they wait until it has nearly arrived.
    const reveal = expanded ? 0.35 : 0.6;
    for (const chrome of dialog.querySelectorAll<HTMLElement>(':scope > .viewer-head, :scope > .viewer-bottom')) {
      run.animations.push(chrome.animate([{opacity: 0}, {opacity: 0, offset: reveal}, {opacity: 1}], MORPH));
    }
    morphRun.current = run;
    void Promise.all(run.animations.map(animation => animation.finished)).then(
      () => { if (morphRun.current === run) stopMorph(); },
      () => undefined,
    );
  }, [expanded, sizeFrame, stopMorph]);

  useEffect(() => {
    if (!open) {
      stopMorph();
      setExpanded(false);
      setRenamingTitle(false);
      return;
    }
    const previous = document.activeElement as HTMLElement | null;
    expandRef.current?.focus({preventScroll: true});
    return () => {
      if (previous?.isConnected) previous.focus({preventScroll: true});
    };
  }, [open, stopMorph]);

  // A different picture is a different drawing. Nothing is carried across.
  useEffect(() => {
    setStrokes([]);
    drawing.current = null;
  }, [image?.slug]);

  const step = useCallback(
    (delta: number) => {
      if (index < 0) return;
      const next = images[index + delta];
      if (next) onSelect(next.slug);
    },
    [images, index, onSelect],
  );

  // The canvas has to match the file, not the element, or every stroke lands
  // scaled and blurred at whatever the window size happened to be.
  const sizeCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img) return;
    const w = img.naturalWidth || image?.width || 0;
    const h = img.naturalHeight || image?.height || 0;
    if (!w || !h) return;
    sizeFrame();
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
  }, [image?.width, image?.height, sizeFrame]);

  useLayoutEffect(sizeCanvas, [sizeCanvas, image?.slug]);

  // Redraw everything, every time. At a handful of strokes this is cheaper
  // than tracking what changed, and it is what makes undo exact.
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = PEN_WIDTH;
    for (const stroke of strokes) paintStroke(ctx, stroke);
  }, [strokes]);

  useEscape(open && !props.shortcutsBlocked, useCallback(() => {
    if (expanded) toggleExpanded();
    else onClose();
  }, [expanded, onClose, toggleExpanded]));

  useEffect(() => {
    if (!open || closing || props.shortcutsBlocked) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      const target = e.target instanceof HTMLElement ? e.target : null;
      if (e.key === 'Tab') {
        const controls = dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, textarea, [tabindex="0"]');
        const first = controls?.[0];
        const last = controls?.[controls.length - 1];
        if (e.shiftKey && target === first) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && target === last) { e.preventDefault(); first?.focus(); }
        return;
      }
      if (target?.closest('input, textarea, select') || target?.isContentEditable || e.repeat) return;
      if (e.key.toLowerCase() === 'z' && (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey) {
        e.preventDefault();
        setStrokes(list => list.slice(0, -1));
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        step(-1);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        step(1);
      } else if (e.key.toLowerCase() === 'f') {
        e.preventDefault();
        toggleExpanded();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, closing, props.shortcutsBlocked, step, toggleExpanded]);

  if (!mounted || !image) return null;

  const pointAt = (event: React.PointerEvent): {x: number; y: number} | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return {
      x: ((event.clientX - rect.left) / rect.width) * canvas.width,
      y: ((event.clientY - rect.top) / rect.height) * canvas.height,
    };
  };

  const beginStroke = (event: React.PointerEvent) => {
    if (!pen || event.button !== 0) return;
    const at = pointAt(event);
    if (!at) return;
    event.preventDefault();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch (err) {
      // Capture is what keeps a drag that leaves the canvas attached. Losing
      // it costs the tail of one stroke; refusing to draw would cost the lot.
      console.debug('Capturing the pointer failed', err);
    }
    // The stroke is captured in a local, never read back off the ref inside
    // the updater. React can replay a queued updater, and by then pointerup
    // has set the ref to null, which appended a null stroke and took the
    // whole render down with it.
    const stroke: Stroke = {color, points: [at]};
    drawing.current = stroke;
    setStrokes(list => [...list, stroke]);
  };

  const extendStroke = (event: React.PointerEvent) => {
    const stroke = drawing.current;
    if (!stroke) return;
    const at = pointAt(event);
    if (!at) return;
    stroke.points.push(at);
    // The stroke object is mutated in place, so the array identity is what
    // tells React a repaint is due.
    setStrokes(list => [...list]);
  };

  const endStroke = () => {
    drawing.current = null;
  };

  const save = async () => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img || !dirty) return;
    setSaving(true);
    try {
      const flat = document.createElement('canvas');
      flat.width = canvas.width;
      flat.height = canvas.height;
      const ctx = flat.getContext('2d');
      if (!ctx) throw new Error(t('images.errCanvas'));
      ctx.drawImage(img, 0, 0, flat.width, flat.height);
      ctx.drawImage(canvas, 0, 0);
      const png = await new Promise<Blob | null>(resolve => flat.toBlob(resolve, 'image/png'));
      if (!png) throw new Error(t('images.errCanvas'));
      await api.annotateImage(image.slug, png);
      setStrokes([]);
      props.notify(t('images.saved'), undefined, 'accent');
    } catch (err) {
      props.notify(t('images.errSave'), (err as Error).message, 'error');
    } finally {
      setSaving(false);
    }
  };

  const meta = [
    image.width ? `${image.width}x${image.height}` : '',
    image.size ? formatBytes(image.size) : '',
  ]
    .filter(Boolean)
    .join(' · ');


  return (
    <div
      className="scrim viewer-scrim image-viewer-scrim"
      data-expanded={expanded || undefined}
      data-closing={closing || undefined}
      onMouseDown={e => e.target === e.currentTarget && onClose()}>
      <div className="viewer-stack">
        <div
          className="modal viewer image-viewer"
          ref={dialogRef}
          data-closing={closing || undefined}
          role="dialog"
          aria-modal="true"
          aria-label={imageTitle(image)}>
          <header className="viewer-head">
            <div className="viewer-heading">
              {renamingTitle ? (
                <InlineRename
                  className="viewer-rename"
                  label={t('card.nameLabel')}
                  initial={imageTitle(image)}
                  onCancel={() => setRenamingTitle(false)}
                  onSubmit={name => {
                    setRenamingTitle(false);
                    props.onRename(image, name);
                  }}
                />
              ) : (
                <h2 title={t('viewer.doubleClickRename')} onDoubleClick={() => setRenamingTitle(true)}>
                  {imageTitle(image)}
                </h2>
              )}
              <p className="mono">{meta}</p>
            </div>
            {images.length > 1 ? (
              <span className="viewer-count mono">
                {index + 1} / {images.length}
              </span>
            ) : null}
            <button
              type="button"
              className="viewer-nav"
              onClick={() => step(-1)}
              disabled={index <= 0}
              aria-label={t('images.prev')}>
              <Chevron dir="left" />
            </button>
            <button
              type="button"
              className="viewer-nav"
              onClick={() => step(1)}
              disabled={index < 0 || index >= images.length - 1}
              aria-label={t('images.next')}>
              <Chevron dir="right" />
            </button>
            <button
              ref={expandRef}
              type="button"
              className="viewer-nav image-expand"
              onClick={toggleExpanded}
              aria-label={t(expanded ? 'viewer.shrink' : 'viewer.expand')}
              title={t(expanded ? 'viewer.shrinkHint' : 'viewer.expandHint')}
              aria-pressed={expanded}>
              <IconExpand collapse={expanded} />
            </button>
            <button
              type="button"
              className="modal-close"
              onClick={onClose}
              aria-label={t('common.close')}>
              <IconClose size={15} />
            </button>
          </header>

          <div className="image-stage" ref={stageRef}
            onDoubleClick={() => { if (!pen) toggleExpanded(); }}>
            <div className="image-frame" ref={frameRef}>
              <img
                ref={imgRef}
                className="image-full"
                src={image.image_url}
                alt={imageTitle(image)}
                draggable={false}
                onLoad={sizeCanvas}
              />
              <canvas
                ref={canvasRef}
                className="image-ink"
                data-pen={pen || undefined}
                onPointerDown={beginStroke}
                onPointerMove={extendStroke}
                onPointerUp={endStroke}
                onPointerCancel={endStroke}
              />
            </div>
          </div>

          <div className="viewer-bottom">
            <div className="image-tools">
              <button
                type="button"
                className="btn btn-quiet btn-sm"
                aria-pressed={pen}
                data-active={pen || undefined}
                onClick={() => setPen(on => !on)}>
                {pen ? t('images.penOn') : t('images.pen')}
              </button>

              <div className="image-swatches" role="group" aria-label={t('images.penColour')}>
                {MARK_COLORS.map(swatch => (
                  <button
                    key={swatch}
                    type="button"
                    className="hl-picker-dot"
                    data-active={swatch === color || undefined}
                    style={{background: swatch}}
                    title={swatch}
                    aria-label={t('viewer.useColour', {color: swatch})}
                    onClick={() => {
                      setColor(swatch);
                      setPen(true);
                    }}
                  />
                ))}
              </div>

              <button
                type="button"
                className="btn btn-quiet btn-sm"
                disabled={!dirty}
                onClick={() => setStrokes(list => list.slice(0, -1))}>
                {t('images.undo')}
              </button>
              <button type="button" className="btn btn-sm" disabled={!dirty || saving} onClick={() => void save()}>
                {saving ? t('images.saving') : t('images.saveDrawing')}
              </button>
            </div>

            <footer className="viewer-foot">
              <span className="viewer-shortcuts mono">{t('images.shortcuts')}</span>
              <div className="viewer-foot-btns">
                <button
                  type="button"
                  className="btn btn-quiet btn-sm"
                  onClick={() => props.onCopy(image)}>
                  {t('images.copy')}
                </button>
                <button
                  type="button"
                  className="btn btn-quiet btn-sm"
                  onClick={() => props.onReveal(image)}>
                  {t('viewer.reveal')}
                </button>
                <button
                  type="button"
                  className="btn btn-quiet btn-danger btn-sm"
                  onClick={() => props.onDelete(image)}>
                  {t('viewer.delete')}
                </button>
              </div>
            </footer>
          </div>
        </div>
      </div>
    </div>
  );
}

function paintStroke(ctx: CanvasRenderingContext2D, stroke: Stroke): void {
  const {points} = stroke;
  if (points.length === 0) return;
  ctx.strokeStyle = stroke.color;
  if (points.length === 1) {
    // A tap is a dot. Without this the pen does nothing until you move it.
    ctx.fillStyle = stroke.color;
    ctx.beginPath();
    ctx.arc(points[0].x, points[0].y, PEN_WIDTH / 2, 0, Math.PI * 2);
    ctx.fill();
    return;
  }
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (let i = 1; i < points.length; i++) ctx.lineTo(points[i].x, points[i].y);
  ctx.stroke();
}

const Chevron = ({dir}: {dir: 'left' | 'right'}) => (
  <svg
    width={16}
    height={16}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true">
    <path d={dir === 'left' ? 'm15 6-6 6 6 6' : 'm9 6 6 6-6 6'} />
  </svg>
);
