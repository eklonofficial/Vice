import {useEffect, useRef, useState} from 'react';

import {endClipDrag, startClipDrag} from '../lib/clipDrag';
import {formatBytes, formatDuration} from '../lib/format';
import {clipTitle, type Clip} from '../lib/types';
import {H264_SUPPORTED} from '../lib/env';
import {playbackUrl} from '../lib/playback';
import {t} from '../lib/i18n';
import {InlineRename} from './InlineRename';

/**
 * A video holds its decoded buffer for as long as a source is attached, so an
 * element that carries src from the moment the card is built gets a media
 * player whether or not it is ever hovered. The URL is parked until hover and
 * dropped again once the pointer has been gone a moment. Waiting rather than
 * releasing on the spot means sweeping across the grid, or coming straight
 * back to the card you just left, does not pay to attach twice.
 */
const PREVIEW_RELEASE_MS = 4000;

export interface ClipActions {
  onOpen?: (clip: Clip) => void;
  onTrim?: (clip: Clip) => void;
  onDelete?: (clip: Clip) => void;
  onReveal?: (clip: Clip) => void;
  onCopyFile?: (clip: Clip) => void;
  onCopyLink?: (clip: Clip) => void;
  onRename?: (clip: Clip, name: string) => Promise<void>;
  onTag?: (clip: Clip) => void;
  onContextMenu?: (clip: Clip, at: {x: number; y: number}) => void;
  /** Set by the context menu to open this card's rename field. */
  renamingSlug?: string | null;
  onRenameDone?: () => void;
}

