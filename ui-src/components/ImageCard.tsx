import {useState} from 'react';

import {endClipDrag, startImageDrag} from '../lib/clipDrag';
import {formatBytes} from '../lib/format';
import {imageTitle, type Image} from '../lib/types';
import {t} from '../lib/i18n';
import {InlineRename} from './InlineRename';
import {IconImages} from './Icons';

export interface ImageActionSet {
  onOpen?: (image: Image) => void;
  onCopy?: (image: Image) => void;
  onReveal?: (image: Image) => void;
  onDelete?: (image: Image) => void;
  onRename?: (image: Image, name: string) => Promise<void>;
  onContextMenu?: (image: Image, at: {x: number; y: number}) => void;
  /** Set by the context menu to open this card's rename field. */
  renamingSlug?: string | null;
  onRenameDone?: () => void;
}

/**
 * The picture is the card: details and actions sit over it on hover or focus,
 * the same on Home, in the library and inside a playlist.
 */
export function ImageCard({
  image,
  isNew,
  actions = {},
  draggable,
}: {
  image: Image;
  isNew?: boolean;
  actions?: ImageActionSet;
  draggable?: boolean;
}) {
  const [renamingHere, setRenamingHere] = useState(false);
  const [failedThumb, setFailedThumb] = useState<string | null>(null);
  const renaming = renamingHere || actions.renamingSlug === image.slug;

  const stopRenaming = () => {
    setRenamingHere(false);
    actions.onRenameDone?.();
  };

  const meta = [
    image.created_at
      ? new Date(image.created_at).toLocaleDateString(undefined, {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        })
      : '',
    image.width ? `${image.width}x${image.height}` : '',
    image.size ? formatBytes(image.size) : '',
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <article
      className="clip-card image-card"
      data-renaming={renaming || undefined}
      draggable={draggable && !renaming}
      onDragStart={e => startImageDrag(e, image)}
      onDragEnd={endClipDrag}
      onContextMenu={e => {
        if (!actions.onContextMenu) return;
        e.preventDefault();
        actions.onContextMenu(image, {x: e.clientX, y: e.clientY});
      }}
      onClick={e => {
        if (renaming) return;
        const target = e.target as HTMLElement;
        if (target.closest('button, a, input, textarea, .clip-name')) return;
        actions.onOpen?.(image);
      }}>
      <button
        type="button"
        className="clip-thumb"
        onClick={() => actions.onOpen?.(image)}
        aria-label={t('images.openTitle', {name: imageTitle(image)})}>
        {image.thumb_url && failedThumb !== image.thumb_url ? (
          <img src={image.thumb_url} loading="lazy" alt="" draggable={false}
            onError={() => setFailedThumb(image.thumb_url ?? null)} />
        ) : (
          <span className="clip-thumb-empty" aria-hidden="true"><IconImages size={32} /></span>
        )}
        <span className="clip-badges">
          {isNew ? <span className="clip-badge clip-badge-new">{t('common.new')}</span> : null}
        </span>
      </button>

      <div className="clip-body">
        {renaming ? (
          <InlineRename
            className="clip-rename"
            label={t('card.nameLabel')}
            initial={imageTitle(image)}
            onCancel={stopRenaming}
            onSubmit={async name => {
              stopRenaming();
              await actions.onRename?.(image, name);
            }}
          />
        ) : (
          <h3
            className="clip-name"
            title={t('card.renameHint', {name: imageTitle(image)})}
            onDoubleClick={() => actions.onRename && setRenamingHere(true)}>
            {imageTitle(image)}
          </h3>
        )}

        <p className="clip-meta">{meta}</p>
        <span className="clip-game" data-untagged={image.game ? undefined : true}>
          {image.game || t('common.untagged')}
        </span>

        {hasActions(actions) ? (
          <div className="clip-actions">
            {actions.onCopy ? (
              <IconButton label={t('images.copy')} onClick={() => actions.onCopy?.(image)}>
                <ClipboardGlyph />
              </IconButton>
            ) : null}
            {actions.onReveal ? (
              <IconButton label={t('card.reveal')} onClick={() => actions.onReveal?.(image)}>
                <FolderGlyph />
              </IconButton>
            ) : null}
            {actions.onDelete ? (
              <IconButton label={t('card.delete')} danger onClick={() => actions.onDelete?.(image)}>
                <TrashGlyph />
              </IconButton>
            ) : null}
          </div>
        ) : null}
      </div>
    </article>
  );
}

const hasActions = (a: ImageActionSet) => Boolean(a.onCopy || a.onReveal || a.onDelete);

function IconButton({
  label,
  onClick,
  danger,
  children,
}: {
  label: string;
  onClick: () => void;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className="clip-icon-btn"
      data-danger={danger || undefined}
      title={label}
      aria-label={label}
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

const ClipboardGlyph = () => (
  <svg {...g}>
    <rect x="8" y="8" width="13" height="13" rx="2" />
    <path d="M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2" />
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
