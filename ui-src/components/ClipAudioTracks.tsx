import {useEffect, useRef, type RefObject} from 'react';

import {Toggle} from './settings/Fields';
import type {Clip} from '../lib/types';
import {t} from '../lib/i18n';

export type TrackSetting = {enabled: boolean; volume: number};

/** A clip's track settings by audio track index. Tracks not listed play as
 * recorded. */
export type TrackSettings = Record<number, TrackSetting>;

const AS_RECORDED: TrackSetting = {enabled: true, volume: 1};

/** Further apart than this, a track is put back on the video's time. */
const DRIFT = 0.2;

/** The sources a clip's tracks can be switched on and off for: every track
 * but the combined one, which only ever holds the rest. */
export function switchableTracks(clip: Clip) {
  return (clip.audio_tracks ?? []).filter(track => !track.combined);
}

/** The settings kept for a clip, as the daemon sent them. */
export function settingsFromClip(clip: Clip | null): TrackSettings {
  const out: TrackSettings = {};
  for (const [key, value] of Object.entries(clip?.track_mix ?? {})) {
    const index = Number(key);
    if (Number.isInteger(index) && value) out[index] = {enabled: value.enabled, volume: value.volume};
  }
  return out;
}

export interface TrackPlan {
  /** The tracks play beside the video, which is muted. */
  active: boolean;
  /** Something is set differently from how it was recorded. */
  changed: boolean;
  /** Each track's level before the player's own volume, by index. */
  gains: Record<number, number>;
}

/**
 * How a clip's tracks should play in the viewer.
 *
 * A clip whose first track is the whole recording plays its own sound unless
 * something is set differently. One whose first track is only a part
 * (separate tracks recorded without the combined one) always plays its
 * parts, or the viewer would leave out every app on a track of its own.
 * Tracks another track already holds are not played, so nothing is heard
 * twice.
 */
export function trackPlan(clip: Clip, settings: TrackSettings, solo: number | null): TrackPlan {
  const tracks = switchableTracks(clip);
  const knownParts = tracks.some(track => track.part);
  const hasCombined = (clip.audio_tracks ?? []).some(track => track.combined);
  const setting = (index: number) => settings[index] ?? AS_RECORDED;
  const changed = tracks.some(track => {
    const s = setting(track.index);
    return !s.enabled || Math.abs(s.volume - 1) > 0.005;
  });
  const partOnly = tracks.length >= 2 && knownParts && !hasCombined;
  const gains: Record<number, number> = {};
  for (const track of tracks) {
    const s = setting(track.index);
    gains[track.index] =
      solo !== null
        ? track.index === solo ? s.volume : 0
        : s.enabled && (track.part || !knownParts) ? s.volume : 0;
  }
  return {
    active: tracks.length >= 2 && (changed || solo !== null || partOnly),
    changed,
    gains,
  };
}

/**
 * Plays a clip's tracks in time with its video while *active*, each through
 * its own gain, so a change in the Audio tracks list is heard at once.
 */
export function useTrackPlayback({
  clip,
  videoRef,
  active,
  gains,
  playerVolume,
  playerMuted,
}: {
  clip: Clip | null;
  videoRef: RefObject<HTMLVideoElement | null>;
  active: boolean;
  gains: Record<number, number>;
  playerVolume: number;
  playerMuted: boolean;
}) {
  const playback = useRef<{
    context: AudioContext;
    players: Map<number, GainNode>;
  } | null>(null);
  const indexes = Object.keys(gains).join(',');

  // Set up while active, torn down the moment it stops.
  useEffect(() => {
    const video = videoRef.current;
    if (!active || !clip || !video) return;
    const context = new AudioContext();
    const revision = new URL(clip.video_url, window.location.href).search;
    const elements: HTMLAudioElement[] = [];
    const players = new Map<number, GainNode>();
    for (const index of indexes.split(',').filter(Boolean).map(Number)) {
      const audio = new Audio(`/api/clips/${encodeURIComponent(clip.slug)}/audio/${index}${revision}`);
      audio.preload = 'auto';
      const gain = context.createGain();
      gain.gain.value = 0;
      context.createMediaElementSource(audio).connect(gain).connect(context.destination);
      elements.push(audio);
      players.set(index, gain);
    }
    playback.current = {context, players};

    const sync = () => {
      if (context.state === 'suspended' && !video.paused) void context.resume();
      const play = !video.paused && !video.seeking;
      for (const audio of elements) {
        if (Math.abs(audio.currentTime - video.currentTime) > DRIFT) audio.currentTime = video.currentTime;
        audio.playbackRate = video.playbackRate;
        if (play && audio.paused) void audio.play().catch(() => {});
        else if (!play && !audio.paused) audio.pause();
      }
    };
    const events = ['play', 'pause', 'seeking', 'seeked', 'timeupdate', 'ratechange', 'waiting', 'playing'];
    events.forEach(name => video.addEventListener(name, sync));
    sync();
    return () => {
      events.forEach(name => video.removeEventListener(name, sync));
      for (const audio of elements) {
        audio.pause();
        audio.removeAttribute('src');
        audio.load();
      }
      void context.close();
      playback.current = null;
    };
    // A new clip or file rebuilds the players; levels move through the gains.
  }, [active, clip?.slug, clip?.video_url, indexes, videoRef]);

  // Levels follow every change without rebuilding the players.
  useEffect(() => {
    const current = playback.current;
    if (!current) return;
    for (const [index, gain] of current.players) {
      const level = playerMuted ? 0 : (gains[index] ?? 0) * playerVolume;
      gain.gain.setTargetAtTime(level, current.context.currentTime, 0.015);
    }
  });
}

