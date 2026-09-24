import {useMemo, useState} from 'react';

import {useStore} from '../state/store';
import {copyToClipboard} from '../lib/clipboard';
import {formatBytes, formatLengthLong} from '../lib/format';
import {Wordmark} from '../components/Wordmark';
import {t} from '../lib/i18n';
import {Modal} from '../components/Modal';
import {IconMark} from '../components/Icons';

const UNINSTALL_CMD = 'vice uninstall';

export function About() {
  const {state, notify} = useStore();
  const {status, config, clips} = state;
  const [manualCopy, setManualCopy] = useState<string | null>(null);

  const totals = useMemo(() => {
    const seconds = clips.reduce((sum, c) => sum + (c.duration ?? 0), 0);
    const bytes = clips.reduce((sum, c) => sum + (c.size ?? 0), 0);
    return {
      count: clips.length,
      bytes,
      footage:
        seconds < 60
          ? `${Math.round(seconds)}s`
          : `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`,
    };
  }, [clips]);

  const recording = (config?.recording ?? {}) as Record<string, unknown>;
  const version = status.version || '';
  const isWindows = status.platform === 'windows';

  const rows: Array<[string, string]> = [
    [t('about.version'), version || 'unknown'],
    [t('about.backend'), status.backend || (recording.backend as string) || 'auto'],
    [t('about.buffer'), formatLengthLong((recording.buffer_duration as number) ?? 120)],
    [t('about.frameRate'), `${(recording.fps as number) ?? 60} fps`],
    [t('about.clipsOnDisk'), `${totals.count} · ${formatBytes(totals.bytes)}`],
    [t('about.totalFootage'), totals.footage],
  ];

  const copyUninstall = async () => {
    if (await copyToClipboard(UNINSTALL_CMD)) {
      notify({
        kind: 'info',
        title: t('about.commandCopied'),
        tone: 'accent',
        holdMs: 4000,
      });
    } else {
      setManualCopy(UNINSTALL_CMD);
    }
  };

  return (
    <div className="about">
      <header className="about-head">
        <h1>{t('about.title')}</h1>
        <p>{isWindows ? t('about.subtitleWindows') : t('about.subtitle')}</p>
      </header>

      <section className="about-hero">
        <span className="about-mark">
          <IconMark size={30} />
        </span>
        <div>
          <h2>
            <Wordmark height={26} />
          </h2>
          <p>{isWindows ? t('about.taglineWindows') : t('about.tagline')}</p>
          <div className="about-chips">
            <span className="about-chip mono">{version || 'unknown'}</span>
            <span className="about-chip mono">GPL-3.0</span>
            <span className="about-chip mono">
              {isWindows ? t('about.windows') : t('about.waylandAndX11')}
            </span>
          </div>
        </div>
      </section>

      <div className="about-grid">
        <section className="about-card">
          <h3 className="eyebrow">{t('about.system')}</h3>
          {rows.map(([label, value]) => (
            <div className="about-row" key={label}>
              <span>{label}</span>
              <b className="mono">{value}</b>
            </div>
          ))}
        </section>

        <section className="about-card">
          <h3 className="eyebrow">{t('about.credits')}</h3>
          <div className="credit">
            <span className="credit-avatar">A</span>
            <div>
              <b>Andrew Marin</b>
              <span>{t('about.creator')}</span>
            </div>
          </div>
          <div className="credit">
            <span className="credit-avatar credit-avatar-dim">C</span>
            <div>
              <b>{t('about.community')}</b>
              <span>{t('about.communityHelp')}</span>
            </div>
          </div>
        </section>
      </div>

      <section className="about-danger">
        <h3 className="eyebrow">{t('about.dangerZone')}</h3>
        <p>{isWindows ? t('about.uninstallHelpWindows') : t('about.uninstallHelp')}</p>
        <div className="about-cmd">
          <code className="mono">{UNINSTALL_CMD}</code>
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => void copyUninstall()}>
            {t('about.copy')}
          </button>
        </div>
      </section>

      <Modal open={manualCopy !== null} title={t('about.copyTitle')} onClose={() => setManualCopy(null)}>
        <p>{t('about.copyByHand')}</p>
        <textarea className="manual-copy" readOnly value={manualCopy ?? ''} rows={2} />
      </Modal>
    </div>
  );
}
