import {t} from '../../lib/i18n';
import {KEY_LAYOUT, keyLayoutRows} from '../../lib/keyLayout';

const ALL_KEYS = KEY_LAYOUT.flat().flatMap(([id]) => (id ? [id] : []));

/**
 * The overlay's panel, where a click takes a key off it or puts it back
 * (#258). The last key cannot go, since an empty panel is never what was meant.
 */
export function KeyPicker({
  hidden,
  layout,
  onChange,
}: {
  hidden: string[];
  /** Which keyboard the labels are for. QWERTY when unset. */
  layout?: string;
  onChange: (next: string[]) => void;
}) {
  const lastOne = hidden.length >= ALL_KEYS.length - 1;
  return (
    <div className="key-picker" role="group" aria-label={t('settings.keyOverlayKeys')}>
      {keyLayoutRows(layout).map((row, r) => (
        <div className="key-picker-row" key={r}>
          {row.map(([id, label, span], k) => {
            if (id === null) return <span key={k} style={{gridColumn: `span ${span * 2}`}} />;
            const on = !hidden.includes(id);
            return (
              <button
                key={k}
                type="button"
                className="key-picker-key"
                style={{gridColumn: `span ${span * 2}`}}
                aria-pressed={on}
                aria-label={label || t('settings.keySpace')}
                disabled={on && lastOne}
                onClick={() => onChange(on ? [...hidden, id] : hidden.filter(key => key !== id))}>
                {label}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}
