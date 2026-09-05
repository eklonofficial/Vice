import {useCallback, useMemo, useState, type ReactNode} from 'react';

import {api} from '../lib/api';
import {copyShareLink} from '../lib/share';
import {clipTitle, type Clip} from '../lib/types';
import type {ClipActions} from '../components/ClipCard';
import {ContextMenu} from '../components/ContextMenu';
import {Modal} from '../components/Modal';
import {TagPicker} from '../components/TagPicker';
import {useStore} from './store';
import {usePlayback} from './playback';
import {t} from '../lib/i18n';

/**
 * Everything a clip card can do, in one place.
 *
 * Home and All Clips render the same card and are expected to behave the
 * same. Building the handler set on each screen is what let them drift the
 * first time, with Home ending up as a card that could only be opened.
 */
/**
 * Rename a clip, wherever the gesture came from.
 *
 * Shared because renaming is reachable from the card, the viewer and the trim
 * window, and the "saved as" notice below is the part that would have been
 * dropped from the copies.
 */
export function useRenameClip(): (clip: Clip, name: string) => Promise<Clip | null> {
  const {notify, refreshClips} = useStore();
  return useCallback(
    async (clip: Clip, name: string) => {
      try {
        const updated = await api.renameClip(clip.slug, name);
        await refreshClips();
        if (updated?.name && clipTitle(updated) !== name) {
          // Punctuation is normalised server side, so say what landed on disk.
          notify({
            kind: 'info',
            title: t('card.savedAs', {name: clipTitle(updated)}),
            tone: 'neutral',
            holdMs: 4000,
          });
        }
        return updated ?? null;
      } catch (err) {
        notify({
          kind: 'error',
          title: t('card.renameFailed'),
          detail: (err as Error).message,
          tone: 'error',
          holdMs: 7000,
        });
        return null;
      }
    },
    [notify, refreshClips],
  );
}

/**
 * Set or clear a clip's tag, wherever the gesture came from (card, viewer
 * header, or the card's context menu all share this).
 */
export function useTagClip(): (clip: Clip, game: string | null) => Promise<boolean> {
  const {notify, refreshClips, refreshPlaylists} = useStore();
  return useCallback(
    async (clip: Clip, game: string | null) => {
      try {
        await api.tagClip(clip.slug, game);
        await Promise.all([refreshClips(), refreshPlaylists()]);
        notify({
          kind: 'info',
          title: game ? t('card.tagged', {game}) : t('card.untaggedNotice'),
          tone: 'accent',
          holdMs: 3000,
        });
        return true;
      } catch (err) {
        notify({
          kind: 'error',
          title: t('card.tagFailed'),
          detail: (err as Error).message,
          tone: 'error',
          holdMs: 7000,
        });
        return false;
      }
    },
    [notify, refreshClips, refreshPlaylists],
  );
}

