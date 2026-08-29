import {useMemo, useState} from 'react';

import {useStore} from '../state/store';
import {useClipActions} from '../state/clipActions';
import {usePlaylistDropTarget} from '../lib/clipDrag';
import {api} from '../lib/api';
import {copyToClipboard} from '../lib/clipboard';
import {formatDuration} from '../lib/format';
import {t, tNode} from '../lib/i18n';
import {ClipCard} from '../components/ClipCard';
import {Tile, ActionTile} from '../components/Tile';
import {CaptureModeTile, type CaptureMode} from '../components/CaptureModeTile';
import {Modal} from '../components/Modal';
import {IconClips, IconPlaylist, IconSettings} from '../components/Icons';

const ROW_LIMIT = 8;

/** The distinctive part of a quick-tunnel URL, which is all that identifies it. */
function tunnelHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/\.trycloudflare\.com$/, '');
  } catch {
    return url;
  }
}

function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return t('home.greetingMorning');
  if (hour < 18) return t('home.greetingAfternoon');
  return t('home.greetingEvening');
}

export function Home() {
  const {state, dispatch, hotkey, saveConfig, notify} = useStore();
  const {clips, playlists, config, tunnelUrl, recentNew, status} = state;

  const {actions, overlays} = useClipActions();
  const [busy, setBusy] = useState<string | null>(null);
  const [manualCopy, setManualCopy] = useState<string | null>(null);
  const [restartNeeded, setRestartNeeded] = useState(false);
  const [wfMicPrompt, setWfMicPrompt] = useState(false);

  const clipDuration = (config?.recording?.clip_duration as number | undefined) ?? 20;
  const captureAudio = config?.recording?.capture_audio !== false;
  const captureMic = Boolean(config?.recording?.capture_microphone);
  const tunnelOn = Boolean(config?.sharing?.cloudflare_tunnel);
  const captureMode: CaptureMode = config?.recording?.window_capture ? 'active_game' : 'desktop';
  // Window pinning is GSR-only; status.backend is what's actually running,
  // not just configured (backend can be "auto").
  const captureModeSupported = status.backend === 'gpu-screen-recorder';

  const recent = useMemo(() => clips.slice(0, ROW_LIMIT), [clips]);
  const mostViewed = useMemo(
    () =>
      clips
        .filter(c => (c.views ?? 0) > 0)
        .sort((a, b) => (b.views ?? 0) - (a.views ?? 0))
        .slice(0, ROW_LIMIT),
    [clips],
  );

  /**
   * wf-recorder cannot mix a microphone in without being told how, so asking
   * first is the only way to avoid silently recording the wrong thing.
   */
  const micNeedsWfChoice =
    !captureMic &&
    captureAudio &&
    (config?.recording?.wf_microphone_strategy ?? 'prompt') === 'prompt' &&
    ((config?.recording?.backend as string) === 'wf-recorder' || status.backend === 'wf-recorder');

  const toggle = async (
    key: string,
    patch: Record<string, Record<string, unknown>>,
    onOk: (result: {restart_required?: boolean; applied?: boolean; warning?: string}) => void,
    failure: string,
  ) => {
    setBusy(key);
    try {
      const result = await saveConfig(patch);
      if (result.applied === false && result.warning) {
        notify({kind: 'error', title: t('home.savedNotApplied'), detail: result.warning, tone: 'error', holdMs: 8000});
      } else {
        onOk(result);
      }
      if (result.restart_required) setRestartNeeded(true);
    } catch (err) {
      notify({kind: 'error', title: failure, detail: (err as Error).message, tone: 'error', holdMs: 7000});
    } finally {
      setBusy(null);
    }
  };

  const setMic = (enabled: boolean, strategy?: string) =>
    toggle(
      'mic',
      {recording: {capture_microphone: enabled, ...(strategy ? {wf_microphone_strategy: strategy} : {})}},
      () =>
        notify({
          kind: 'info',
          title: enabled ? t('home.micOn') : t('home.micOff'),
          detail: enabled ? t('home.micOnDetail') : t('home.micOffDetail'),
          tone: 'accent',
          holdMs: 3000,
        }),
      t('home.errMic'),
    );

  const setCaptureMode = (mode: CaptureMode) =>
    toggle(
      'captureMode',
      {recording: {window_capture: mode === 'active_game', capture_mode: mode}},
      () =>
        notify({
          kind: 'info',
          title: mode === 'active_game' ? t('home.gameCaptureOn') : t('home.gameCaptureOff'),
          detail: mode === 'active_game' ? t('home.gameCaptureOnDetail') : undefined,
          tone: 'accent',
          holdMs: 3000,
        }),
      t('home.errGameCapture'),
    );

  const copyTunnel = async () => {
    if (!tunnelUrl) {
      notify({kind: 'error', title: t('home.enablePublicLinkFirst'), tone: 'error', holdMs: 4000});
      return;
    }
    if (await copyToClipboard(tunnelUrl)) {
      notify({kind: 'info', title: t('home.publicLinkCopied'), tone: 'accent', holdMs: 3000});
    } else {
      setManualCopy(tunnelUrl);
    }
  };

  return (
    <div className="home">
      <header className="home-hero">
        <h1>{greeting()}</h1>
        <p>
          {tNode('home.lede', {
            duration: <b key="d">{formatDuration(clipDuration, true)}</b>,
            hotkey: <kbd key="k">{hotkey}</kbd>,
          })}
        </p>
      </header>

      <section className="tiles" aria-label={t('home.quickSettings')}>
        <div className="tile-row tile-row-3">
          <Tile
            label={t('home.microphone')}
            detail={captureMic ? t('home.on') : t('home.off')}
            on={captureMic}
            busy={busy === 'mic'}
            icon={<MicIcon />}
            onToggle={() => {
              if (micNeedsWfChoice) setWfMicPrompt(true);
              else void setMic(!captureMic);
            }}
          />
          <Tile
            label={t('home.desktopAudio')}
            detail={captureAudio ? t('home.on') : t('home.off')}
            on={captureAudio}
            busy={busy === 'audio'}
            icon={<SpeakerIcon />}
            onToggle={() =>
              void toggle(
                'audio',
                {recording: {capture_audio: !captureAudio}},
                () =>
                  notify({
                    kind: 'info',
                    title: !captureAudio ? t('home.desktopAudioOn') : t('home.desktopAudioOff'),
                    tone: 'accent',
                    holdMs: 3000,
                  }),
                t('home.errDesktopAudio'),
              )
            }
          />
          <CaptureModeTile
            mode={captureMode}
            supported={captureModeSupported}
            busy={busy === 'captureMode'}
            onSelect={mode => void setCaptureMode(mode)}
          />
        </div>

        <div className="tile-row tile-row-2">
          <Tile
            label={t('home.publicLink')}
            detail={tunnelOn ? (tunnelUrl ? t('home.active') : t('home.starting')) : t('home.off')}
            on={tunnelOn}
            busy={busy === 'tunnel'}
            icon={<GlobeIcon />}
            onToggle={() =>
              void toggle(
                'tunnel',
                {sharing: {cloudflare_tunnel: !tunnelOn}},
                () =>
                  notify({
                    kind: 'info',
                    title: !tunnelOn ? t('home.publicLinkStarting') : t('home.publicLinkStopped'),
                    tone: 'accent',
                    holdMs: 3500,
                  }),
                t('home.errPublicLink'),
              )
            }
          />
          {/* The address, not a second copy of the switch beside it. */}
          <button
            type="button"
            className="tile tile-readout"
            onClick={copyTunnel}
            disabled={!tunnelUrl}
            aria-label={tunnelUrl ? t('home.copyPublicLink') : t('home.noPublicLinkYet')}>
            <span className="tile-badge" aria-hidden="true">
              <LinkIcon />
            </span>
            <span className="tile-text">
              <b>
                {tunnelUrl
                  ? tunnelHost(tunnelUrl)
                  : tunnelOn
                    ? t('home.connecting')
                    : t('home.localOnly')}
              </b>
              <span className="tile-mono">
                {tunnelUrl
                  ? t('home.tapToCopy')
                  : tunnelOn
                    ? t('home.cloudflaredStarting')
                    : t('home.linksWorkOnNetwork')}
              </span>
            </span>
          </button>
        </div>

        <div className="tile-row tile-row-3">
          <ActionTile
            label={t('home.saveClipNow')}
            icon={<ClipIcon />}
            onClick={() => {
              void api.triggerClip().catch((err: Error) =>
                notify({kind: 'error', title: t('home.errSaveClip'), detail: err.message, tone: 'error', holdMs: 7000}),
              );
            }}
          />
          <ActionTile
            label={t('home.allClips')}
            icon={<IconClips size={19} />}
            onClick={() => dispatch({type: 'setView', view: 'clips', playlistId: null})}
          />
          <ActionTile
            label={t('home.settings')}
            icon={<IconSettings size={19} />}
            onClick={() => dispatch({type: 'setView', view: 'settings'})}
          />
        </div>
      </section>

      <ClipRow
        title={t('home.recentClips')}
        action={{label: t('home.seeAll'), onClick: () => dispatch({type: 'setView', view: 'clips', playlistId: null})}}
        clips={recent}
        recentNew={recentNew}
        actions={actions}
        empty={t('home.emptyReel', {hotkey})}
      />

      {playlists.length > 0 ? (
        <section className="home-section">
          <div className="home-section-head">
            <h2>{t('home.playlists')}</h2>
          </div>
          <div className="playlist-row">
            {playlists.map(playlist => (
              <PlaylistChip
                key={playlist.id}
                playlist={playlist}
                onOpen={() => dispatch({type: 'setView', view: 'clips', playlistId: playlist.id})}
                onDone={(message, tone) =>
                  notify({
                    kind: tone === 'error' ? 'error' : 'info',
                    title: message,
                    tone,
                    holdMs: tone === 'error' ? 7000 : 3000,
                  })
                }
              />
            ))}
          </div>
        </section>
      ) : null}

      {mostViewed.length > 0 ? (
        <ClipRow title={t('home.mostViewed')} clips={mostViewed} recentNew={recentNew} actions={actions} />
      ) : null}

      {overlays}

      <Modal
        open={wfMicPrompt}
        title={t('home.wfMicTitle')}
        onClose={() => setWfMicPrompt(false)}>
        <p>{t('home.wfMicBody')}</p>
        <div className="choice-list">
          <button
            type="button"
            className="choice"
            onClick={() => {
              setWfMicPrompt(false);
              void setMic(true, 'backend_fallback');
            }}>
            <b>{t('home.wfMicBothLabel')}</b>
            <span>{t('home.wfMicBothHelp')}</span>
          </button>
          <button
            type="button"
            className="choice"
            onClick={() => {
              setWfMicPrompt(false);
              void setMic(true, 'mic_only');
            }}>
            <b>{t('home.wfMicOnlyLabel')}</b>
            <span>{t('home.wfMicOnlyHelp')}</span>
          </button>
        </div>
      </Modal>

      <Modal
        open={manualCopy !== null}
        title={t('home.copyLinkTitle')}
        onClose={() => setManualCopy(null)}>
        <p>{t('home.copyLinkBody')}</p>
        <textarea className="manual-copy" readOnly value={manualCopy ?? ''} rows={3} />
      </Modal>

      <Modal
        open={restartNeeded}
        title={t('home.restartTitle')}
        onClose={() => setRestartNeeded(false)}
        footer={
          <button type="button" className="btn" onClick={() => setRestartNeeded(false)}>
            {t('home.gotIt')}
          </button>
        }>
        <p>{t('home.restartBody')}</p>
      </Modal>
    </div>
  );
}

