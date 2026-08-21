import {useCallback, useEffect, useLayoutEffect, useRef, useState} from 'react';

import {api} from '../lib/api';
import {useEscape} from '../lib/escape';
import {useExitTransition} from '../lib/exit';
import {formatBytes, formatDuration} from '../lib/format';
import {
  clipNeedsProxy,
  playQuietly,
  playbackUrl,
  seekTo,
  useVideoFailure,
  videoFailureMessage,
} from '../lib/playback';
import {clipTitle, type Clip, type Highlight} from '../lib/types';
import {IconClose} from './Icons';
import {t, tNode} from '../lib/i18n';

/** Cycled by index as highlights are added, so a clip's marks stay distinct. */
const HIGHLIGHT_COLORS = [
  '#f59e0b',
  '#ef4444',
  '#22c55e',
  '#3b82f6',
  '#ec4899',
  '#8b5cf6',
  '#06b6d4',
  '#f97316',
];

const DEFAULT_HIGHLIGHT_COLOR = HIGHLIGHT_COLORS[0];

export interface ViewerProps {
  /** The clip on screen, or null when the viewer is closed. */
  clip: Clip | null;
  /** Every clip, in order, which is what prev and next walk. */
  clips: Clip[];
  highlights: Highlight[];
  onHighlightsChange: (next: Highlight[]) => void;
  onSelect: (slug: string) => void;
  onClose: () => void;
  onTrim: (clip: Clip) => void;
  onShare: (clip: Clip) => void;
  onReveal: (clip: Clip) => void;
  onDelete: (clip: Clip) => void;
  onOpenExternally: (clip: Clip) => void;
  notify: (title: string, detail?: string, tone?: 'accent' | 'error') => void;
}

/**
 * The viewer: one playback element, two surfaces around it.
 *
 * The modal shows the video and its highlights; the bar below carries the
 * transport and mirrors the same element. They open and close together, and
 * the bar sits in the same column as the modal so it inherits the modal's
 * width and centring rather than measuring them. The old UI floated the bar
 * separately and had to read offsetLeft rather than a client rect, because the
 * modal animates in under a scale() transform that skews a live rect; laying
 * them out together removes the measurement, and with it that whole hazard.
 */
