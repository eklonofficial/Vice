import {useCallback, useLayoutEffect, useRef, useState} from 'react';
import {flushSync} from 'react-dom';

import {api} from '../lib/api';
import {useStore} from '../state/store';
import {Modal} from '../components/Modal';
import {formatDuration} from '../lib/format';
import {gameCaptureBlocked} from '../lib/gameCapture';
import {t} from '../lib/i18n';
import {IconCheck, IconPlus} from './Icons';
import {AccentPicker} from './AccentPicker';
import {useAccentChoice} from '../lib/accentChoice';
import {customAccent as deriveCustom} from '../theme/viceTheme';
import {KeyCapture} from './settings/KeyCapture';
import {Slider, Toggle} from './settings/Fields';
import {ACCENTS, ACCENT_NAMES} from '../theme/accents';

/** Titles in page order, so the dot row and the header cannot disagree. */
const PAGES = [
  'tutorial.pickColour',
  'tutorial.startClipping',
  'tutorial.bufferTitle',
  'tutorial.playlistsTitle',
  'tutorial.shareTitle',
  'tutorial.moreTitle',
];

/**
 * The first-run quick start. Picking an accent comes first, because it retints
 * the window underneath the modal, so the rest of the tutorial is read in the
 * colours the user just chose rather than in defaults they have not agreed to
 * yet.
 *
 * The two pages after it are the only settings a new user has to have an
 * opinion about, and they are set here rather than described, because a
 * tutorial that says where a control is has still left the work undone. Every
 * change persists as it is made: the modal has no Save.
 *
 * The copy carries the user's own hotkeys and clip length rather than the
 * defaults, so nothing on screen is wrong for the person reading it.
 */