/** A playlist tile that also accepts a clip dropped onto it. */
function PlaylistChip({
  playlist,
  onOpen,
  onDone,
}: {
  playlist: import('../lib/types').Playlist;
  onOpen: () => void;
  onDone: (message: string, tone: 'accent' | 'error') => void;
}) {
  const drop = usePlaylistDropTarget(playlist, onDone);
  const count = playlist.clip_slugs.length;
  return (
    <button
      type="button"
      className="playlist-chip"
      data-drop-over={drop.over || undefined}
      data-received={drop.caught || undefined}
      style={playlist.color1 ? ({'--chip': playlist.color1} as React.CSSProperties) : undefined}
      onClick={onOpen}
      {...drop.props}>
      <span className="playlist-chip-mark" aria-hidden="true">
        {playlist.emoji || <IconPlaylist size={15} />}
      </span>
      <span className="playlist-chip-text">
        <b>{playlist.name}</b>
        <span>{t('clips.countClips', {count})}</span>
      </span>
    </button>
  );
}

function ClipRow({
  title,
  clips,
  recentNew,
  actions,
  action,
  empty,
}: {
  title: string;
  clips: import('../lib/types').Clip[];
  recentNew: string[];
  actions: import('../components/ClipCard').ClipActions;
  action?: {label: string; onClick: () => void};
  empty?: string;
}) {
  return (
    <section className="home-section">
      <div className="home-section-head">
        <h2>{title}</h2>
        {action ? (
          <button type="button" className="section-link" onClick={action.onClick}>
            {action.label}
          </button>
        ) : null}
      </div>
      {clips.length === 0 && empty ? (
        <p className="home-empty">{empty}</p>
      ) : (
        <div className="clip-row">
          {clips.map(clip => (
            <ClipCard
              key={clip.slug}
              clip={clip}
              draggable
              isNew={recentNew.includes(clip.slug)}
              actions={actions}
            />
          ))}
        </div>
      )}
    </section>
  );
}

const LinkIcon = () => (
  <svg {...stroke} width={19} height={19} viewBox="0 0 24 24">
    <path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1" />
    <path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1" />
  </svg>
);

const stroke = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
};

const MicIcon = () => (
  <svg width="19" height="19" viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <rect x="9" y="2" width="6" height="12" rx="3" />
    <path d="M5 11a7 7 0 0 0 14 0M12 18v4" />
  </svg>
);

const SpeakerIcon = () => (
  <svg width="19" height="19" viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M4 9v6h4l5 4V5L8 9H4z" />
    <path d="M17 8.5a5 5 0 0 1 0 7" />
  </svg>
);

const GlobeIcon = () => (
  <svg width="19" height="19" viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18" />
  </svg>
);

const ClipIcon = () => (
  <svg width="19" height="19" viewBox="0 0 24 24" {...stroke} aria-hidden="true">
    <path d="M5 3h11l3 3v15H5z" />
    <path d="M9 3v6h6" />
  </svg>
);
