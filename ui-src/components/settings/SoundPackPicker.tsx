import {api} from '../../lib/api';
import {t} from '../../lib/i18n';

const PACKS = [
  ['classic', 'sounds.classic'],
  ['futuristic', 'sounds.futuristic'],
] as const;

/** The built-in sound packs (#300). Picking one plays it, so it is chosen by ear. */
export function SoundPackPicker({value, onChange}: {value: string; onChange: (pack: string) => void}) {
  return (
    <div className="sound-packs" role="radiogroup" aria-label={t('sounds.label')}>
      {PACKS.map(([id, label]) => (
        <button
          key={id}
          type="button"
          role="radio"
          aria-checked={value === id}
          className="sound-pack"
          onClick={() => {
            onChange(id);
            void api.previewSound(id).catch(err => console.debug('Sound preview failed', err));
          }}>
          <span className="sound-pack-play" aria-hidden="true" />
          {t(label)}
        </button>
      ))}
    </div>
  );
}