export function Tutorial({open, onClose}: {open: boolean; onClose: () => void}) {
  const {state, hotkey, notify, saveConfig} = useStore();
  const {choose, chooseCustom, seed} = useAccentChoice();
  const [page, setPage] = useState(1);
  const [picking, setPicking] = useState(false);

  // The pages are different heights and different amounts of text, so moving
  // between them used to resize the window in one frame and swap the words
  // underneath it. The outgoing page leaves in the direction of travel, the
  // box grows or shrinks to the new page's height, and the new page arrives
  // from the other side.
  const wrapRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const running = useRef<Animation[]>([]);
  // Where the user has asked to be, which runs ahead of what is on screen
  // while a transition plays. Reading `page` instead meant five quick presses
  // of Next all resolved to "page 2": each handler saw the page from its own
  // render, and four of the five went nowhere.
  const target = useRef(page);

  const go = useCallback(
    (delta: number) => {
      const next = Math.min(Math.max(target.current + delta, 1), PAGES.length);
      if (next === target.current) return;
      target.current = next;

      const wrap = wrapRef.current;
      const inner = innerRef.current;
      // A page change must never be lost to a missing animation API.
      if (!wrap || !inner || typeof inner.animate !== 'function') {
        setPage(next);
        return;
      }
      // A second press interrupts rather than being swallowed. Dropping it
      // would leave the user having clicked Next with nothing happening, which
      // is worse than a transition that gets cut short.
      for (const a of running.current) a.cancel();
      running.current = [];

      const dir = delta > 0 ? 1 : -1;
      const from = wrap.offsetHeight;

      const leaving = inner.animate(
        [
          {opacity: 1, transform: 'none'},
          {opacity: 0, transform: `translateX(${-dir * 18}px)`},
        ],
        {duration: 140, easing: 'cubic-bezier(0.3, 0, 1, 1)', fill: 'forwards'},
      );
      running.current.push(leaving);

      leaving.onfinish = () => {
        // Synchronous so the new page can be measured in this callback rather
        // than a frame later, by which point the box has already jumped.
        flushSync(() => setPage(next));
        const to = wrap.offsetHeight;

        // Clipped only while the height is wrong for the content inside it.
        wrap.style.overflow = 'hidden';
        const growing = wrap.animate(
          [{height: `${from}px`}, {height: `${to}px`}],
          {duration: 280, easing: 'cubic-bezier(0.2, 0, 0, 1)'},
        );
        // Cancelling counts: an interrupted growth must not leave the box
        // clipped at a height that no longer matches its content.
        growing.onfinish = growing.oncancel = () => {
          wrap.style.overflow = '';
        };

        leaving.cancel();
        const arriving = inner.animate(
          [
            {opacity: 0, transform: `translateX(${dir * 18}px)`},
            {opacity: 1, transform: 'none'},
          ],
          {duration: 240, delay: 40, easing: 'cubic-bezier(0.2, 0, 0, 1)', fill: 'backwards'},
        );
        running.current = [growing, arriving];
      };
    },
    [],
  );

  // Closing and reopening must not resume mid-transition.
  useLayoutEffect(() => {
    if (open) return;
    for (const a of running.current) a.cancel();
    running.current = [];
    target.current = 1;
    if (wrapRef.current) wrapRef.current.style.overflow = '';
  }, [open]);
  const customBase = seed ? deriveCustom(seed).ramp.base : null;
  const duration = (state.config?.recording?.clip_duration as number | undefined) ?? 20;
  const buffer = (state.config?.recording?.buffer_duration as number | undefined) ?? 60;
  const clipDuration = (state.config?.recording?.clip_duration as number | undefined) ?? 20;
  const clipKey = (state.config?.hotkeys?.clip as string | undefined) ?? '';
  const shotKey = (state.config?.hotkeys?.screenshot as string | undefined) ?? '';
  const accent = state.accent;
  // Both default off, which is how Vice behaved before either existed. Asking
  // here is the only place a new user meets them before they matter.
  const gameCapture = Boolean(state.config?.recording?.window_capture);
  const gameCaptureBlock = gameCaptureBlocked(state.status);
  const shareDiscordFile = Boolean(state.config?.sharing?.share_discord_file);

  const persist = (patch: Record<string, Record<string, unknown>>) => {
    void saveConfig(patch).catch((err: Error) =>
      notify({
        kind: 'error',
        title: t('tutorial.errSave'),
        detail: err.message,
        tone: 'error',
        holdMs: 7000,
      }),
    );
  };

  const finish = () => {
    // Stored on the daemon as well as locally: the native window's
    // localStorage does not survive a restart on every QtWebEngine build,
    // which made the tutorial reappear on every launch.
    localStorage.setItem('vice_tutorial_shown', '1');
    // A new install has nothing to catch up on, so this version's notes count
    // as read.
    const version = state.status.version;
    if (version) localStorage.setItem('vice_whats_new_seen', version);
    void api.setAppState({tutorial_seen: true, ...(version ? {whats_new_seen: version} : {})}).catch(err => {
      console.debug('Recording that the tutorial was seen failed', err);
    });
    setPage(1);
    target.current = 1;
    onClose();
  };

  const unsupported = () =>
    notify({kind: 'error', title: t('settings.keyUnsupported'), tone: 'error', holdMs: 4000});

  return (
    <Modal
      open={open}
      title={t(PAGES[page - 1])}
      wide
      onClose={finish}
      footer={
        <>
          <span className="tut-dots" aria-hidden="true">
            {PAGES.map((key, i) => (
              <i key={key} data-active={page === i + 1 || undefined} />
            ))}
          </span>
          {page > 1 ? (
            <button type="button" className="btn btn-quiet" onClick={() => go(-1)}>
              {t('tutorial.back')}
            </button>
          ) : null}
          {page < PAGES.length ? (
            <button type="button" className="btn" onClick={() => go(1)}>
              {t('tutorial.next')}
            </button>
          ) : (
            <button type="button" className="btn" onClick={finish}>
              {t('tutorial.gotIt')}
            </button>
          )}
        </>
      }>
      <div className="tut-pages" ref={wrapRef}>
        <div className="tut-page" ref={innerRef}>
        {page === 1 ? (
          <>
            <p>{t('tutorial.colourBody')}</p>
            <div className="tut-accents">
              {ACCENT_NAMES.map(name => (
                <button
                  key={name}
                  type="button"
                  className="swatch swatch-lg"
                  data-active={accent === name || undefined}
                  style={{background: ACCENTS[name].base}}
                  title={t(`accents.${name}`)}
                  aria-label={t('tutorial.accentLabel', {name: t(`accents.${name}`)})}
                  aria-pressed={accent === name}
                  onClick={() => choose(name)}>
                  {accent === name ? <IconCheck size={16} /> : null}
                </button>
              ))}
              <button
                type="button"
                className="swatch swatch-lg swatch-custom"
                data-active={accent === 'custom' || undefined}
                style={customBase ? {background: customBase} : undefined}
                title={t('accents.customTitle')}
                aria-label={t('accents.customTitle')}
                aria-pressed={accent === 'custom'}
                onClick={() => setPicking(true)}>
                {accent === 'custom' ? <IconCheck size={16} /> : <IconPlus size={16} />}
              </button>
            </div>
          </>
        ) : page === 2 ? (
          <>
            <p>{t('tutorial.quickStartBody')}</p>
            <div className="tut-steps">
              <Step badge={hotkey} title={t('tutorial.saveLast', {duration})}>
                {t('tutorial.saveLastHelp', {hotkey})}
              </Step>
              <Step badge={`·${hotkey}·`} title={t('tutorial.sessionTitle', {hotkey})}>
                {t('tutorial.sessionHelp')}
              </Step>
            </div>

            <div className="tut-keys">
              <label className="tut-key">
                <span>{t('tutorial.clipKeyLabel')}</span>
                <KeyCapture
                  value={clipKey}
                  onUnsupported={unsupported}
                  onCapture={key => persist({hotkeys: {clip: key}})}
                />
              </label>
              <label className="tut-key">
                <span>{t('tutorial.screenshotKeyLabel')}</span>
                <KeyCapture
                  value={shotKey}
                  onUnsupported={unsupported}
                  onCapture={key => persist({hotkeys: {screenshot: key}})}
                />
              </label>
            </div>
            <p className="tut-note">{t('tutorial.screenshotKeyNote')}</p>
          </>
        ) : page === 3 ? (
          <>
            <p>{t('tutorial.bufferBody')}</p>
            <div className="tut-buffer">
              <Slider
                label={t('tutorial.bufferLabel')}
                exact
                value={clipDuration}
                min={5}
                max={300}
                step={5}
                // The buffer is what a clip is cut out of, so it is pushed up to
                // match rather than left behind where the daemon would clamp the
                // clip back down without saying so.
                onChange={next =>
                  persist({
                    recording: {clip_duration: next, buffer_duration: Math.max(buffer, next)},
                  })
                }
                format={v => formatDuration(v, true)}
              />
            </div>
            <p className="tut-note">
              {t('tutorial.bufferNote', {buffer: formatDuration(Math.max(buffer, clipDuration), true)})}
            </p>
            <Option
              title={t('tutorial.gameCaptureTitle')}
              help={gameCaptureBlock ?? t('tutorial.gameCaptureHelp')}
              checked={!gameCaptureBlock && gameCapture}
              disabled={Boolean(gameCaptureBlock)}
              onChange={enabled =>
                persist({recording: {window_capture: enabled, ...(enabled ? {follow_mouse_display: false} : {})}})
              }
            />
          </>
        ) : page === 4 ? (
          <>
            <p>{t('tutorial.playlistsBody')}</p>
            <div className="tut-steps">
              <Step badge={t('tutorial.badgeClip')} title={t('tutorial.reviewTitle')}>
                {t('tutorial.reviewHelp')}
              </Step>
              <Step badge={t('tutorial.badgeEdit')} title={t('tutorial.editorTitle')}>
                {t('tutorial.editorHelp')}
              </Step>
              <Step badge={t('tutorial.badgeAuto')} title={t('tutorial.autoTitle')}>
                {t('tutorial.autoHelp')}
              </Step>
              <Step badge={t('tutorial.badgeNew')} title={t('tutorial.customTitle')}>
                {t('tutorial.customHelp')}
              </Step>
            </div>
          </>
        ) : page === 5 ? (
          <>
            <p>{t('tutorial.shareBody')}</p>
            <div className="tut-steps">
              <Step badge={t('tutorial.badgeLink')} title={t('tutorial.linkTitle')}>
                {t('tutorial.linkHelp')}
              </Step>
              <Step badge={t('tutorial.badgeTunnel')} title={t('tutorial.tunnelTitle')}>
                {t('tutorial.tunnelHelp')}
              </Step>
            </div>
            <Option
              title={t('tutorial.shareDiscordTitle')}
              help={t('tutorial.shareDiscordHelp')}
              checked={shareDiscordFile}
              onChange={enabled => persist({sharing: {share_discord_file: enabled}})}
            />
          </>
        ) : (
          <>
            <p>{t('tutorial.moreBody')}</p>
            <div className="tut-steps">
              <Step badge={t('tutorial.badgeRec')} title={t('tutorial.recTitle')}>
                {t('tutorial.recHelp')}
              </Step>
              <Step badge={t('tutorial.badgeBg')} title={t('tutorial.backgroundTitle')}>
                {t('tutorial.backgroundHelp')}
              </Step>
              <Step badge={t('tutorial.badgeSafe')} title={t('tutorial.safeTitle')}>
                {t('tutorial.safeHelp')}
              </Step>
            </div>
          </>
        )}
        </div>
      </div>

      <AccentPicker
        open={picking}
        initial={seed}
        onCancel={() => setPicking(false)}
        onConfirm={next => {
          chooseCustom(next);
          setPicking(false);
        }}
      />
    </Modal>
  );
}

/** A choice made on the spot, laid out like a step with its switch beside it. */
function Option({
  title,
  help,
  checked,
  disabled,
  onChange,
}: {
  title: string;
  help: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className="tut-step tut-option">
      <div>
        <b>{title}</b>
        <span>{help}</span>
      </div>
      <Toggle label={title} checked={checked} disabled={disabled} onChange={onChange} />
    </div>
  );
}

function Step({
  badge,
  title,
  children,
}: {
  badge: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="tut-step">
      <span className="tut-badge mono">{badge}</span>
      <div>
        <b>{title}</b>
        <span>{children}</span>
      </div>
    </div>
  );
}
