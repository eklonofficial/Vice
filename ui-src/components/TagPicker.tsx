import {useEffect, useState} from 'react';

import {api} from '../lib/api';
import {type Clip} from '../lib/types';
import {t} from '../lib/i18n';
import {Modal} from './Modal';
import {Select, TextField} from './settings/Fields';

const NONE = '__untagged__';
const CUSTOM = '__custom__';

/**
 * Tag editor for a single clip: the bundled/custom games list, same shape as
 * the resolution and accent pickers elsewhere in the app, a `Select` with a
 * trailing "Custom" entry that reveals a free-text field.
 *
 * Owns its own list fetch and local draft state; the caller only supplies
 * which clip is being tagged (or null for closed) and what to do with the
 * result.
 */
export function TagPicker({
  clip,
  onClose,
  onSave,
}: {
  clip: Clip | null;
  onClose: () => void;
  /** Resolves to whether the save landed; the picker closes itself on success. */
  onSave: (clip: Clip, game: string | null) => Promise<boolean>;
}) {
  const [games, setGames] = useState<string[]>([]);
  const [choice, setChoice] = useState<string>(NONE);
  const [custom, setCustom] = useState('');
  const [saving, setSaving] = useState(false);

  // Fetched once per open rather than once per app, since the custom games
  // list in Settings can change between visits.
  useEffect(() => {
    if (!clip) return;
    let cancelled = false;
    void api
      .games()
      .then(list => !cancelled && setGames(list))
      .catch(err => console.debug('Loading the games list failed', err));
    return () => {
      cancelled = true;
    };
  }, [clip]);

  // Seed the draft from the clip's current tag once the list is in, so an
  // already-custom tag lands on "Custom" pre-filled instead of vanishing off
  // the list.
  useEffect(() => {
    if (!clip) {
      setChoice(NONE);
      setCustom('');
      return;
    }
    const current = clip.game || '';
    if (!current) {
      setChoice(NONE);
      setCustom('');
    } else if (games.includes(current)) {
      setChoice(current);
      setCustom('');
    } else {
      setChoice(CUSTOM);
      setCustom(current);
    }
  }, [clip, games]);

  if (!clip) return null;

  const options: Array<[string, string]> = [
    [NONE, t('common.untagged')],
    ...games.map(g => [g, g] as [string, string]),
    [CUSTOM, t('card.tagCustom')],
  ];

  const customEmpty = choice === CUSTOM && !custom.trim();

  const submit = async () => {
    if (customEmpty) return;
    const game = choice === NONE ? null : choice === CUSTOM ? custom.trim() : choice;
    setSaving(true);
    const ok = await onSave(clip, game);
    setSaving(false);
    if (ok) onClose();
  };

  return (
    <Modal
      open
      title={t('card.tagTitle')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn-quiet" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn" disabled={saving || customEmpty} onClick={() => void submit()}>
            {t('common.save')}
          </button>
        </>
      }>
      <div className="tag-picker">
        <Select label={t('card.tagTitle')} value={choice} onChange={setChoice} options={options} />
        {choice === CUSTOM ? (
          <TextField
            label={t('card.tagCustom')}
            value={custom}
            placeholder={t('card.tagCustomPlaceholder')}
            onChange={setCustom}
          />
        ) : null}
      </div>
    </Modal>
  );
}