export function ClipCard({
  clip,
  isNew,
  actions = {},
  draggable,
}: {
  clip: Clip;
  isNew?: boolean;
  actions?: ClipActions;
  draggable?: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const releaseTimer = useRef<number | undefined>(undefined);
  const [previewFailed, setPreviewFailed] = useState(false);
  const [renamingHere, setRenamingHere] = useState(false);
  const renaming = renamingHere || actions.renamingSlug === clip.slug;

  const stopRenaming = () => {
    setRenamingHere(false);
    actions.onRenameDone?.();
  };

  const broken = clip.unreadable;
  const canPreview = H264_SUPPORTED && !broken && !previewFailed && Boolean(clip.thumb_url);

  useEffect(() => () => window.clearTimeout(releaseTimer.current), []);

  const attachPreview = () => {
    const video = videoRef.current;
    if (!video || !canPreview) return;
    window.clearTimeout(releaseTimer.current);
    // Same source the viewer uses, so an H.265 library previews through the
    // proxy instead of showing a black card.
    if (!video.getAttribute('src')) video.src = playbackUrl(clip);
    void video.play().catch(() => setPreviewFailed(true));
  };

  const releasePreview = () => {
    const video = videoRef.current;
    if (!video) return;
    video.pause();
    window.clearTimeout(releaseTimer.current);
    releaseTimer.current = window.setTimeout(() => {
      if (!video.getAttribute('src')) return;
      video.removeAttribute('src');
      // Dropping the attribute alone leaves the buffer in place; load() is
      // what tears the player down.
      try {
        video.load();
      } catch (err) {
        console.debug('Releasing the preview buffer failed', err);
      }
    }, PREVIEW_RELEASE_MS);
  };

  const meta = [
    clip.created_at
      ? new Date(clip.created_at).toLocaleDateString(undefined, {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        })
      : '',
    !broken && clip.width ? `${clip.width}x${clip.height}` : '',
    clip.size ? formatBytes(clip.size) : '',
    clip.views ? t('card.views', {count: clip.views}) : '',
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <article
      className="clip-card"
      data-broken={broken || undefined}
      draggable={draggable && !renaming}
      onDragStart={e => startClipDrag(e, clip)}
      onDragEnd={endClipDrag}
      onContextMenu={e => {
        if (!actions.onContextMenu) return;
        e.preventDefault();
        actions.onContextMenu(clip, {x: e.clientX, y: e.clientY});
      }}
      onClick={e => {
        // The whole card opens the clip, not just the thumbnail. Anything that
        // already does something of its own is left alone: the action row, the
        // rename field, and the title, which takes a double-click to rename and
        // would never get its second click if the first one opened the viewer.
        if (renaming) return;
        const target = e.target as HTMLElement;
        if (target.closest('button, a, input, textarea, .clip-name')) return;
        actions.onOpen?.(clip);
      }}>
      <button
        type="button"
        className="clip-thumb"
        onClick={() => actions.onOpen?.(clip)}
        onPointerEnter={attachPreview}
        onPointerLeave={releasePreview}
        aria-label={t('card.openTitle', {name: clipTitle(clip)})}>
        {clip.thumb_url ? (
          <img src={clip.thumb_url} loading="lazy" alt="" draggable={false} />
        ) : (
          <span className="clip-thumb-empty" aria-hidden="true" />
        )}
        {canPreview ? (
          <video
            ref={videoRef}
            className="clip-preview"
            muted
            loop
            playsInline
            preload="none"
            onError={() => setPreviewFailed(true)}
          />
        ) : null}

        <span className="clip-badges">
          {broken ? (
            <span className="clip-badge clip-badge-broken" title={clip.unreadable_reason}>
              {t('card.unreadable')}
            </span>
          ) : null}
          {isNew ? <span className="clip-badge clip-badge-new">{t('common.new')}</span> : null}
        </span>

        {clip.duration && !broken ? (
          <span className="clip-duration">{formatDuration(Math.round(clip.duration), true)}</span>
        ) : null}
      </button>

      <div className="clip-body">
        {renaming ? (
          <InlineRename
            className="clip-rename"
            label={t('card.nameLabel')}
            initial={clipTitle(clip)}
            onCancel={stopRenaming}
            onSubmit={async name => {
              stopRenaming();
              await actions.onRename?.(clip, name);
            }}
          />
        ) : (
          <h3
            className="clip-name"
            title={t('card.renameHint', {name: clipTitle(clip)})}
            onDoubleClick={() => actions.onRename && setRenamingHere(true)}>
            {clipTitle(clip)}
          </h3>
        )}

        <p className="clip-meta">{meta}</p>
        {broken ? (
          <p className="clip-broken-note">
            {t('card.unreadableNote', {
              reason: clip.unreadable_reason || t('card.unreadableFallback'),
            })}
          </p>
        ) : null}
        {actions.onTag ? (
          <button
            type="button"
            className="clip-game clip-game-btn"
            data-untagged={clip.game ? undefined : true}
            title={t('card.tagTitle')}
            onClick={() => actions.onTag?.(clip)}>
            {clip.game || t('common.untagged')}
          </button>
        ) : (
          <span className="clip-game" data-untagged={clip.game ? undefined : true}>
            {clip.game || t('common.untagged')}
          </span>
        )}

        {hasActions(actions) ? (
          <div className="clip-actions">
            {actions.onTrim ? (
              <IconButton label={t('card.trim')} onClick={() => actions.onTrim?.(clip)}>
                <ScissorsGlyph />
              </IconButton>
            ) : null}
            {actions.onCopyFile ? (
              <IconButton label={t('card.copyVideo')} onClick={() => actions.onCopyFile?.(clip)}>
                <ClipboardGlyph />
              </IconButton>
            ) : null}
            {actions.onCopyLink ? (
              <IconButton
                label={clip.share_url ? t('card.copyShareLink') : t('card.noShareLink')}
                disabled={!clip.share_url}
                onClick={() => actions.onCopyLink?.(clip)}>
                <LinkGlyph />
              </IconButton>
            ) : null}
            {actions.onReveal ? (
              <IconButton label={t('card.reveal')} onClick={() => actions.onReveal?.(clip)}>
                <FolderGlyph />
              </IconButton>
            ) : null}
            {actions.onDelete ? (
              <IconButton label={t('card.delete')} danger onClick={() => actions.onDelete?.(clip)}>
                <TrashGlyph />
              </IconButton>
            ) : null}
          </div>
        ) : null}
      </div>
    </article>
  );
}

const hasActions = (a: ClipActions) =>
  Boolean(a.onTrim || a.onCopyFile || a.onCopyLink || a.onReveal || a.onDelete);

function IconButton({
  label,
  onClick,
  danger,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className="clip-icon-btn"
      data-danger={danger || undefined}
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={e => {
        e.stopPropagation();
        onClick();
      }}>
      {children}
    </button>
  );
}

const g = {
  width: 13,
  height: 13,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
};

const ScissorsGlyph = () => (
  <svg {...g}>
    <circle cx="6" cy="7" r="3" />
    <circle cx="6" cy="17" r="3" />
    <path d="M20 5 9 15M20 19 9 9" />
  </svg>
);
const ClipboardGlyph = () => (
  <svg {...g}>
    <rect x="8" y="8" width="13" height="13" rx="2" />
    <path d="M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2" />
  </svg>
);
const LinkGlyph = () => (
  <svg {...g}>
    <path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1" />
    <path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1" />
  </svg>
);
const FolderGlyph = () => (
  <svg {...g}>
    <path d="M3 7h6l2 2h10v10H3z" />
  </svg>
);
const TrashGlyph = () => (
  <svg {...g}>
    <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13" />
  </svg>
);
