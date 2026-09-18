import {useStore, type BannerId} from '../state/store';
import {H264_SUPPORTED} from '../lib/env';
import {IconClose, IconWarning} from './Icons';
import {t} from '../lib/i18n';

/**
 * The four states worth interrupting for. Wording is carried over from the
 * releases that introduced each one: it took real back and forth with the
 * reporters to land on text that says what happened and what to do about it.
 */
export function Banners() {
  const {state, dispatch} = useStore();
  const {status, dismissed} = state;

  const showing = (id: BannerId) => !dismissed.includes(id);
  const dismiss = (banner: BannerId) => () => dispatch({type: 'dismiss', banner});

  const recorderDown = status.ready === false && Boolean(status.recorder_error);

  return (
    <div className="banners">
      {/* Not dismissible: the daemon is up but nothing is being recorded, so
          the window exists mainly to say this and keep Settings reachable. */}
      {recorderDown ? (
        <Banner tone="error">
          <strong>{t('banners.notRecording')}</strong>
          <span className="banner-mono">{status.recorder_error}</span>
          <span>{t('banners.notRecordingHelp')}</span>
        </Banner>
      ) : null}

      {status.cpu_fallback && showing('cpu') ? (
        <Banner tone="warning" onDismiss={dismiss('cpu')}>
          <strong>{t('banners.cpuFallback')}</strong>
          <span>{t('banners.cpuFallbackHelp')}</span>
        </Banner>
      ) : null}

      {status.codec_fallback && showing('codec-gpu') ? (
        <Banner tone="warning" onDismiss={dismiss('codec-gpu')}>
          <strong>{t('banners.codecFallback')}</strong>
          <span>
            {status.platform === 'windows'
              ? t('banners.codecFallbackHelpWindows')
              : t('banners.codecFallbackHelp')}
          </span>
        </Banner>
      ) : null}

      {!H264_SUPPORTED && showing('codec-h264') ? (
        <Banner tone="warning" onDismiss={dismiss('codec-h264')}>
          <strong>{t('banners.cannotPlay')}</strong>
          <span>{t('banners.cannotPlayHelp')}</span>
          <span className="banner-mono">sudo apt install python3-pyqt6.qtwebengine</span>
          <span className="banner-mono">sudo dnf install python3-pyqt6-webengine</span>
          <span className="banner-mono">sudo zypper install python3-qt6-webengine</span>
        </Banner>
      ) : null}
    </div>
  );
}

function Banner({
  tone,
  onDismiss,
  children,
}: {
  tone: 'error' | 'warning';
  onDismiss?: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="banner" data-tone={tone} role={tone === 'error' ? 'alert' : 'status'}>
      <IconWarning size={17} className="banner-icon" />
      <div className="banner-text">{children}</div>
      {onDismiss ? (
        <button type="button" className="banner-close" onClick={onDismiss} aria-label={t('common.dismiss')}>
          <IconClose size={14} />
        </button>
      ) : null}
    </div>
  );
}