export function Viewer(props: ViewerProps) {
  const {clips, highlights, onHighlightsChange, onSelect, onClose} = props;

  // The viewer is held on screen for the length of its exit so it animates
  // away instead of vanishing between frames. props.clip is already null by
  // then, so the last one is kept to render against; the video is stopped the
  // moment closing starts, or it would keep playing audio behind the fade.
  const {mounted, closing} = useExitTransition(props.clip !== null, 320);
  const lastClip = useRef(props.clip);
  if (props.clip) lastClip.current = props.clip;
  const clip = props.clip ?? lastClip.current;

  const videoRef = useRef<HTMLVideoElement>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({current: 0, duration: 0});
  const [paused, setPaused] = useState(true);
  const [preparing, setPreparing] = useState(false);
  const [dragging, setDragging] = useState<string | null>(null);
  const [picker, setPicker] = useState<{id: string; color: string; at: {x: number; y: number}} | null>(
    null,
  );
  const [renaming, setRenaming] = useState<string | null>(null);
  const failed = useVideoFailure(videoRef);
  const [volume, setVolume] = useState<number>(readStoredVolume);
  const lastVolume = useRef(volume > 0 ? volume : 1);

  const applyVolume = useCallback((next: number) => {
    const clamped = clamp01(next);
    setVolume(clamped);
    if (clamped > 0) lastVolume.current = clamped;
    try {
      localStorage.setItem(VOLUME_STORAGE_KEY, String(clamped));
    } catch {

    }
  }, []);

  const toggleMute = useCallback(() => {
    applyVolume(volume > 0 ? 0 : lastVolume.current);
  }, [applyVolume, volume]);

  useEffect(() => {
    const video = videoRef.current;
    if (video) video.volume = volume;
  }, [volume, clip]);

  const index = clip ? clips.findIndex(c => c.slug === clip.slug) : -1;
  const open = clip !== null;

  useLayoutEffect(() => {
    const video = videoRef.current;
    if (!video || !clip) return;
    const src = playbackUrl(clip);
    if (video.getAttribute('src') === src) {
      playQuietly(video);
      return;
    }
    video.pause();
    video.src = src;
    video.load();
    setPreparing(clipNeedsProxy(clip));
    setPosition({current: 0, duration: 0});
    playQuietly(video);
    void api
      .markViewed(clip.slug)
      .catch(err => console.debug('Recording the view failed', err));
  }, [clip]);

  // Release the decoder when the viewer goes, not merely pause it.
  useEffect(() => {
    if (open) return;
    const video = videoRef.current;
    if (!video?.getAttribute('src')) return;
    video.pause();
    video.removeAttribute('src');
    video.load();
  }, [open]);

  const step = useCallback(
    (delta: number) => {
      if (index < 0) return;
      const next = clips[index + delta];
      if (next) onSelect(next.slug);
    },
    [clips, index, onSelect],
  );

  const addHighlight = useCallback(async () => {
    const video = videoRef.current;
    if (!clip || !video) return;
    const time = video.currentTime;
    const label = nextHighlightLabel(highlights);
    const color = HIGHLIGHT_COLORS[highlights.length % HIGHLIGHT_COLORS.length];
    try {
      const result = await api.addHighlight(clip.slug, {time, label, color});
      if (!result.highlight) throw new Error(result.error || t('viewer.daemonNoMark'));
      onHighlightsChange(sortHighlights([...highlights, result.highlight]));
      props.notify(`${label} at ${formatDuration(time, true)}`, undefined, 'accent');
    } catch (err) {
      props.notify(t('viewer.errAddHighlight'), (err as Error).message, 'error');
    }
  }, [clip, highlights, onHighlightsChange, props]);

  useEscape(open, onClose);

  useEffect(() => {
    if (closing) videoRef.current?.pause();
  }, [closing]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        step(-1);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        step(1);
      } else if (e.key === 'h' || e.key === 'H') {
        e.preventDefault();
        void addHighlight();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, step, addHighlight]);

  if (!mounted || !clip) return null;

  const duration = position.duration;
  const percent = duration > 0 ? (position.current / duration) * 100 : 0;

  const toggle = () => {
    const video = videoRef.current;
    if (!video?.getAttribute('src')) return;
    if (video.paused) playQuietly(video);
    else video.pause();
  };

  const seekFromPointer = (clientX: number) => {
    const track = timelineRef.current;
    if (!track || !duration) return;
    const rect = track.getBoundingClientRect();
    seekTo(videoRef.current, clamp01((clientX - rect.left) / rect.width) * duration);
  };

  const beginHighlightDrag = (event: React.PointerEvent, highlight: Highlight) => {
    if (event.button !== 0 || !duration) return;
    event.preventDefault();
    event.stopPropagation();
    setDragging(highlight.id);

    const timeAt = (clientX: number) => {
      const rect = timelineRef.current?.getBoundingClientRect();
      if (!rect) return null;
      return clamp01((clientX - rect.left) / rect.width) * duration;
    };

    let latest = highlight.time;
    const move = (moveEvent: PointerEvent) => {
      const next = timeAt(moveEvent.clientX);
      if (next == null) return;
      latest = next;
      onHighlightsChange(sortHighlights(replaceTime(highlights, highlight.id, round3(next))));
    };
    const up = async (upEvent: PointerEvent) => {
      window.removeEventListener('pointermove', move, true);
      window.removeEventListener('pointerup', up, true);
      window.removeEventListener('pointercancel', up, true);
      setDragging(null);
      const applied = round3(timeAt(upEvent.clientX) ?? latest);
      onHighlightsChange(sortHighlights(replaceTime(highlights, highlight.id, applied)));
      try {
        const result = await api.updateHighlight(clip.slug, highlight.id, {time: applied});
        if (result.ok === false) throw new Error(result.error || t('viewer.daemonRejectedMove'));
        props.notify(`Highlight moved to ${formatDuration(applied, true)}`, undefined, 'accent');
      } catch (err) {
        // Put it back where it was. An optimistic move that silently did not
        // persist is worse than one that visibly snaps back.
        onHighlightsChange(sortHighlights(replaceTime(highlights, highlight.id, highlight.time)));
        props.notify(t('viewer.errMoveHighlight'), (err as Error).message, 'error');
      }
    };
    window.addEventListener('pointermove', move, true);
    window.addEventListener('pointerup', up, true);
    window.addEventListener('pointercancel', up, true);
  };

  const patchHighlight = async (id: string, body: Partial<Omit<Highlight, 'id'>>) => {
    try {
      const result = await api.updateHighlight(clip.slug, id, body);
      if (result.ok === false) throw new Error(result.error || t('viewer.daemonRejectedChange'));
      onHighlightsChange(
        sortHighlights(highlights.map(h => (h.id === id ? {...h, ...body} : h))),
      );
    } catch (err) {
      props.notify(t('viewer.errUpdateHighlight'), (err as Error).message, 'error');
    }
  };

  const removeHighlight = async (id: string) => {
    try {
      await api.deleteHighlight(clip.slug, id);
      onHighlightsChange(highlights.filter(h => h.id !== id));
    } catch (err) {
      props.notify(t('viewer.errDeleteHighlight'), (err as Error).message, 'error');
    }
  };

  const meta = [
    clip.duration ? formatDuration(Math.round(clip.duration), true) : '',
    clip.width ? `${clip.width}x${clip.height}` : '',
    clip.size ? formatBytes(clip.size) : '',
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div
      className="scrim viewer-scrim"
      data-closing={closing || undefined}
      onMouseDown={e => e.target === e.currentTarget && onClose()}>
      <div className="viewer-stack">
        <div
          className="modal viewer"
          data-closing={closing || undefined}
          role="dialog"
          aria-modal="true"
          aria-label={clipTitle(clip)}>
          <header className="viewer-head">
            <div className="viewer-heading">
              <h2>{clipTitle(clip)}</h2>
              <p className="mono">{meta}</p>
            </div>
            {clips.length > 1 ? (
              <span className="viewer-count mono">
                {index + 1} / {clips.length}
              </span>
            ) : null}
            <button
              type="button"
              className="viewer-nav"
              onClick={() => step(-1)}
              disabled={index <= 0}
              aria-label={t('viewer.prevClip')}>
              <Chevron dir="left" />
            </button>
            <button
              type="button"
              className="viewer-nav"
              onClick={() => step(1)}
              disabled={index < 0 || index >= clips.length - 1}
              aria-label={t('viewer.nextClip')}>
              <Chevron dir="right" />
            </button>
            <button type="button" className="modal-close" onClick={onClose} aria-label={t('common.close')}>
              <IconClose size={15} />
            </button>
          </header>

          <div className="viewer-stage" onClick={toggle}>
            <video
              ref={videoRef}
              className="viewer-video"
              playsInline
              onTimeUpdate={e =>
                setPosition({
                  current: e.currentTarget.currentTime,
                  duration: Number.isFinite(e.currentTarget.duration)
                    ? e.currentTarget.duration
                    : 0,
                })
              }
              onLoadedMetadata={e =>
                setPosition({
                  current: e.currentTarget.currentTime,
                  duration: Number.isFinite(e.currentTarget.duration)
                    ? e.currentTarget.duration
                    : 0,
                })
              }
              onPlay={() => setPaused(false)}
              onPause={() => setPaused(true)}
              onCanPlay={() => setPreparing(false)}
              onLoadedData={() => setPreparing(false)}
              onError={() => setPreparing(false)}
            />
            <button
              type="button"
              className="viewer-play"
              data-paused={paused || undefined}
              aria-label={paused ? t('viewer.play') : t('viewer.pause')}>
              {paused ? <PlayGlyph /> : <PauseGlyph />}
            </button>
            <span className="viewer-timebadge mono">
              {formatDuration(position.current, true)} / {formatDuration(duration, true)}
            </span>

            {preparing ? (
              <div className="video-overlay" onClick={e => e.stopPropagation()}>
                <span className="video-spinner" aria-hidden="true" />
                <p>{t('viewer.preparingPreview')}</p>
              </div>
            ) : null}

            {failed ? (
              <div className="video-overlay" onClick={e => e.stopPropagation()}>
                <p>{videoFailureMessage()}</p>
                <div className="video-overlay-actions">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => props.onOpenExternally(clip)}>
                    {t('viewer.openInPlayer')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-quiet"
                    onClick={() => props.onReveal(clip)}>
                    {t('viewer.showInFolder')}
                  </button>
                </div>
              </div>
            ) : null}
          </div>

          <div className="viewer-bottom">
            <div
              className="viewer-timeline"
              ref={timelineRef}
              onClick={e => seekFromPointer(e.clientX)}>
              <div className="viewer-progress" style={{width: `${percent}%`}} />
              <div className="viewer-playhead" style={{left: `${percent}%`}} />
              {highlights.map(h => (
                <button
                  key={h.id}
                  type="button"
                  className="hl-marker"
                  data-dragging={dragging === h.id || undefined}
                  style={{
                    left: `${duration ? (h.time / duration) * 100 : 0}%`,
                    background: h.color || DEFAULT_HIGHLIGHT_COLOR,
                  }}
                  title={`${h.label}, ${formatDuration(h.time, true)}, drag to move`}
                  aria-label={t('viewer.highlightAt', {label: h.label, time: formatDuration(h.time, true)})}
                  onPointerDown={e => beginHighlightDrag(e, h)}
                  onClick={e => {
                    e.stopPropagation();
                    if (!dragging) seekTo(videoRef.current, h.time);
                  }}
                />
              ))}
            </div>

            <div className="viewer-hl-head">
              <span className="eyebrow">{t('viewer.highlightsHeading')}</span>
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => void addHighlight()}>
                {tNode('viewer.addHighlightKey', {key: <kbd key="k">H</kbd>})}
              </button>
            </div>

            <div className="viewer-hl-list">
              {highlights.length === 0 ? (
                <p className="viewer-hl-empty">
                  {tNode('viewer.noHighlightsHelp', {key: <kbd key="k">H</kbd>})}
                </p>
              ) : (
                highlights.map(h => (
                  <div className="hl-item" key={h.id} onClick={() => seekTo(videoRef.current, h.time)}>
                    <button
                      type="button"
                      className="hl-swatch"
                      style={{background: h.color || DEFAULT_HIGHLIGHT_COLOR}}
                      title={t('viewer.changeColour')}
                      aria-label={t('viewer.changeColourAria', {name: h.label})}
                      onClick={e => {
                        e.stopPropagation();
                        const rect = e.currentTarget.getBoundingClientRect();
                        setPicker({
                          id: h.id,
                          color: h.color || DEFAULT_HIGHLIGHT_COLOR,
                          at: {x: rect.left, y: rect.bottom + 6},
                        });
                      }}
                    />
                    <span className="hl-time mono">{formatDuration(h.time, true)}</span>
                    {renaming === h.id ? (
                      <HighlightRename
                        initial={h.label}
                        onCancel={() => setRenaming(null)}
                        onSubmit={label => {
                          setRenaming(null);
                          void patchHighlight(h.id, {label});
                        }}
                      />
                    ) : (
                      <span
                        className="hl-label"
                        title={t('viewer.doubleClickRename')}
                        onDoubleClick={e => {
                          e.stopPropagation();
                          setRenaming(h.id);
                        }}>
                        {h.label}
                      </span>
                    )}
                    <button
                      type="button"
                      className="hl-del"
                      title={t('viewer.removeHighlight')}
                      aria-label={t('viewer.removeHighlightAria', {name: h.label})}
                      onClick={e => {
                        e.stopPropagation();
                        void removeHighlight(h.id);
                      }}>
                      <IconClose size={12} />
                    </button>
                  </div>
                ))
              )}
            </div>

            <footer className="viewer-foot">
              <span className="viewer-shortcuts mono">
                {t('viewer.shortcuts')}
              </span>
              <div className="viewer-foot-btns">
                <button type="button" className="btn btn-quiet btn-sm" onClick={() => props.onTrim(clip)}>
                  {t('viewer.trim')}
                </button>
                <button type="button" className="btn btn-quiet btn-sm" onClick={() => props.onShare(clip)}>
                  {t('viewer.share')}
                </button>
                <button type="button" className="btn btn-quiet btn-sm" onClick={() => props.onReveal(clip)}>
                  {t('viewer.reveal')}
                </button>
                <button
                  type="button"
                  className="btn btn-quiet btn-danger btn-sm"
                  onClick={() => props.onDelete(clip)}>
                  {t('viewer.delete')}
                </button>
              </div>
            </footer>
          </div>
        </div>

        <PlayerBar
          clip={clip}
          paused={paused}
          current={position.current}
          duration={duration}
          canStepBack={index > 0}
          canStepForward={index >= 0 && index < clips.length - 1}
          volume={volume}
          onToggle={toggle}
          onStep={step}
          onSeek={ratio => seekTo(videoRef.current, ratio * duration)}
          onVolumeChange={applyVolume}
          onToggleMute={toggleMute}
          onShare={() => props.onShare(clip)}
          onClose={onClose}
        />
      </div>

      {picker ? (
        <ColorPicker
          at={picker.at}
          current={picker.color}
          onClose={() => setPicker(null)}
          onPick={color => {
            setPicker(null);
            void patchHighlight(picker.id, {color});
          }}
        />
      ) : null}
    </div>
  );
}

