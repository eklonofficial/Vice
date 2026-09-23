import {useCallback, useMemo, useState, type ReactNode} from 'react';

import {api} from '../lib/api';
import {imageSlug, imageTitle, type Image} from '../lib/types';
import type {ImageActionSet} from '../components/ImageCard';
import {ContextMenu} from '../components/ContextMenu';
import {ImageViewer} from '../components/ImageViewer';
import {Modal} from '../components/Modal';
import {useStore} from './store';
import {t} from '../lib/i18n';

/**
 * Everything a screenshot card can do, in one place, for the same reason the
 * clip version exists: two screens show the same card and are expected to
 * behave the same.
 *
 * The viewer is one of the overlays rather than a provider of its own. A
 * picture has nothing to keep alive across a change of view, which is the only
 * thing that put the clip viewer up at the app root.
 */
export function useImageActions(collection?: Image[]): {actions: ImageActionSet; overlays: ReactNode} {
  const {state, visibleImages, notify, refreshImages, refreshPlaylists} = useStore();
  const {playlists} = state;

  const [viewing, setViewing] = useState<string | null>(null);
  const [menu, setMenu] = useState<{image: Image; at: {x: number; y: number}} | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Image | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [sequence, setSequence] = useState<string[] | null>(null);
  const [pendingRename, setPendingRename] = useState<Image | null>(null);

  const navigation = useMemo(() => sequence === null ? visibleImages : sequence
    .map(slug => state.images.find(image => image.slug === slug))
    .filter((image): image is Image => Boolean(image)), [sequence, visibleImages, state.images]);
  // Rename broadcasts deletion of the old slug before its replacement arrives.
  const viewingImage = navigation.find(i => i.slug === viewing)
    ?? (pendingRename?.slug === viewing ? pendingRename : null);
  const openImage = useCallback((image: Image) => {
    setSequence(collection ? collection.map(item => item.slug) : null);
    setViewing(image.slug);
  }, [collection]);
  const closeImage = useCallback(() => setViewing(null), []);

  const fail = useCallback(
    (title: string) => (err: Error) =>
      notify({kind: 'error', title, detail: err.message, tone: 'error', holdMs: 7000}),
    [notify],
  );

  const say = useCallback(
    (title: string, detail?: string, tone: 'accent' | 'error' = 'accent') =>
      notify({
        kind: tone === 'error' ? 'error' : 'info',
        title,
        detail,
        tone,
        holdMs: tone === 'error' ? 7000 : 3500,
      }),
    [notify],
  );

  const copy = useCallback(
    (image: Image) =>
      void api
        .copyImage(image.slug)
        .then(result => {
          if (result?.ok === false) throw new Error(result.error || t('images.errCopy'));
          say(t('images.copied'));
        })
        .catch(fail(t('images.errCopy'))),
    [fail, say],
  );

  const reveal = useCallback(
    (image: Image) => void api.revealImage(image.slug).catch(fail(t('viewer.errReveal'))),
    [fail],
  );

  const openExternally = useCallback(
    (image: Image) => void api.openImage(image.slug).catch(fail(t('images.errOpen'))),
    [fail],
  );

  const rename = useCallback(
    async (image: Image, name: string) => {
      setPendingRename(image);
      try {
        const updated = await api.renameImage(image.slug, name);
        // Renaming changes the slug, so the viewer has to follow it or the
        // picture the user just named disappears from under them.
        if (updated?.slug) {
          setPendingRename(updated);
          setViewing(current => (current === image.slug ? updated.slug : current));
          setSequence(current => current?.map(slug => slug === image.slug ? updated.slug : slug) ?? null);
          if (imageTitle(updated) !== name) {
            notify({
              kind: 'info',
              title: t('card.savedAs', {name: imageTitle(updated)}),
              tone: 'neutral',
              holdMs: 4000,
            });
          }
        }
        await refreshImages();
      } catch (err) {
        notify({
          kind: 'error',
          title: t('card.renameFailed'),
          detail: (err as Error).message,
          tone: 'error',
          holdMs: 7000,
        });
      } finally {
        setPendingRename(null);
      }
    },
    [notify, refreshImages],
  );

  const actions = useMemo<ImageActionSet>(
    () => ({
      onOpen: openImage,
      onCopy: copy,
      onReveal: reveal,
      onDelete: setConfirmDelete,
      onRename: rename,
      onContextMenu: (image, at) => setMenu({image, at}),
      renamingSlug: renaming,
      onRenameDone: () => setRenaming(null),
    }),
    [openImage, copy, reveal, rename, renaming],
  );

  const menuImage = menu?.image;
  const overlays = (
    <>
      <ImageViewer
        image={viewingImage}
        images={navigation}
        onSelect={setViewing}
        onClose={closeImage}
        shortcutsBlocked={confirmDelete !== null || menu !== null}
        onRename={rename}
        onCopy={copy}
        onReveal={reveal}
        onDelete={setConfirmDelete}
        notify={say}
      />

      {menu && menuImage ? (
        <ContextMenu
          at={menu.at}
          heading={imageTitle(menuImage)}
          emptyLabel={t('common.noActions')}
          onClose={() => setMenu(null)}
          items={[
            {id: 'open', label: t('images.open'), onSelect: () => openImage(menuImage)},
            {id: 'rename', label: t('card.rename'), onSelect: () => setRenaming(menuImage.slug)},
            {id: 'copy', label: t('images.copy'), onSelect: () => copy(menuImage)},
            {id: 'reveal', label: t('card.reveal'), onSelect: () => reveal(menuImage)},
            {
              id: 'open-external',
              label: t('images.openExternally'),
              onSelect: () => openExternally(menuImage),
            },
            ...(playlists.length ? [{id: 'sep-playlists', separator: true} as const] : []),
            ...playlists.map(playlist => {
              const key = imageSlug(menuImage.slug);
              const inIt = playlist.clip_slugs?.includes(key) ?? false;
              return {
                id: playlist.id,
                label: inIt
                  ? t('card.removeFrom', {playlist: playlist.name})
                  : t('card.addTo', {playlist: playlist.name}),
                mark: inIt ? '✓' : (playlist.emoji ?? undefined),
                onSelect: () => {
                  const call = inIt
                    ? api.removeClipFromPlaylist(playlist.id, key)
                    : api.addClipToPlaylist(playlist.id, key);
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
              label: t('images.deleteImage'),
              danger: true,
              onSelect: () => setConfirmDelete(menuImage),
            },
          ]}
        />
      ) : null}

      <Modal
        open={confirmDelete !== null}
        title={t('images.confirmDeleteTitle')}
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
                const image = confirmDelete;
                setConfirmDelete(null);
                if (!image) return;
                setViewing(current => (current === image.slug ? null : current));
                void api
                  .deleteImage(image.slug)
                  .then(() => say(t('images.deleted')))
                  .catch(fail(t('images.errDelete')));
              }}>
              {t('common.delete')}
            </button>
          </>
        }>
        <p>
          {t('images.confirmDeleteBody', {name: confirmDelete ? imageTitle(confirmDelete) : ''})}
        </p>
      </Modal>
    </>
  );

  return {actions, overlays};
}
