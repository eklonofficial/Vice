import {IconClose} from '../Icons';
import {Slider} from './Fields';
import {
  appsAlsoOnDesktop,
  recordEverythingElse,
  tracksLostWithoutDesktopAudio,
} from '../../lib/settingsDraft';
import {t} from '../../lib/i18n';

export interface AudioSource {
  id: string;
  label?: string;
  kind?: string;
}

/**
 * Separate audio tracks, in the order the recorder will write them.
 *
 * Order is the whole point of the control: track 1 is what players, Discord
 * and share links use, so which source sits there decides what most people
 * actually hear.
 */
export function AudioTracks({
  tracks,
  sources,
  mixFirst,
  desktopAudioOn,
  pick,
  onPickChange,
  onChange,
  onRefresh,
  refreshing,
  onDuplicate,
  volumes,
  onVolumeChange,
  micTrack,
}: {
  tracks: string[];
  sources: AudioSource[];
  mixFirst: boolean;
  desktopAudioOn: boolean;
  pick: string;
  onPickChange: (id: string) => void;
  /** The new track list, and the volumes too when they had to follow it. */
  onChange: (next: string[], volumes?: Record<string, number>) => void;
  onRefresh: () => void;
  refreshing: boolean;
  onDuplicate: () => void;
  /** Percent per track id, applied when a clip is saved (#251). */
  volumes: Record<string, number>;
  onVolumeChange: (id: string, percent: number) => void;
  /** The microphone's own track, when capture adds one the list does not
   * show, with its volume. */
  micTrack: {value: number; onChange: (percent: number) => void} | null;
}) {
  const label = (id: string) => sources.find(s => s.id === id)?.label || id;

  const add = () => {
    if (!pick) return;
    if (tracks.includes(pick)) {
      onDuplicate();
      return;
    }
    onChange([...tracks, pick]);
  };

  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= tracks.length) return;
    const next = [...tracks];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };

  // The recorder only adds the combined track when there are at least two to
  // mix, so showing it below that would be a lie about what gets recorded.
  const showsMix = mixFirst && tracks.length > 1;
  const base = showsMix ? 2 : 1;
  const {dropped, trimmed} = tracksLostWithoutDesktopAudio(tracks);
  const warn = !desktopAudioOn && (dropped.length > 0 || trimmed.length > 0);
  // An app recorded on its own track and on the desktop track cannot be muted
  // out of a clip in the editor, which is the reason to give it a track.
  const doubled = desktopAudioOn ? appsAlsoOnDesktop(tracks) : [];
  const useEverythingElse = () => {
    const next = recordEverythingElse(tracks, volumes);
    onChange(next.tracks, next.volumes);
  };

  return (
    <div className="tracks">
      <div className="tracks-add">
        <div className="select-wrap">
          <select
            className="select"
            value={pick}
            aria-label={t('audioTracks.sourceToAdd')}
            onChange={e => onPickChange(e.target.value)}>
            {sources.map(source => (
              <option key={source.id} value={source.id}>
                {source.label || source.id}
              </option>
            ))}
          </select>
        </div>
        <button type="button" className="btn btn-quiet btn-sm" onClick={add} disabled={!pick}>
          {t('audioTracks.addTrack')}
        </button>
        <button
          type="button"
          className="btn btn-quiet btn-sm btn-icon"
          title={t('audioTracks.refresh')}
          aria-label={t('audioTracks.refresh')}
          disabled={refreshing}
          onClick={onRefresh}>
          <RefreshGlyph spinning={refreshing} />
        </button>
      </div>

      {tracks.length === 0 ? (
        <p className="tracks-empty">{t('audioTracks.none')}</p>
      ) : (
        <div className="track-list">
          {showsMix ? (
            <span className="track track-mix">
              <span className="track-num">1</span>
              <span className="track-id">{t('audioTracks.mixOfAll')}</span>
            </span>
          ) : null}
          {tracks.map((id, i) => (
            <span className="track" key={`${id}-${i}`} title={id}>
              <span className="track-num">{i + base}</span>
              <span className="track-id">{label(id)}</span>
              <button
                type="button"
                className="track-btn"
                title={t('audioTracks.moveEarlier')}
                aria-label={t('audioTracks.moveEarlierAria', {name: label(id)})}
                disabled={i === 0}
                onClick={() => move(i, -1)}>
                <ArrowGlyph dir="up" />
              </button>
              <button
                type="button"
                className="track-btn"
                title={t('audioTracks.moveLater')}
                aria-label={t('audioTracks.moveLaterAria', {name: label(id)})}
                disabled={i === tracks.length - 1}
                onClick={() => move(i, 1)}>
                <ArrowGlyph dir="down" />
              </button>
              <button
                type="button"
                className="track-btn track-btn-remove"
                title={t('audioTracks.removeTrack')}
                aria-label={t('audioTracks.removeAria', {name: label(id)})}
                onClick={() => onChange(tracks.filter((_, at) => at !== i))}>
                <IconClose size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      {tracks.length > 0 ? (
        <div className="track-levels">
          {tracks.map((id, i) => (
            <div className="track-level" key={`${id}-${i}`} title={id}>
              <span className="track-num">{i + base}</span>
              <span className="track-id">{label(id)}</span>
              <Slider
                label={t('audioTracks.levelAria', {name: label(id)})}
                value={volumes[id] ?? 100}
                min={0}
                max={200}
                step={5}
                onChange={percent => onVolumeChange(id, percent)}
                format={v => `${v}%`}
              />
            </div>
          ))}
          {micTrack ? (
            <div className="track-level">
              <span className="track-num">{tracks.length + base}</span>
              <span className="track-id">{t('audioTracks.micTrack')}</span>
              <Slider
                label={t('audioTracks.levelAria', {name: t('audioTracks.micTrack')})}
                value={micTrack.value}
                min={0}
                max={200}
                step={5}
                onChange={micTrack.onChange}
                format={v => `${v}%`}
              />
            </div>
          ) : null}
          <p className="tracks-empty">{showsMix ? t('audioTracks.levelsHelpMix') : t('audioTracks.levelsHelp')}</p>
        </div>
      ) : null}

      {doubled.length ? (
        <div className="tracks-warning tracks-doubled" role="status">
          <p>
            {t('audioTracks.doubledWarning', {count: doubled.length, list: doubled.join(', ')})}
          </p>
          <button type="button" className="btn btn-quiet btn-sm" onClick={useEverythingElse}>
            {t('audioTracks.useEverythingElse')}
          </button>
        </div>
      ) : null}

      {warn ? (
        <p className="tracks-warning" role="status">
          {dropped.length
            ? `${t('audioTracks.droppedWarning', {
                count: dropped.length,
                list: dropped.map(label).join(', '),
              })} `
            : ''}
          {trimmed.length ? `${t('audioTracks.trimmedWarning', {count: trimmed.length})} ` : ''}
          {t('audioTracks.turnBackOn')}
        </p>
      ) : null}
    </div>
  );
}

const RefreshGlyph = ({spinning}: {spinning?: boolean}) => (
  <svg
    width={14}
    height={14}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    className={spinning ? 'spin' : undefined}
    aria-hidden="true">
    <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
    <path d="M21 3v5h-5" />
    <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
    <path d="M8 16H3v5" />
  </svg>
);

const ArrowGlyph = ({dir}: {dir: 'up' | 'down'}) => (
  <svg
    width={11}
    height={11}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2.6}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true">
    <path d={dir === 'up' ? 'M12 19V5M6 11l6-6 6 6' : 'M12 5v14M6 13l6 6 6-6'} />
  </svg>
);