function PlayerBar({
  clip,
  paused,
  current,
  duration,
  canStepBack,
  canStepForward,
  volume,
  onToggle,
  onStep,
  onSeek,
  onVolumeChange,
  onToggleMute,
  onShare,
  onClose,
}: {
  clip: Clip;
  paused: boolean;
  current: number;
  duration: number;
  canStepBack: boolean;
  canStepForward: boolean;
  volume: number;
  onToggle: () => void;
  onStep: (delta: number) => void;
  onSeek: (ratio: number) => void;
  onVolumeChange: (next: number) => void;
  onToggleMute: () => void;
  onShare: () => void;
  onClose: () => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const percent = duration > 0 ? (current / duration) * 100 : 0;

  return (
    <div className="player-bar">
      <div className="player-clip">
        {clip.thumb_url ? <img src={clip.thumb_url} alt="" /> : <span className="player-thumb-empty" />}
        <div className="player-titles">
          <b>{clipTitle(clip)}</b>
          <span className="mono">{clip.game || ''}</span>
        </div>
      </div>

      <div className="player-controls">
        <button
          type="button"
          className="player-btn"
          onClick={() => onStep(-1)}
          disabled={!canStepBack}
          aria-label={t('viewer.prevClip')}>
          <StepGlyph dir="back" />
        </button>
        <button type="button" className="player-main" onClick={onToggle} aria-label={paused ? t('viewer.play') : t('viewer.pause')}>
          {paused ? <PlayGlyph /> : <PauseGlyph />}
        </button>
        <button
          type="button"
          className="player-btn"
          onClick={() => onStep(1)}
          disabled={!canStepForward}
          aria-label={t('viewer.nextClip')}>
          <StepGlyph dir="forward" />
        </button>
      </div>

      <div className="player-scrub">
        <span className="mono player-tc">{formatDuration(current, true)}</span>
        <div
          className="player-track"
          ref={trackRef}
          onClick={e => {
            const rect = trackRef.current?.getBoundingClientRect();
            if (rect) onSeek(clamp01((e.clientX - rect.left) / rect.width));
          }}>
          <div className="player-fill" style={{width: `${percent}%`}} />
          <div className="player-knob" style={{left: `${percent}%`}} />
        </div>
        <span className="mono player-tc">{formatDuration(duration, true)}</span>
      </div>

      <div className="player-extra">
        <VolumeControl volume={volume} onChange={onVolumeChange} onToggleMute={onToggleMute} />
        <button type="button" className="player-btn" onClick={onShare} aria-label={t('viewer.copyShareLink')}>
          <ShareGlyph />
        </button>
        <button type="button" className="player-btn" onClick={onClose} aria-label={t('viewer.closePlayer')}>
          <IconClose size={15} />
        </button>
      </div>
    </div>
  );
}

function VolumeControl({
  volume,
  onChange,
  onToggleMute,
}: {
  volume: number;
  onChange: (next: number) => void;
  onToggleMute: () => void;
}) {
  return (
    <div className="player-volume">
      <button
        type="button"
        className="player-btn"
        onClick={onToggleMute}
        aria-label={volume === 0 ? t('viewer.unmute') : t('viewer.mute')}>
        <VolumeGlyph level={volume} />
      </button>
      <input
        type="range"
        className="player-volume-range"
        min={0}
        max={1}
        step={0.01}
        value={volume}
        aria-label={t('viewer.volume')}
        style={{['--filled' as string]: `${volume * 100}%`}}
        onChange={e => onChange(Number(e.target.value))}
      />
    </div>
  );
}

function ColorPicker({
  at,
  current,
  onPick,
  onClose,
}: {
  at: {x: number; y: number};
  current: string;
  onPick: (color: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const dismiss = (e: Event) => {
      if (ref.current?.contains(e.target as Node)) return;
      onClose();
    };
    // Deferred a frame: the click that opened this popup is still travelling.
    const id = window.setTimeout(() => document.addEventListener('pointerdown', dismiss, true), 0);
    return () => {
      window.clearTimeout(id);
      document.removeEventListener('pointerdown', dismiss, true);
    };
  }, [onClose]);

  return (
    <div className="hl-picker" ref={ref} style={{left: at.x, top: at.y}} role="menu">
      {HIGHLIGHT_COLORS.map(color => (
        <button
          key={color}
          type="button"
          className="hl-picker-dot"
          data-active={color === current || undefined}
          style={{background: color}}
          title={color}
          aria-label={t('viewer.useColour', {color})}
          onClick={() => onPick(color)}
        />
      ))}
    </div>
  );
}

function HighlightRename({
  initial,
  onSubmit,
  onCancel,
}: {
  initial: string;
  onSubmit: (label: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const done = useRef(false);

  const submit = () => {
    if (done.current) return;
    done.current = true;
    const next = value.trim() || t('viewer.highlight');
    if (next === initial) onCancel();
    else onSubmit(next);
  };

  return (
    <input
      className="hl-rename"
      value={value}
      autoFocus
      aria-label={t('viewer.highlightLabel')}
      onClick={e => e.stopPropagation()}
      onChange={e => setValue(e.target.value)}
      onFocus={e => e.target.select()}
      onBlur={submit}
      onKeyDown={e => {
        e.stopPropagation();
        if (e.key === 'Enter') {
          e.preventDefault();
          submit();
        }
        if (e.key === 'Escape') {
          done.current = true;
          onCancel();
        }
      }}
    />
  );
}

const VOLUME_STORAGE_KEY = 'vice-volume';

/** Last volume the user set, so a fresh clip does not default back to full. */
function readStoredVolume(): number {
  try {
    const raw = localStorage.getItem(VOLUME_STORAGE_KEY);
    if (raw == null) return 1;
    const n = Number(raw);
    return Number.isFinite(n) ? clamp01(n) : 1;
  } catch {
    return 1;
  }
}

const clamp01 = (n: number) => Math.max(0, Math.min(1, n));
const round3 = (n: number) => Number(n.toFixed(3));
const sortHighlights = (list: Highlight[]) => [...list].sort((a, b) => a.time - b.time);
const replaceTime = (list: Highlight[], id: string, time: number) =>
  list.map(h => (h.id === id ? {...h, time} : h));

/** "Highlight", then "Highlight 1", and so on, skipping names already used. */
function nextHighlightLabel(highlights: Highlight[]): string {
  // The label is stored with the clip, so this is a default name the user can
  // overwrite, like "Untitled" in an editor. It is translated for the same
  // reason. Labels already on disk keep whatever they were saved as.
  const base = t('viewer.highlight');
  const used = new Set(highlights.map(h => h.label));
  if (!used.has(base)) return base;
  let n = 1;
  while (used.has(t('viewer.highlightNumbered', {base, n}))) n++;
  return t('viewer.highlightNumbered', {base, n});
}

const stroke = {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2.2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
};

const Chevron = ({dir}: {dir: 'left' | 'right'}) => (
  <svg {...stroke} width={16} height={16}>
    <path d={dir === 'left' ? 'M15 6 9 12l6 6' : 'M9 6l6 6-6 6'} />
  </svg>
);
const PlayGlyph = () => (
  <svg viewBox="0 0 24 24" width={16} height={16} fill="currentColor" aria-hidden="true">
    <path d="M7 4l13 8-13 8z" />
  </svg>
);
const PauseGlyph = () => (
  <svg viewBox="0 0 24 24" width={16} height={16} fill="currentColor" aria-hidden="true">
    <path d="M7 4h3.5v16H7zM13.5 4H17v16h-3.5z" />
  </svg>
);
const StepGlyph = ({dir}: {dir: 'back' | 'forward'}) => (
  <svg viewBox="0 0 24 24" width={13} height={13} fill="currentColor" aria-hidden="true">
    <path d={dir === 'back' ? 'M6 4h2v16H6zM20 4 9.5 12 20 20z' : 'M18 4h-2v16h2zM4 4l10.5 8L4 20z'} />
  </svg>
);
const ShareGlyph = () => (
  <svg {...stroke} width={15} height={15} strokeWidth={2}>
    <circle cx="18" cy="5" r="3" />
    <circle cx="6" cy="12" r="3" />
    <circle cx="18" cy="19" r="3" />
    <path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4" />
  </svg>
);
/** Speaker glyph with zero, one, or two sound waves, by volume level. */
const VolumeGlyph = ({level}: {level: number}) => (
  <svg viewBox="0 0 24 24" width={15} height={15} aria-hidden="true">
    <path d="M4 9v6h4l5 5V4L8 9H4z" fill="currentColor" />
    {level === 0 ? (
      <g {...stroke} strokeWidth={2.2}>
        <path d="m16 9 5 6M21 9l-5 6" />
      </g>
    ) : (
      <g {...stroke} strokeWidth={2.2} fill="none">
        {level > 0.1 ? <path d="M16.3 8.6a5 5 0 0 1 0 6.8" /> : null}
        {level > 0.55 ? <path d="M19 5.8a9 9 0 0 1 0 12.4" /> : null}
      </g>
    )}
  </svg>
);