export function useClipActions(): {actions: ClipActions; overlays: ReactNode} {
  const {state, notify, refreshPlaylists} = useStore();
  const {openViewer, openTrim} = usePlayback();
  const {playlists} = state;

  const [menu, setMenu] = useState<{clip: Clip; at: {x: number; y: number}} | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Clip | null>(null);
  const [manualCopy, setManualCopy] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [tagging, setTagging] = useState<Clip | null>(null);

  const fail = useCallback(
    (title: string) => (err: Error) =>
      notify({kind: 'error', title, detail: err.message, tone: 'error', holdMs: 7000}),
    [notify],
  );

  const say = useCallback(
    (title: string, detail?: string) =>
      notify({kind: 'info', title, detail, tone: 'accent', holdMs: 3500}),
    [notify],
  );

  const copyLink = useCallback(
    (clip: Clip) => void copyShareLink(clip, notify, setManualCopy),
    [notify],
  );

  const reveal = useCallback(
    (clip: Clip) => void api.revealClip(clip.slug).catch(fail(t('viewer.errReveal'))),
    [fail],
  );

  const copyFile = useCallback(
    (clip: Clip) =>
      void api
        .copyClipFile(clip.slug)
        .then(() => say(t('card.videoCopied')))
        .catch(fail(t('card.errCopyVideo'))),
    [fail, say],
  );

  const renameClip = useRenameClip();
  const rename = useCallback(
    async (clip: Clip, name: string) => {
      await renameClip(clip, name);
    },
    [renameClip],
  );

  const tagClip = useTagClip();

  const actions = useMemo<ClipActions>(
    () => ({
      onOpen: clip => openViewer(clip.slug),
      onTrim: clip => openTrim(clip.slug),
      onCopyLink: copyLink,
      onCopyFile: copyFile,
      onReveal: reveal,
      onDelete: setConfirmDelete,
      onRename: rename,
      onTag: setTagging,
      onContextMenu: (clip, at) => setMenu({clip, at}),
      renamingSlug: renaming,
      onRenameDone: () => setRenaming(null),
    }),
    [openViewer, openTrim, copyLink, copyFile, reveal, rename, renaming],
  );

  const menuClip = menu?.clip;
  const overlays = (
    <>
      {menu && menuClip ? (
        <ContextMenu
          at={menu.at}
          heading={clipTitle(menuClip)}
          emptyLabel={t('common.noActions')}
          onClose={() => setMenu(null)}
          items={[
            {id: 'open', label: t('card.open'), onSelect: () => openViewer(menuClip.slug)},
            {id: 'trim', label: t('card.trim'), onSelect: () => openTrim(menuClip.slug)},
            {id: 'rename', label: t('card.rename'), onSelect: () => setRenaming(menuClip.slug)},
            {id: 'tag', label: t('card.tag'), onSelect: () => setTagging(menuClip)},
            {
              id: 'copy-link',
              label: menuClip.share_url ? t('card.copyShareLink') : t('card.noShareLink'),
              disabled: !menuClip.share_url,
              onSelect: () => copyLink(menuClip),
            },
            {id: 'copy-file', label: t('card.copyVideoShort'), onSelect: () => copyFile(menuClip)},
            {id: 'reveal', label: t('card.reveal'), onSelect: () => reveal(menuClip)},
            ...(playlists.length ? [{id: 'sep-playlists', separator: true} as const] : []),
            // One row per playlist that toggles, so adding and removing are
            // the same gesture in the same place.
            ...playlists.map(playlist => {
              const inIt = playlist.clip_slugs?.includes(menuClip.slug) ?? false;
              return {
                id: playlist.id,
                label: inIt
                  ? t('card.removeFrom', {playlist: playlist.name})
                  : t('card.addTo', {playlist: playlist.name}),
                mark: inIt ? '✓' : (playlist.emoji ?? undefined),
                onSelect: () => {
                  const call = inIt
                    ? api.removeClipFromPlaylist(playlist.id, menuClip.slug)
                    : api.addClipToPlaylist(playlist.id, menuClip.slug);
                  void call
                    .then(async result => {
                      if (result?.ok === false) {
                        throw new Error(result.error || t('card.playlistUnchanged'));
                      }
                      await refreshPlaylists();
                      say(
                        inIt
                          ? t('card.removedFrom', {playlist: playlist.name})
                          : t('card.addedTo', {playlist: playlist.name}),
                      );
                    })
                    .catch(fail(t('clips.errUpdatePlaylist')));
                },
              };
            }),
            {id: 'sep-delete', separator: true},
            {
              id: 'delete',
              label: t('card.deleteClip'),
              danger: true,
              onSelect: () => setConfirmDelete(menuClip),
            },
          ]}
        />
      ) : null}

      <Modal
        open={confirmDelete !== null}
        title={t('viewer.confirmDeleteTitle')}
        onClose={() => setConfirmDelete(null)}
        footer={
          <>
            <button type="button" className="btn btn-quiet" onClick={() => setConfirmDelete(null)}>
              {t('common.keepIt')}
            </button>
            <button
              type="button"
              className="btn btn-danger-solid"
              onClick={() => {
                const clip = confirmDelete;
                setConfirmDelete(null);
                if (!clip) return;
                void api
                  .deleteClip(clip.slug)
                  .then(() => say(t('viewer.clipDeleted')))
                  .catch(fail(t('viewer.errDelete')));
              }}>
              {t('common.delete')}
            </button>
          </>
        }>
        <p>
          {t('viewer.confirmDeleteBody', {
            name: confirmDelete ? clipTitle(confirmDelete) : '',
          })}
        </p>
      </Modal>

      <Modal open={manualCopy !== null} title={t('viewer.copyLinkTitle')} onClose={() => setManualCopy(null)}>
        <p>{t('viewer.copyLinkBody')}</p>
        <textarea className="manual-copy" readOnly value={manualCopy ?? ''} rows={3} />
      </Modal>

      <TagPicker clip={tagging} onClose={() => setTagging(null)} onSave={tagClip} />
    </>
  );

  return {actions, overlays};
}
