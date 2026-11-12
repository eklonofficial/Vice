import {useCallback} from 'react';

import {api} from '../lib/api';
import {t} from '../lib/i18n';
import type {Clip} from '../lib/types';
import type {MenuItem} from '../components/ContextMenu';
import {useStore} from './store';

/**
 * One menu row per playlist that adds the clip to it or takes it out again.
 *
 * Shared by the clip card's menu and the viewer's Playlist button (#265), so
 * adding and removing are the same gesture with the same notice wherever the
 * clip is.
 */
export function usePlaylistToggleItems(): (clip: Clip) => MenuItem[] {
  const {state, notify, refreshPlaylists} = useStore();
  const {playlists} = state;
  return useCallback(
    (clip: Clip) =>
      playlists.map(playlist => {
        const inIt = playlist.clip_slugs?.includes(clip.slug) ?? false;
        return {
          id: playlist.id,
          label: inIt
            ? t('card.removeFrom', {playlist: playlist.name})
            : t('card.addTo', {playlist: playlist.name}),
          mark: inIt ? '✓' : (playlist.emoji ?? undefined),
          onSelect: () => {
            const call = inIt
              ? api.removeClipFromPlaylist(playlist.id, clip.slug)
              : api.addClipToPlaylist(playlist.id, clip.slug);
            void call
              .then(async result => {
                if (result?.ok === false) {
                  throw new Error(result.error || t('card.playlistUnchanged'));
                }
                await refreshPlaylists();
                notify({
                  kind: 'info',
                  title: inIt
                    ? t('card.removedFrom', {playlist: playlist.name})
                    : t('card.addedTo', {playlist: playlist.name}),
                  tone: 'accent',
                  holdMs: 3500,
                });
              })
              .catch((err: Error) =>
                notify({
                  kind: 'error',
                  title: t('clips.errUpdatePlaylist'),
                  detail: err.message,
                  tone: 'error',
                  holdMs: 7000,
                }),
              );
          },
        };
      }),
    [playlists, notify, refreshPlaylists],
  );
}