/**
 * The audio tracks a clip was recorded with, under the viewer's player:
 * switch a source off or change how loud it is and hear it straight away
 * (useTrackPlayback). The viewer keeps each change for the clip, and share
 * links, the Discord embed and copied files are sent mixed that way, without
 * the sources switched off. The clip itself keeps every track. Opened from
 * the viewer's Audio tracks button, which only clips with two or more
 * sources have.
 */
export function ClipAudioTracks({
  clip,
  settings,
  onChange,
  onReset,
  solo,
  onSolo,
  plan,
  videoRef,
}: {
  clip: Clip;
  settings: TrackSettings;
  onChange: (index: number, patch: Partial<TrackSetting>) => void;
  onReset: () => void;
  solo: number | null;
  onSolo: (index: number | null) => void;
  plan: TrackPlan;
  videoRef: RefObject<HTMLVideoElement | null>;
}) {
  const tracks = switchableTracks(clip);
  const sectionRef = useRef<HTMLElement>(null);

  // Opened below the highlights, which can be out of sight in a short window.
  useEffect(() => {
    sectionRef.current?.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }, []);

  const setting = (index: number) => settings[index] ?? AS_RECORDED;
  const knownParts = tracks.some(track => track.part);

  return (
    <section className="viewer-tracks" ref={sectionRef} aria-label={t('viewer.tracksHeading')}>
      <div className="viewer-hl-head">
        <span className="eyebrow">{t('viewer.tracksHeading')}</span>
        {plan.changed ? (
          <button type="button" className="btn btn-quiet btn-sm" onClick={onReset}>
            {t('viewer.tracksReset')}
          </button>
        ) : null}
      </div>
      <p className="viewer-tracks-note">{t('viewer.tracksKept')}</p>
      <div className="viewer-track-list">
        {tracks.map(track => {
          const s = setting(track.index);
          const name = track.title || t('viewer.trackNumber', {number: track.index + 1});
          const percent = Math.round(s.volume * 100);
          const soloed = solo === track.index;
          return (
            <div className="viewer-track" key={track.index} data-off={!s.enabled}>
              <button
                type="button"
                className="btn btn-quiet btn-sm btn-icon"
                aria-pressed={soloed}
                title={soloed ? t('viewer.trackSoloOff') : t('viewer.trackSolo', {name})}
                aria-label={soloed ? t('viewer.trackSoloOff') : t('viewer.trackSolo', {name})}
                onClick={() => {
                  onSolo(soloed ? null : track.index);
                  const video = videoRef.current;
                  if (!soloed && video?.paused) void video.play().catch(() => {});
                }}>
                {soloed ? <StopGlyph /> : <PlayGlyph />}
              </button>
              <div className="viewer-track-name">
                <b>{name}</b>
                {knownParts && !track.part ? (
                  <span className="viewer-track-note">{t('viewer.trackAlsoElsewhere')}</span>
                ) : null}
              </div>
              <label className="viewer-track-volume">
                <input
                  type="range"
                  min="0"
                  max="200"
                  step="5"
                  value={percent}
                  aria-label={t('viewer.trackVolume', {name})}
                  onChange={e => onChange(track.index, {volume: Number(e.target.value) / 100})}
                />
                <span className="mono">{percent}%</span>
              </label>
              <Toggle
                label={t('viewer.trackOn', {name})}
                checked={s.enabled}
                onChange={enabled => onChange(track.index, {enabled})}
              />
            </div>
          );
        })}
      </div>
    </section>
  );
}

const PlayGlyph = () => (
  <svg width={12} height={12} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5z" />
  </svg>
);

const StopGlyph = () => (
  <svg width={12} height={12} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <rect x="5" y="5" width="14" height="14" rx="2" />
  </svg>
);
