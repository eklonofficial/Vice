import {useEffect, useMemo, useState} from 'react';

import {api} from '../lib/api';
import {t} from '../lib/i18n';
import {clipTitle, type Clip} from '../lib/types';
import {useStore} from '../state/store';
import {Modal} from './Modal';

/**
 * Set a clip's game by hand, for the ones detection missed (#187). Any name
 * can be typed; the games already in the library are offered so a tag does
 * not end up spelled three ways.
 */
export function GameTagModal({clip, onClose}: {clip: Clip | null; onClose: () => void}) {
  const {state, notify} = useStore();
  const [game, setGame] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!clip) return;
    setGame(clip.game ?? '');
    setSaving(false);
  }, [clip]);

  const known = useMemo(
    () =>
      [...new Set(state.clips.map(c => c.game).filter((g): g is string => Boolean(g)))].sort((a, b) =>
        a.localeCompare(b),
      ),
    [state.clips],
  );

  const submit = async () => {
    if (!clip || saving) return;
    setSaving(true);
    try {
      await api.setClipGame(clip.slug, game.trim());
      onClose();
    } catch (err) {
      setSaving(false);
      notify({
        kind: 'error',
        title: t('gameTag.errSave'),
        detail: (err as Error).message,
        tone: 'error',
        holdMs: 7000,
      });
    }
  };

  return (
    <Modal
      open={clip !== null}
      title={clip ? t('gameTag.title', {name: clipTitle(clip)}) : ''}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn-quiet" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn" onClick={() => void submit()} disabled={saving}>
            {t('common.save')}
          </button>
        </>
      }>
      <label className="field">
        <span>{t('gameTag.label')}</span>
        <input
          value={game}
          list="game-tag-known"
          maxLength={80}
          autoFocus
          onChange={e => setGame(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && void submit()}
        />
        <datalist id="game-tag-known">
          {known.map(name => (
            <option key={name} value={name} />
          ))}
        </datalist>
      </label>
      <p className="game-tag-help">{t('gameTag.help')}</p>
    </Modal>
  );
}
