import {useEffect, useState} from 'react';

import {api} from '../lib/api';
import {copyToClipboard} from '../lib/clipboard';
import {t} from '../lib/i18n';
import type {GsrInstall, Status} from '../lib/types';
import {useStore} from '../state/store';
import {IconCheck} from './Icons';

/** How long the finished state stays before Home takes over again. */
const DONE_HOLD_MS = 2400;

/**
 * Whether Home should show the setup step: the Vice Flatpak is missing the gpu-screen-recorder Flatpak it
 * records with. Only the Flatpak can be in this state: a Flatpak cannot
 * depend on another app, so Vice installs it on request.
 */
export function needsGsrSetup(status: Status, install: GsrInstall | null): boolean {
  // An install in progress or just finished keeps the card up until its own
  // finished state has been seen.
  if (install) return true;
  return status.platform?.packaging === 'flatpak' && status.gsr?.source == null;
}

const RING = 2 * Math.PI * 34;

/**
 * The setup step that stands in for Home until gpu-screen-recorder is there.
 * Not dismissible and not a banner: without it Vice cannot record anything.
 */
export function GsrSetup() {
  const {state, dispatch} = useStore();
  const progress = state.gsrInstall;
  const phase = progress?.phase ?? null;
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (phase !== 'done') return;
    const timer = window.setTimeout(() => dispatch({type: 'gsrInstall', value: null}), DONE_HOLD_MS);
    return () => window.clearTimeout(timer);
  }, [phase, dispatch]);

  const start = () => {
    dispatch({type: 'gsrInstall', value: {phase: 'preparing', percent: null}});
    void api
      .installGsr()
      .then(result => {
        if (result?.ok === false) throw new Error(result.error || t('gsrSetup.failed'));
      })
      .catch((err: Error) =>
        dispatch({
          type: 'gsrInstall',
          value: {phase: 'error', percent: null, error: err.message, command: MANUAL_COMMAND},
        }),
      );
  };

  const working = phase === 'preparing' || phase === 'downloading' || phase === 'installing';
  const percent = progress?.percent ?? null;
  const command = progress?.command || MANUAL_COMMAND;

  return (
    <section className="gsr-setup" aria-live="polite" data-phase={phase ?? 'idle'}>
      <div className="gsr-setup-card">
        {phase === 'done' ? (
          <>
            <div className="gsr-setup-done" aria-hidden="true">
              <IconCheck size={46} />
            </div>
            <h1>{t('gsrSetup.doneTitle')}</h1>
            <p>{t('gsrSetup.doneBody')}</p>
          </>
        ) : working ? (
          <>
            <div
              className="gsr-setup-ring"
              role="progressbar"
              aria-label={t('gsrSetup.installing')}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent ?? undefined}
              data-indeterminate={percent === null || undefined}>
              <svg viewBox="0 0 80 80" aria-hidden="true">
                <circle className="gsr-setup-ring-track" cx="40" cy="40" r="34" />
                <circle
                  className="gsr-setup-ring-fill"
                  cx="40"
                  cy="40"
                  r="34"
                  strokeDasharray={RING}
                  strokeDashoffset={percent === null ? RING * 0.72 : RING * (1 - percent / 100)}
                />
              </svg>
              <span className="gsr-setup-ring-value mono">{percent === null ? '' : `${percent}%`}</span>
            </div>
            <div className="gsr-setup-bar" aria-hidden="true">
              <span style={{width: `${percent ?? 0}%`}} />
            </div>
            <h1>{t(`gsrSetup.phase.${phase}`)}</h1>
            <p>{t('gsrSetup.workingBody')}</p>
          </>
        ) : (
          <>
            <span className="gsr-setup-eyebrow">{t('gsrSetup.eyebrow')}</span>
            <h1>{phase === 'error' ? t('gsrSetup.failedTitle') : t('gsrSetup.title')}</h1>
            {phase === 'error' ? (
              <>
                <p className="gsr-setup-error mono">{progress?.error || t('gsrSetup.failed')}</p>
                <p>{t('gsrSetup.manual')}</p>
                <button
                  type="button"
                  className="gsr-setup-command mono"
                  title={t('gsrSetup.copyCommand')}
                  onClick={() => void copyToClipboard(command).then(ok => setCopied(ok))}>
                  {command}
                </button>
                {copied ? <span className="gsr-setup-copied">{t('gsrSetup.copied')}</span> : null}
              </>
            ) : (
              <p>{t('gsrSetup.body')}</p>
            )}
            <button type="button" className="btn gsr-setup-go" onClick={start}>
              {phase === 'error' ? t('gsrSetup.retry') : t('gsrSetup.install')}
            </button>
            <span className="gsr-setup-note">{t('gsrSetup.note')}</span>
          </>
        )}
      </div>
    </section>
  );
}

const MANUAL_COMMAND = 'flatpak install --system flathub com.dec05eba.gpu_screen_recorder';
