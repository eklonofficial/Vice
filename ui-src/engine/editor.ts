import {formatBytes} from '../lib/format';
// Aliased: this file uses `t` as a local for times and tracks throughout.
import {t as translate} from '../lib/i18n';
import {clipNeedsProxy, playbackUrl} from '../lib/playback';
import {clipTitle, type Clip} from '../lib/types';
import {
  ED_FONTS,
  ED_FX,
  ED_ICONS,
  ED_TEXT_PRESETS,
  edFx,
  edGlyph,
  fxName,
} from './editorConstants';
import type {EdItem, EdProject, EdSnapshot, EdTab, EdTrack, EditorDeps} from './editorTypes';

/**
 * The timeline editor's engine.
 *
 * Everything here that touches playback timing, the video pool, transition
 * approximation or snapping is carried over from the shipped editor rather
 * than re-derived. Each of those pieces has a comment saying which failure it
 * exists to prevent, and none of them is obvious from the code alone:
 *
 *   - three video elements per track, not two, or a run of transitions
 *     reassigns a src every frame and freezes
 *   - the clock reads from the master video while a clip is on screen, or a
 *     corrective seek fires every few frames and stutters
 *   - a fresh src gets a short muted warm-up, or its first frames arrive late
 *   - the outgoing element is parked on its tail so transitions blend real
 *     frames rather than black
 *
 * React owns the chrome around this and reads state through `subscribe`. The
 * engine owns the stage, the timeline canvas and the library list, because all
 * three are drag-heavy imperative surfaces where a virtual DOM buys nothing.
 */

const ED_PPS_MIN = 4;
const ED_PPS_MAX = 48;
const ED_UNDO_CAP = 50;
const ED_RAIL = 54;
const ED_DRIFT = 0.15;
/** Seconds before an item to start warming its video. */
const ED_PRELOAD = 2.0;
/** Muted decode warm-up after a src change. */
const ED_WARM_MS = 350;

const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

interface PoolEntry {
  els: HTMLVideoElement[];
  itemKey: string | null;
  shown: HTMLVideoElement | null;
  handover: boolean;
  seq: number;
  zBase: number;
  wasMaster?: boolean;
}

interface PoolVideo extends HTMLVideoElement {
  _key: string | null;
  _need: number;
  _role: string | null;
  _parkAt: number;
  _warmUntil: number;
}

interface TrackState {
  it: EdItem;
  cur: PoolVideo;
  prev: PoolVideo | null;
}

interface Need {
  key: string;
  it: EdItem;
  role: string;
  parkAt: number;
  el?: PoolVideo;
}

export interface EditorContainers {
  stage: HTMLElement;
  stageWrap: HTMLElement;
  timelineCanvas: HTMLElement;
  timelineScroll: HTMLElement;
  libraryScroll: HTMLElement;
  fadeOverlay: HTMLElement;
}

export interface EditorEngine {
  mount: (containers: EditorContainers) => void;
  destroy: () => void;
  subscribe: (fn: (snapshot: EdSnapshot) => void) => () => void;
  snapshot: () => EdSnapshot;
  load: () => Promise<void>;
  setClips: (clips: Clip[]) => void;
  onClipDeleted: (slug: string) => void;
  onProjectChanged: () => Promise<void>;
  saveNow: () => Promise<void>;
  isDirty: () => boolean;
  project: () => EdProject | null;
  setTab: (tab: EdTab) => void;
  search: (query: string) => void;
  zoom: (factor: number) => void;
  fit: () => void;
  seek: (t: number) => void;
  setPlaying: (playing: boolean) => void;
  split: () => void;
  detachAudio: () => void;
  duplicate: () => void;
  remove: () => void;
  addTrack: (type: 'video' | 'audio') => void;
  reset: () => void;
  select: (id: string | null) => void;
  inspectorChange: (field: string, value: string | number | boolean) => void;
  end: () => number;
}

export function createEditorEngine(deps: EditorDeps): EditorEngine {
  // ── state ───────────────────────────────────────────────────────
  let project: EdProject | null = null;
  let sel: string | null = null;
  let playhead = 0;
  let playing = false;
  let pps = 12;
  let clipboard: EdItem | null = null;
  let missing = new Set<string>();
  let loaded = false;
  let dirty = false;
  let saveTimer: number | undefined;
  let undoStack: string[] = [];
  let redoStack: string[] = [];
  let tab: EdTab = 'library';
  let query = '';
  let mounted = false;
  let box: EditorContainers | null = null;
  let preparingNow = false;
  let audioWaiting = false;
  let stageEmpty = true;

  let raf: number | null = null;
  let lastTick = 0;
  let pool: Record<string, PoolEntry> = {};
  let poolKey = '';
  let audioPool: Record<string, HTMLAudioElement> = {};
  let lastTextKey = '';
  let master: TrackState | null = null;
  let stageObserver: ResizeObserver | null = null;
  let drag: {kind: string; id: string} | null = null;
  let dragGhost: HTMLElement | null = null;
  let dropHint: {trackId: string; t?: number; w?: number; junctionX?: number} | null = null;

  const listeners = new Set<(s: EdSnapshot) => void>();

  // ── helpers ─────────────────────────────────────────────────────
  const clips = () => deps.clips();
  const uid = () => 'i' + Math.random().toString(36).slice(2, 9);
  const round = (v: number) => Math.round(v * 1000) / 1000;
  const clipOf = (it: EdItem) => clips().find(c => c.slug === it.clipId);
  const fmtS = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
  const escHtml = (s: unknown) =>
    String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const escAttr = (s: unknown) =>
    String(s).replace(/&/g, '&amp;').replace(/'/g, '&#39;').replace(/"/g, '&quot;');
  const capture = (el: Element, e: PointerEvent) => {
    try {
      el.setPointerCapture(e.pointerId);
    } catch {
      // Pointer capture is a nicety; a drag still works without it.
    }
  };

  const end = () =>
    project ? project.items.reduce((m, i) => Math.max(m, i.start + i.dur), 0) : 0;
  const items = (trackId: string) =>
    project!.items.filter(i => i.trackId === trackId).sort((a, b) => a.start - b.start);
  const item = (id: string | null) => (id ? project!.items.find(i => i.id === id) ?? null : null);
  const selItem = () => item(sel);
  const videoTracks = () => project!.tracks.filter(t => t.type === 'video');
  const totalSec = () => Math.max(end() + 30, 90);

  /** Missing clips keep their timeline length, so the layout survives. */
  const sourceDur = (it: EdItem) => {
    const c = clipOf(it);
    return c && c.duration ? c.duration : (it.offset || 0) + it.dur;
  };

  function defaultProject(): EdProject {
    return {
      version: 1,
      tracks: [
        {id: 'T1', type: 'text', label: 'T1'},
        {id: 'V2', type: 'video', label: 'V2'},
        {id: 'V1', type: 'video', label: 'V1'},
        {id: 'A1', type: 'audio', label: 'A1'},
      ],
      items: [],
    };
  }

  // ── notification ────────────────────────────────────────────────
  function snapshot(): EdSnapshot {
    const it = selItem();
    const canSplit = Boolean(
      it && playhead > it.start + 0.2 && playhead < it.start + it.dur - 0.2,
    );
    return {
      ready: Boolean(project),
      tab,
      query,
      playing,
      playhead,
      duration: end(),
      pps,
      selected: it,
      canSplit,
      canDetach: Boolean(it && it.kind === 'clip' && !it.muted && clipOf(it)?.audio_tracks?.length),
      canDuplicate: Boolean(it),
      canDelete: Boolean(it),
      canUndo: undoStack.length > 0,
      canRedo: redoStack.length > 0,
      preparing: preparingNow,
      empty: stageEmpty,
    };
  }

  function emit() {
    const s = snapshot();
    listeners.forEach(fn => fn(s));
  }

  // ── persistence ─────────────────────────────────────────────────
  async function load() {
    try {
      const r = await fetch('/api/editor/project');
      const d = (await r.json()) as {project?: EdProject; missing?: string[]};
      project =
        d.project && Array.isArray(d.project.tracks) && d.project.tracks.length
          ? d.project
          : defaultProject();
      missing = new Set(d.missing || []);
    } catch (err) {
      console.debug('Loading the editor project failed', err);
      project = project || defaultProject();
    }
    loaded = true;
    emit();
  }

  function scheduleSave() {
    dirty = true;
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => void saveNow(), 800);
  }

  async function saveNow() {
    if (!dirty || !project) return;
    dirty = false;
    try {
      await fetch('/api/editor/project', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(project),
      });
    } catch (err) {
      console.debug('Autosaving the editor project failed', err);
      dirty = true;
    }
  }

  function refreshMissing() {
    if (!project) return;
    const have = new Set(clips().map(c => c.slug));
    missing = new Set(
      project.items.filter(i => i.clipId && !have.has(i.clipId)).map(i => i.clipId!),
    );
  }

  // ── undo and redo ───────────────────────────────────────────────
  function begin() {
    undoStack.push(JSON.stringify(project));
    if (undoStack.length > ED_UNDO_CAP) undoStack.shift();
    redoStack = [];
  }

  function undo() {
    if (!undoStack.length) return;
    redoStack.push(JSON.stringify(project));
    project = JSON.parse(undoStack.pop()!);
    afterRestore();
  }

  function redo() {
    if (!redoStack.length) return;
    undoStack.push(JSON.stringify(project));
    project = JSON.parse(redoStack.pop()!);
    afterRestore();
  }

  function afterRestore() {
    if (sel && !item(sel)) sel = null;
    refreshMissing();
    scheduleSave();
    renderTimeline();
    renderPreviewFrame(true);
  }

  /** A transition can never outrun the clip it belongs to. */
  function pruneTransitions() {
    project!.items.forEach(it => {
      if (it.trans && it.kind === 'clip') {
        const cap = Math.min(3, round(it.dur * 0.8));
        it.trans.len = Math.min(Math.max(0.2, it.trans.len), cap);
      }
    });
  }

  function commit() {
    pruneTransitions();
    scheduleSave();
    renderTimeline();
    renderPreviewFrame(true);
  }

  // ── item operations ─────────────────────────────────────────────

  /** Push an item into the nearest gap so lane-mates never overlap. */
  function resolve(it: EdItem) {
    const mates = project!.items
      .filter(o => o.trackId === it.trackId && o.id !== it.id)
      .sort((a, b) => a.start - b.start);
    let s = Math.max(0, it.start);
    for (let pass = 0; pass < mates.length + 1; pass++) {
      const hit = mates.find(o => s < o.start + o.dur && s + it.dur > o.start);
      if (!hit) break;
      const before = hit.start - it.dur;
      const after = hit.start + hit.dur;
      s = before >= 0 && Math.abs(before - s) <= Math.abs(after - s) ? before : after;
    }
    it.start = round(Math.max(0, s));
    return it;
  }

  function insert(it: EdItem) {
    project!.items.push(resolve(it));
    return it;
  }

  function snapTime(t: number, exclId?: string) {
    const pts = [0, playhead];
    project!.items.forEach(i => {
      if (i.id !== exclId) pts.push(i.start, i.start + i.dur);
    });
    let best = t;
    let bd = 8 / pps;
    pts.forEach(p => {
      const d = Math.abs(p - t);
      if (d < bd) {
        bd = d;
        best = p;
      }
    });
    return Math.max(0, round(best));
  }

  function limits(it: EdItem) {
    let prevEnd = 0;
    let nextStart = Infinity;
    project!.items.forEach(o => {
      if (o.trackId !== it.trackId || o.id === it.id) return;
      const e = o.start + o.dur;
      if (e <= it.start + 0.001 && e > prevEnd) prevEnd = e;
      if (o.start >= it.start + it.dur - 0.001 && o.start < nextStart) nextStart = o.start;
    });
    return {prevEnd, nextStart};
  }

  function addClip(slug: string, trackId: string, t: number, asAudio: boolean) {
    if (!project) return;
    const c = clips().find(x => x.slug === slug);
    if (!c || !c.duration) {
      deps.notify(translate('editor.noReadableDuration'), 'error');
      return;
    }
    begin();
    const it = insert({
      id: uid(),
      kind: asAudio ? 'audio' : 'clip',
      trackId,
      clipId: slug,
      start: round(Math.max(0, t)),
      dur: round(c.duration),
      offset: 0,
    });
    sel = it.id;
    commit();
  }

  function quickAdd(slug: string) {
    if (!project) return;
    const vts = videoTracks();
    const vt = vts[vts.length - 1];
    const at = items(vt.id).reduce((m, i) => Math.max(m, i.start + i.dur), 0);
    addClip(slug, vt.id, at, false);
  }

  function addText(presetId: string, opts: {t?: number; x?: number; y?: number} = {}) {
    if (!project) return;
    const p = ED_TEXT_PRESETS.find(x => x.id === presetId);
    const tt = project.tracks.find(t => t.type === 'text');
    if (!p || !tt) return;
    begin();
    const it = insert({
      id: uid(),
      kind: 'text',
      trackId: tt.id,
      start: round(Math.max(0, opts.t !== undefined ? opts.t : playhead)),
      dur: 4,
      text: translate(`editor.textPreset.${p.id}.sample`),
      font: p.font,
      size: p.size,
      weight: p.weight,
      color: p.color,
      x: opts.x !== undefined ? round(opts.x) : p.x,
      y: opts.y !== undefined ? round(opts.y) : p.y,
    });
    sel = it.id;
    commit();
  }

  function applyFx(itemId: string, fxId: string) {
    const it = item(itemId);
    const fx = edFx(fxId);
    if (!it || !fx || it.kind !== 'clip') return;
    begin();
    it.trans = {fx: fxId, len: Math.min(fx.len, round(it.dur * 0.8))};
    sel = itemId;
    commit();
  }

  function split() {
    const it = selItem();
    if (!it || playhead <= it.start + 0.2 || playhead >= it.start + it.dur - 0.2) return;
    begin();
    const t = playhead;
    const right: EdItem = {
      ...it,
      id: uid(),
      start: round(t),
      dur: round(it.start + it.dur - t),
      offset: round((it.offset || 0) + (t - it.start)),
    };
    delete right.trans;
    it.dur = round(t - it.start);
    project!.items.push(right);
    commit();
  }

  function detachAudio() {
    const it = selItem();
    if (!it || it.kind !== 'clip' || it.muted) return;
    const streams = clipOf(it)?.audio_tracks;
    if (!streams?.length) return;
    begin();
    it.muted = true;
    let first: EdItem | null = null;
    for (const stream of streams) {
      // Keep detached streams aligned even when an existing lane is occupied.
      let lane = project!.tracks.find(t => t.type === 'audio' && !project!.items.some(
        other => other.trackId === t.id && other.start < it.start + it.dur &&
          other.start + other.dur > it.start));
      if (!lane) {
        lane = {id: uid(), type: 'audio', label: `A${project!.tracks.filter(t => t.type === 'audio').length + 1}`};
        project!.tracks.push(lane);
      }
      const audio = insert({id: uid(), kind: 'audio', trackId: lane.id,
        clipId: it.clipId, start: it.start, dur: it.dur, offset: it.offset || 0,
        audioStream: stream.index, volume: 1, muted: stream.index !== 0});
      first ??= audio;
    }
    sel = first!.id;
    commit();
  }

  function duplicate() {
    const it = selItem();
    if (!it) return;
    begin();
    const copy: EdItem = {...it, id: uid(), start: round(it.start + it.dur + 0.25)};
    delete copy.trans;
    insert(copy);
    sel = copy.id;
    commit();
  }

  function copySel() {
    const it = selItem();
    if (it) clipboard = JSON.parse(JSON.stringify(it));
  }

  function paste() {
    if (!clipboard || !project!.tracks.find(t => t.id === clipboard!.trackId)) return;
    begin();
    const copy: EdItem = {...clipboard, id: uid(), start: round(playhead)};
    delete copy.trans;
    insert(copy);
    sel = copy.id;
    commit();
  }

  function remove() {
    if (!sel) return;
    begin();
    project!.items = project!.items.filter(i => i.id !== sel);
    sel = null;
    commit();
  }

  function addTrack(type: 'video' | 'audio') {
    begin();
    const n = project!.tracks.filter(t => t.type === type).length + 1;
    const nt: EdTrack = {id: uid(), type, label: (type === 'video' ? 'V' : 'A') + n};
    if (type === 'audio') {
      project!.tracks.push(nt);
    } else {
      const idx = project!.tracks.findIndex(t => t.type === 'video');
      project!.tracks.splice(idx === -1 ? project!.tracks.length : idx, 0, nt);
    }
    commit();
  }

  function removeTrack(id: string) {
    const tr = project!.tracks.find(t => t.id === id);
    if (!tr || tr.type === 'text') return;
    if (tr.type === 'video' && videoTracks().length <= 1) return;
    begin();
    project!.tracks = project!.tracks.filter(t => t.id !== id);
    project!.items = project!.items.filter(i => i.trackId !== id);
    sel = null;
    commit();
  }

  function reset() {
    begin();
    project = defaultProject();
    sel = null;
    playhead = 0;
    setPlaying(false);
    missing = new Set();
    commit();
  }

  function select(id: string | null) {
    sel = id;
    renderTimelineSelection();
    renderTexts(playhead);
    emit();
  }

  // ── stage sizing ────────────────────────────────────────────────
  function initStage() {
    if (!box || stageObserver) return;
    const size = () => {
      const r = box!.stageWrap.getBoundingClientRect();
      if (r.width < 10 || r.height < 10) return;
      const w = Math.min(r.width, (r.height * 16) / 9);
      box!.stage.style.width = `${w}px`;
      box!.stage.style.height = `${(w * 9) / 16}px`;
      positionTexts();
    };
    stageObserver = new ResizeObserver(size);
    stageObserver.observe(box.stageWrap);
    size();
  }

  // ── video pool ──────────────────────────────────────────────────

  /** Detaching does not free what was decoded: the source has to go first. */
  function dropVideo(v: PoolVideo) {
    v.pause();
    v.removeAttribute('src');
    v._key = null;
    try {
      v.load();
    } catch (err) {
      console.debug('Tearing down a preview element failed', err);
    }
    v.remove();
  }

  /**
   * Three elements per track is reasonable while the editor is on screen and
   * wasteful for the rest of the session once it is not.
   */
  function releasePool() {
    Object.keys(pool).forEach(tid => {
      pool[tid].els.forEach(v => dropVideo(v as PoolVideo));
      delete pool[tid];
    });
    poolKey = '';
    master = null;
    Object.keys(audioPool).forEach(id => {
      const a = audioPool[id];
      a.pause();
      a.removeAttribute('src');
      try {
        a.load();
      } catch (err) {
        console.debug('Tearing down a preview audio element failed', err);
      }
      delete audioPool[id];
    });
  }

  function syncPool() {
    if (!box) return;
    const vts = videoTracks();
    const key = vts.map(t => t.id).join(',');
    if (key === poolKey) return;
    poolKey = key;

    Object.keys(pool).forEach(tid => {
      if (!vts.find(t => t.id === tid)) {
        pool[tid].els.forEach(v => dropVideo(v as PoolVideo));
        delete pool[tid];
      }
    });

    // Three, not two. With two, the outgoing clip and the preloaded one fought
    // over the same element and reassigned its src every frame, which is what
    // froze playback through a run of transitions.
    vts.forEach((t, idx) => {
      if (!pool[t.id]) {
        const make = () => {
          const v = document.createElement('video') as PoolVideo;
          v.playsInline = true;
          v.preload = 'auto';
          v.className = 'ed-vhide';
          v._key = null;
          v._need = 0;
          box!.stage.insertBefore(v, box!.stage.firstChild);
          return v;
        };
        pool[t.id] = {
          els: [make(), make(), make()],
          itemKey: null,
          shown: null,
          handover: false,
          seq: 0,
          zBase: 0,
        };
      }
      // Tracks are listed top first. Each owns a band of four so its roles can
      // stack, and every video stays below the overlays and the titles.
      pool[t.id].zBase = 4 * (vts.length - idx);
    });
  }

  const activeItemOn = (trackId: string, t: number) =>
    items(trackId).find(
      i => i.kind === 'clip' && t >= i.start && t < i.start + i.dur && !missing.has(i.clipId!),
    ) || null;

  const nextItemOn = (trackId: string, t: number) =>
    items(trackId).find(i => i.kind === 'clip' && i.start > t && !missing.has(i.clipId!)) || null;

  const itemKey = (it: EdItem) => {
    const clip = clipOf(it);
    return it.id + '|' + (clip ? clip.video_url : it.clipId);
  };

  const prevItemOn = (trackId: string, it: EdItem) =>
    items(trackId)
      .filter(
        i =>
          i.kind === 'clip' &&
          i.id !== it.id &&
          !missing.has(i.clipId!) &&
          Math.abs(i.start + i.dur - it.start) < 0.11,
      )
      .pop() || null;

  /** What this track needs decoded right now, most important first. */
  function needed(trackId: string, t: number): Need[] {
    const need: Need[] = [];
    const push = (it: EdItem | null, role: string, parkAt: number) => {
      if (!it || need.some(n => n.key === itemKey(it))) return;
      if (!clipOf(it)) return;
      need.push({key: itemKey(it), it, role, parkAt});
    };
    const cur = activeItemOn(trackId, t);
    push(cur, 'cur', (cur ? cur.offset || 0 : 0) + (cur ? Math.max(0, t - cur.start) : 0));
    if (cur && cur.trans && t < cur.start + cur.trans.len) {
      const prev = prevItemOn(trackId, cur);
      if (prev) push(prev, 'prev', (prev.offset || 0) + prev.dur);
    }
    const next = nextItemOn(trackId, t);
    if (next && next.start - t < ED_PRELOAD) push(next, 'next', next.offset || 0);
    return need;
  }

  /**
   * Bind elements to the needed items. An element already holding the right
   * source keeps it, so a run of transitions never reassigns a src it already
   * has. This is the only place src is written.
   */
  function allocate(entry: PoolEntry, need: Need[]) {
    entry.seq++;
    const taken = new Set<HTMLVideoElement>();
    need.forEach(n => {
      const hit = entry.els.find(v => (v as PoolVideo)._key === n.key && !taken.has(v));
      if (hit) {
        n.el = hit as PoolVideo;
        taken.add(hit);
      }
    });
    need.forEach(n => {
      if (n.el) return;
      const free = entry.els.filter(v => !taken.has(v)) as PoolVideo[];
      const el = free.find(v => !v._key) || free.sort((a, b) => a._need - b._need)[0];
      if (!el) return;
      el._key = n.key;
      el.src = playbackUrl(clipOf(n.it));
      warmStart(el, n.parkAt);
      n.el = el;
      taken.add(el);
    });
    need.forEach(n => {
      if (n.el) {
        n.el._role = n.role;
        n.el._need = entry.seq;
      }
    });
    entry.els.forEach(v => {
      const pv = v as PoolVideo;
      if (taken.has(v)) return;
      pv._role = null;
      hideVideo(pv);
      if (!pv.paused && !warming(pv)) pv.pause();
      settle(pv);
    });
    return need;
  }

  /**
   * Seek only when the element is not already mid-seek: re-issuing a seek on
   * every frame while the decoder catches up is what made first plays stutter.
   * While playing the clock follows the video, so the tolerance is wide and
   * only a real desync triggers a correction.
   */
  function seekVideo(v: HTMLMediaElement, t: number, tol?: number) {
    if (v.readyState < 1 || v.seeking) return;
    if (Math.abs(v.currentTime - t) > (tol || ED_DRIFT)) {
      try {
        v.currentTime = t;
      } catch (err) {
        console.debug('Seeking a preview element failed', err);
      }
    }
  }

  const playTol = () => (playing ? 0.45 : ED_DRIFT);

  /**
   * A fresh src decodes its first frames noticeably late, so every newly
   * assigned element gets a short muted play window and then parks back on its
   * in-point with the decoder already rolling.
   */
  function warmStart(v: PoolVideo, parkAt: number) {
    v._parkAt = parkAt || 0;
    v._warmUntil = performance.now() + ED_WARM_MS;
    v.muted = true;
    void v.play().catch(() => {});
  }

  const warming = (v: PoolVideo) => Boolean(v._warmUntil && performance.now() < v._warmUntil);

  /** A warmed-but-idle element has run past its in-point; park it back. */
  function settle(v: PoolVideo) {
    if (!v._warmUntil || performance.now() < v._warmUntil) return;
    v._warmUntil = 0;
    if (!v.paused) v.pause();
    seekVideo(v, v._parkAt || 0);
  }

  function showVideo(v: HTMLVideoElement, z: number) {
    v.classList.remove('ed-vhide');
    v.style.zIndex = String(z);
  }

  function hideVideo(v: HTMLVideoElement) {
    v.classList.add('ed-vhide');
    v.style.opacity = '';
  }

  function syncTrack(trackId: string, t: number, isMaster: boolean): TrackState | null {
    const entry = pool[trackId];
    if (!entry) return null;
    const need = allocate(entry, needed(trackId, t));
    const curN = need.find(n => n.role === 'cur');
    const prevN = need.find(n => n.role === 'prev');

    if (!curN || !curN.el) {
      entry.els.forEach(hideVideo);
      entry.itemKey = null;
      entry.shown = null;
      return null;
    }

    const it = curN.it;
    const cur = curN.el;
    const prev = prevN ? prevN.el ?? null : null;
    const fresh = entry.itemKey !== curN.key;
    // A track playing under the master is only loosely in sync, so it needs
    // one correction at the moment it becomes the master itself.
    const promoted = isMaster && !entry.wasMaster;
    entry.wasMaster = isMaster;
    if (fresh) {
      entry.itemKey = curN.key;
      // Hold the previous frame until the new element can paint, so a cut
      // never flashes black while the decoder spins up.
      entry.handover = Boolean(entry.shown && entry.shown !== cur);
    }
    if (entry.handover && cur.readyState >= 2 && !cur.seeking) entry.handover = false;

    const inTrans = Boolean(it.trans && t < it.start + it.trans.len);
    entry.els.forEach(v => {
      if (v === cur) showVideo(v, entry.zBase + 2);
      else if (v === prev && inTrans) showVideo(v, entry.zBase + 1);
      else if (v === entry.shown && entry.handover) showVideo(v, entry.zBase + 1);
      else hideVideo(v);
    });

    const desired = (it.offset || 0) + (t - it.start);
    if (playing && !audioWaiting) {
      cur._warmUntil = 0;
      cur.muted = Boolean(it.muted);
      if (cur.paused) void cur.play().catch(() => {});
      // The clock reads its time from the master element, so correcting that
      // element is chasing its own tail. Only a fresh cut, or a track playing
      // underneath the master, needs a correction.
      if (fresh || promoted) seekVideo(cur, desired, ED_DRIFT);
      else if (!isMaster) seekVideo(cur, desired, playTol());
    } else if (warming(cur)) {
      cur._parkAt = desired;
    } else {
      if (!cur.paused) cur.pause();
      cur.muted = Boolean(it.muted);
      seekVideo(cur, desired);
    }

    if (!entry.handover) entry.shown = cur;
    return {it, cur, prev};
  }

  // ── transition approximation ────────────────────────────────────
  function applyTransitionStyles(states: Array<TrackState | null>, t: number) {
    if (!box) return;
    let fadeOpacity = 0;
    let fadeColor = '#02040a';

    states.forEach(st => {
      if (!st) return;
      const {it, cur, prev} = st;
      cur.style.opacity = '';
      cur.style.filter = '';
      cur.style.transform = '';
      if (prev) {
        prev.style.transform = '';
        prev.style.filter = '';
      }
      if (!it.trans) return;
      const len = it.trans.len;
      if (t < it.start || t >= it.start + len) return;
      const p = Math.max(0, Math.min(1, (t - it.start) / len));
      const fx = it.trans.fx;
      if (fx === 'crossfade' || fx === 'blurdis' || fx === 'slide') {
        if (fx === 'crossfade') {
          cur.style.opacity = String(p);
        } else if (fx === 'blurdis') {
          // Matches xfade hblur: both sides defocus, peaking at the midpoint,
          // while they cross over. Scaled to the stage so it reads the same at
          // any preview size.
          cur.style.opacity = String(p);
          const peak = Math.sin(p * Math.PI) * Math.max(6, (box!.stage.clientWidth || 640) * 0.022);
          cur.style.filter = `blur(${peak.toFixed(1)}px)`;
          if (prev) prev.style.filter = `blur(${peak.toFixed(1)}px)`;
        } else if (reducedMotion()) {
          cur.style.opacity = String(p);
        } else {
          // Matches xfade slideleft: incoming enters from the right while the
          // outgoing is pushed off to the left.
          cur.style.transform = `translateX(${((1 - p) * 100).toFixed(2)}%)`;
          if (prev) prev.style.transform = `translateX(${(-p * 100).toFixed(2)}%)`;
        }
      } else {
        const color =
          fx === 'fadewhite' ? '#f2f5fa' : fx === 'dipaccent' ? deps.accent() : '#02040a';
        fadeOpacity = Math.max(fadeOpacity, 1 - p);
        fadeColor = color;
      }
    });

    box.fadeOverlay.style.background = fadeColor;
    box.fadeOverlay.style.opacity = String(fadeOpacity);
  }

  /** Park the outgoing clip on its tail so a transition blends real frames. */
  function syncOutgoing(st: TrackState | null, t: number) {
    if (!st || !st.prev || !st.it.trans) return;
    const {it, prev: el} = st;
    if (t >= it.start + it.trans!.len) return;
    const prev = prevItemOn(it.trackId, it);
    if (!prev) return;
    el.muted = true;
    const pd = Math.min(
      (prev.offset || 0) + prev.dur + (t - it.start),
      sourceDur(prev) - 0.05,
    );
    seekVideo(el, pd, playTol());
    if (playing && !audioWaiting && el.paused) void el.play().catch(() => {});
    else if ((!playing || audioWaiting) && !el.paused) el.pause();
  }

  // ── audio items ─────────────────────────────────────────────────
  function syncAudio(t: number) {
    const active = new Set<string>();
    const audible: HTMLAudioElement[] = [];
    project!.items.forEach(it => {
      if (it.kind !== 'audio' || it.muted || (it.volume ?? 1) === 0) return;
      if (t < it.start || t >= it.start + it.dur || missing.has(it.clipId!)) return;
      const clip = clipOf(it);
      if (!clip) return;
      active.add(it.id);
      let a = audioPool[it.id];
      if (!a) {
        a = new Audio();
        a.preload = 'auto';
        a.addEventListener('loadeddata', () => {
          if (mounted && !playing) renderPreviewFrame(false);
        });
        a.addEventListener('error', () => {
          if (!a.getAttribute('src') || !mounted) return;
          setPlaying(false);
          deps.notify(translate('editor.audioPreviewFailed'), 'error');
        });
        audioPool[it.id] = a;
      }
      const revision = new URL(clip.video_url, window.location.href).search;
      const url = `/api/clips/${encodeURIComponent(clip.slug)}/audio/${it.audioStream ?? 0}${revision}`;
      if (a.getAttribute('src') !== url) a.src = url;
      a.volume = Math.max(0, Math.min(1, it.volume ?? 1));
      seekVideo(a, (it.offset || 0) + (t - it.start), playTol());
      audible.push(a);
    });
    audioWaiting = audible.some(a => a.readyState < 2 && !a.error);
    audible.forEach(a => {
      if (playing && !audioWaiting && a.paused) void a.play().catch(() => {});
      else if ((!playing || audioWaiting) && !a.paused) a.pause();
    });
    Object.keys(audioPool).forEach(id => {
      if (!active.has(id)) {
        audioPool[id].pause();
        if (!item(id)) {
          audioPool[id].removeAttribute('src');
          audioPool[id].load();
          delete audioPool[id];
        }
      }
    });
  }

  // ── text overlays ───────────────────────────────────────────────
  const visibleTexts = (t: number) =>
    project!.items.filter(i => i.kind === 'text' && t >= i.start && t < i.start + i.dur);

  function renderTexts(t: number) {
    if (!box || !project) return;
    const texts = visibleTexts(t);
    const key = texts.map(i => i.id).join(',') + '|' + sel;
    if (key === lastTextKey) {
      positionTexts();
      return;
    }
    lastTextKey = key;

    box.stage.querySelectorAll('.ed-text-overlay').forEach(el => el.remove());
    texts.forEach(it => {
      const el = document.createElement('div');
      el.className = 'ed-text-overlay' + (sel === it.id ? ' selected' : '');
      el.dataset.text = it.id;
      const inner = document.createElement('span');
      inner.textContent = it.text || '';
      el.appendChild(inner);
      if (sel === it.id) {
        ['tl', 'tr', 'bl', 'br'].forEach(corner => {
          const h = document.createElement('span');
          h.className = 'ed-th ' + corner;
          h.addEventListener('pointerdown', e => textResize(e, el, it.id));
          el.appendChild(h);
        });
      }
      box!.stage.appendChild(el);
      el.addEventListener('pointerdown', e => textDrag(e, el, it.id));
    });
    positionTexts();
  }

  /** Corner handles scale with the pointer's distance from the centre. */
  function textResize(e: PointerEvent, el: HTMLElement, id: string) {
    e.stopPropagation();
    e.preventDefault();
    const it = item(id);
    if (!it) return;
    begin();
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const d0 = Math.max(12, Math.hypot(e.clientX - cx, e.clientY - cy));
    const s0 = it.size!;
    const h = e.currentTarget as HTMLElement;
    capture(h, e);
    const mv = (ev: PointerEvent) => {
      const d = Math.hypot(ev.clientX - cx, ev.clientY - cy);
      it.size = Math.round(Math.min(1000, Math.max(8, (s0 * d) / d0)));
      positionTexts();
      emit();
    };
    const up = () => {
      h.removeEventListener('pointermove', mv);
      h.removeEventListener('pointerup', up);
      scheduleSave();
      emit();
    };
    h.addEventListener('pointermove', mv);
    h.addEventListener('pointerup', up);
  }

  function positionTexts() {
    if (!box) return;
    const w = box.stage.clientWidth || 1;
    box.stage.querySelectorAll<HTMLElement>('.ed-text-overlay').forEach(el => {
      const it = item(el.dataset.text ?? null);
      if (!it) return;
      el.style.left = `${it.x}%`;
      el.style.top = `${it.y}%`;
      el.style.fontFamily = ED_FONTS[it.font!]?.stack ?? ED_FONTS.display.stack;
      el.style.fontWeight = String(it.weight);
      el.style.fontSize = `${(it.size! * w) / 1920}px`;
      el.style.color = it.color!;
      el.style.letterSpacing = it.font === 'display' ? '-.02em' : '0';
    });
  }

  function textDrag(e: PointerEvent, el: HTMLElement, id: string) {
    e.stopPropagation();
    e.preventDefault();
    select(id);
    const it = item(id);
    if (!it || !box) return;
    begin();
    const stage = box.stage.getBoundingClientRect();
    const ox = it.x!;
    const oy = it.y!;
    const sx = e.clientX;
    const sy = e.clientY;
    capture(el, e);
    const mv = (ev: PointerEvent) => {
      it.x = round(Math.min(97, Math.max(3, ox + ((ev.clientX - sx) / stage.width) * 100)));
      it.y = round(Math.min(95, Math.max(4, oy + ((ev.clientY - sy) / stage.height) * 100)));
      positionTexts();
      emit();
    };
    const up = () => {
      el.removeEventListener('pointermove', mv);
      el.removeEventListener('pointerup', up);
      scheduleSave();
      renderTimeline();
    };
    el.addEventListener('pointermove', mv);
    el.addEventListener('pointerup', up);
  }

  function inspectorChange(field: string, value: string | number | boolean) {
    const it = selItem();
    if (!it) return;
    if (it.kind === 'audio' && ['audioStream', 'volume', 'muted'].includes(field)) {
      begin();
      if (field === 'muted') it.muted = Boolean(value);
      else if (field === 'audioStream') it.audioStream = Number(value);
      else it.volume = Math.max(0, Math.min(1, Number(value)));
      commit();
      return;
    }
    if (it.kind !== 'text') return;
    begin();
    (it as unknown as Record<string, unknown>)[field] =
      field === 'size' ? parseInt(String(value), 10) : value;
    scheduleSave();
    lastTextKey = '';
    renderTexts(playhead);
    if (field === 'text') renderTimeline();
    emit();
  }

  // ── clock ───────────────────────────────────────────────────────
  function setPlaying(next: boolean) {
    if (next && end() <= 0) return;
    if (next && playhead >= end() - 0.01) playhead = 0;
    playing = next;
    emit();
    if (!next) {
      renderPreviewFrame(false);
      return;
    }
    if (raf) return;

    // Let the decoders spin up before the clock starts, otherwise the first
    // half second runs ahead of the picture and looks like a stutter.
    renderPreviewFrame(true);
    const start = () => {
      if (!playing || raf) return;
      lastTick = performance.now();
      raf = requestAnimationFrame(tick);
    };
    const lead = master && master.cur;
    if (lead && lead.readyState < 3) {
      let fired = false;
      const go = () => {
        if (fired) return;
        fired = true;
        start();
      };
      lead.addEventListener('playing', go, {once: true});
      lead.addEventListener('canplaythrough', go, {once: true});
      window.setTimeout(go, 400);
    } else {
      start();
    }
  }

  function tick(now: number) {
    raf = null;
    if (!playing || !mounted) {
      playing = false;
      renderPreviewFrame(false);
      emit();
      return;
    }
    if (audioWaiting) {
      lastTick = now;
      renderPreviewFrame(false);
      if (audioWaiting) {
        raf = requestAnimationFrame(tick);
        return;
      }
    }
    const total = end();
    const dt = Math.min(0.25, (now - lastTick) / 1000);
    lastTick = now;
    playhead = Math.min(total, playhead + dt);
    renderPreviewFrame(false);

    // The video, not the wall clock, is the timebase while a clip is on
    // screen: chasing wall time forced a corrective seek every few frames,
    // which is what stuttered. Wall time only carries across gaps and cuts.
    const m = master;
    if (m && !m.cur.paused && !m.cur.seeking && m.cur.readyState >= 2) {
      const vt = m.it.start + (m.cur.currentTime - (m.it.offset || 0));
      if (
        vt >= m.it.start &&
        vt <= m.it.start + m.it.dur &&
        Math.abs(vt - playhead) < 0.5
      ) {
        playhead = Math.min(total, vt);
      }
    }

    updatePlayheadDom();
    emit();
    if (playhead >= total) {
      setPlaying(false);
      return;
    }
    if (playing) raf = requestAnimationFrame(tick);
  }

  function renderPreviewFrame(structural: boolean) {
    if (!project || !mounted || !box) return;
    initStage();
    syncPool();
    const t = playhead;
    syncAudio(t);
    const vts = videoTracks();
    // Tracks are listed top first, so the first active one is what the viewer
    // is actually looking at. The clock has to follow that, not a lower track
    // hidden underneath it.
    let m: TrackState | null = null;
    const states = vts.map(tr => {
      const st = syncTrack(tr.id, t, !m);
      if (st && !m) m = st;
      return st;
    });
    master = m;
    states.forEach(st => syncOutgoing(st, t));
    applyTransitionStyles(states, t);
    renderTexts(t);

    const anyVideo = states.some(st => st);
    const nextEmpty = !anyVideo;
    const nextPreparing = audioWaiting || states.some(st => {
      if (!st) return false;
      const c = clipOf(st.it);
      return Boolean(c && clipNeedsProxy(c) && st.cur.readyState < 2);
    });
    if (nextEmpty !== stageEmpty || nextPreparing !== preparingNow) {
      stageEmpty = nextEmpty;
      preparingNow = nextPreparing;
      emit();
    }

    if (structural) updatePlayheadDom();
  }

  // ── timeline rendering ──────────────────────────────────────────
  function zoom(factor: number) {
    pps = Math.max(ED_PPS_MIN, Math.min(ED_PPS_MAX, pps * factor));
    renderTimeline();
  }

  function fit() {
    const w = box ? box.timelineScroll.clientWidth - ED_RAIL - 20 : 900;
    pps = Math.max(ED_PPS_MIN, Math.min(ED_PPS_MAX, w / Math.max(10, end() + 2)));
    renderTimeline();
  }

  function seek(t: number) {
    playhead = Math.max(0, Math.min(totalSec(), t));
    updatePlayheadDom();
    renderPreviewFrame(true);
    emit();
  }

  function updatePlayheadDom() {
    if (!box) return;
    const ph = box.timelineCanvas.querySelector<HTMLElement>('#ed-playhead');
    const cur = box.timelineCanvas.querySelector<HTMLElement>('#ed-ruler-cursor');
    if (ph) ph.style.left = `${ED_RAIL + playhead * pps - 0.75}px`;
    if (cur) cur.style.left = `${playhead * pps - 4.5}px`;
  }

  function waveBars(seed: number, n: number) {
    const out: number[] = [];
    let s = seed * 9301 + 49297;
    for (let i = 0; i < n; i++) {
      s = (s * 9301 + 49297) % 233280;
      out.push(0.18 + 0.82 * (s / 233280));
    }
    return out;
  }

  function waveSeed(slug: string) {
    let h = 0;
    for (let i = 0; i < (slug || '').length; i++) h = (h * 31 + slug.charCodeAt(i)) % 9973;
    return h + 3;
  }

  function itemHTML(it: EdItem) {
    const w = Math.max(8, it.dur * pps);
    const left = it.start * pps;
    const selCls = sel === it.id ? ' selected' : '';
    const miss = it.clipId && missing.has(it.clipId) ? ' missing' : '';
    const base = `data-item="${escAttr(it.id)}" style="left:${left}px;width:${w}px"`;
    const c = it.clipId ? clipOf(it) : null;
    const name = c ? clipTitle(c) : it.clipId || '';

    if (it.kind === 'audio') {
      const stream = c?.audio_tracks?.find(track => track.index === (it.audioStream ?? 0));
      const title = stream?.title || translate('editor.recordedTrack', {number: (it.audioStream ?? 0) + 1});
      const bars = waveBars(waveSeed(it.clipId || ''), Math.max(16, Math.round(it.dur * 3)));
      const rects = bars
        .map(
          (b, i) =>
            `<rect x="${i * 3}" y="${(10 - b * 8).toFixed(2)}" width="2" height="${(b * 16).toFixed(2)}" rx="1" fill="currentColor"/>`,
        )
        .join('');
      return `<div class="ed-item ed-item-audio${selCls}${miss}" data-muted="${Boolean(it.muted)}" ${base}>
      <svg class="ed-wave" viewBox="0 0 ${bars.length * 3} 20" preserveAspectRatio="none">${rects}</svg>
      <span class="ed-item-name">${escHtml(name)} · ${escHtml(title)}</span>
      <div class="ed-handle l" data-handle="l"><div class="ed-handle-bar"></div></div>
      <div class="ed-handle r" data-handle="r"><div class="ed-handle-bar"></div></div>
    </div>`;
    }
    if (it.kind === 'text') {
      return `<div class="ed-item ed-item-text${selCls}" ${base}>
      <span class="ed-item-type-ic">${edGlyph(ED_ICONS.type, 10)}</span>
      <span class="ed-item-name">${escHtml(it.text || '')}</span>
      <div class="ed-handle l" data-handle="l"><div class="ed-handle-bar"></div></div>
      <div class="ed-handle r" data-handle="r"><div class="ed-handle-bar"></div></div>
    </div>`;
    }
    // Lazy like the library list: a long timeline scrolls well past its own
    // width, and a thumbnail costs about 900 KB once decoded.
    const thumb =
      c && c.thumb_url
        ? `<img src="${escAttr(c.thumb_url)}" loading="lazy" alt="" draggable="false">`
        : '';
    return `<div class="ed-item ed-item-clip${selCls}${miss}" ${base}>
    ${thumb}<div class="ed-item-shade"></div>
    <div class="ed-item-hd">
      <span class="ed-item-name">${escHtml(name)}</span>
      <span class="ed-item-dur">${fmtS(it.dur)}</span>
    </div>
    ${it.muted ? `<span class="ed-item-mute" title="${escAttr(translate('editor.audioDetached'))}">${edGlyph(ED_ICONS.volumeX, 10)}</span>` : ''}
    <div class="ed-handle l" data-handle="l"><div class="ed-handle-bar"></div></div>
    <div class="ed-handle r" data-handle="r"><div class="ed-handle-bar"></div></div>
  </div>`;
  }

  /**
   * Both the marker and its editing pill are rendered up front and toggled by
   * a class. Swapping innerHTML on hover made the element resize under the
   * pointer, which retriggered hover and left the buttons unclickable.
   */
  function junctionHTML(it: EdItem) {
    const fx = edFx(it.trans!.fx)!;
    return `<div class="ed-junction" data-junction="${escAttr(it.id)}" style="left:${it.start * pps}px">
    <div class="ed-junction-mark" title="${escAttr(translate('editor.junctionTitle', {name: fxName(fx.id), len: it.trans!.len.toFixed(1)}))}">
      <div class="ed-junction-stem"></div>
      <div class="ed-junction-dot">${edGlyph(fx.glyph, 8)}</div>
      <div class="ed-junction-stem"></div>
    </div>
    <div class="ed-junction-pill">
      <span class="fx-ic">${edGlyph(fx.glyph, 11)}</span>
      <span class="fx-name">${escHtml(fxName(fx.id))}</span>
      <button class="ed-junction-btn" data-bump="-0.1" title="${escAttr(translate('editor.transShorter'))}">&minus;</button>
      <span class="fx-len">${it.trans!.len.toFixed(1)}s</span>
      <button class="ed-junction-btn" data-bump="0.1" title="${escAttr(translate('editor.transLonger'))}">+</button>
      <button class="ed-junction-btn rm" data-rm="1" title="${escAttr(translate('editor.transRemove'))}">&times;</button>
    </div>
  </div>`;
  }

  function renderTimeline() {
    if (!project || !mounted || !box) return;
    const total = totalSec();
    const totalW = total * pps;
    const minor = pps >= 10 ? 1 : 5;
    const labelEvery = pps >= 10 ? 5 : 15;

    let ticks = '';
    for (let s = 0; s <= total; s += minor) {
      const major = s % labelEvery === 0;
      ticks += `<div class="ed-tick${major ? ' major' : ''}" style="left:${s * pps}px">
      <div class="ed-tick-mark"></div>
      ${major ? `<span class="ed-tick-label">${fmtS(s)}</span>` : ''}
    </div>`;
    }

    const rows = project.tracks
      .map(tr => {
        const laneItems = items(tr.id);
        const rendered = laneItems.map(itemHTML).join('');
        const junctions =
          tr.type === 'video' ? laneItems.filter(i => i.trans).map(junctionHTML).join('') : '';
        return `<div class="ed-track-row">
      <div class="ed-rail ${tr.type}" data-rail="${escAttr(tr.id)}" title="${escAttr(translate('editor.trackOptionsHint'))}">
        <span class="ed-rail-label">${escHtml(tr.label)}</span>
        <span class="ed-rail-type">${tr.type.slice(0, 3).toUpperCase()}</span>
      </div>
      <div class="ed-lane ${tr.type}" data-lane="${escAttr(tr.id)}" style="width:${totalW}px">
        ${rendered}${junctions}
      </div>
    </div>`;
      })
      .join('');

    box.timelineCanvas.innerHTML = `
    <div class="ed-ruler-row">
      <div class="ed-rail-corner"></div>
      <div id="ed-ruler" style="width:${totalW}px">
        ${ticks}
        <div id="ed-ruler-cursor"></div>
      </div>
    </div>
    ${rows}
    <div id="ed-playhead"></div>`;
    box.timelineCanvas.style.width = `${ED_RAIL + totalW}px`;

    wireTimeline();
    updatePlayheadDom();
    emit();
  }

  function renderTimelineSelection() {
    if (!box) return;
    box.timelineCanvas.querySelectorAll<HTMLElement>('.ed-item').forEach(el => {
      el.classList.toggle('selected', el.dataset.item === sel);
    });
  }

  // ── timeline wiring ─────────────────────────────────────────────
  function wireTimeline() {
    if (!box) return;
    const ruler = box.timelineCanvas.querySelector<HTMLElement>('#ed-ruler');
    if (ruler) {
      ruler.addEventListener('pointerdown', e => {
        capture(ruler, e);
        const rect = ruler.getBoundingClientRect();
        const upd = (ev: PointerEvent) => seek((ev.clientX - rect.left) / pps);
        upd(e);
        const up = () => {
          ruler.removeEventListener('pointermove', upd);
          ruler.removeEventListener('pointerup', up);
        };
        ruler.addEventListener('pointermove', upd);
        ruler.addEventListener('pointerup', up);
      });
    }

    box.timelineCanvas.querySelectorAll<HTMLElement>('.ed-rail').forEach(rail => {
      rail.addEventListener('contextmenu', e => openTrackMenu(e, rail.dataset.rail!));
    });

    box.timelineCanvas.querySelectorAll<HTMLElement>('.ed-lane').forEach(lane => {
      const trackId = lane.dataset.lane!;
      lane.addEventListener('pointerdown', e => {
        if (e.target === lane) select(null);
      });
      lane.addEventListener('contextmenu', e => {
        if (e.target === lane) openTrackMenu(e, trackId);
      });
      lane.addEventListener('dragover', e => laneDragOver(e, lane, trackId));
      lane.addEventListener('dragleave', () => {
        if (dropHint && dropHint.trackId === trackId) clearDropHints();
      });
      lane.addEventListener('drop', e => laneDrop(e, lane, trackId));

      lane.querySelectorAll<HTMLElement>('.ed-item').forEach(el => {
        const id = el.dataset.item!;
        el.addEventListener('pointerdown', e => itemPointerDown(e, el, id));
        el.addEventListener('contextmenu', e => openItemMenu(e, id));
        if (el.classList.contains('ed-item-clip')) {
          el.addEventListener('dragover', e => {
            if (drag && drag.kind === 'fx' && !nearJunction(e, el, id)) {
              e.preventDefault();
              e.stopPropagation();
              el.classList.add('fx-target');
            }
          });
          el.addEventListener('dragleave', () => el.classList.remove('fx-target'));
          el.addEventListener('drop', e => {
            if (drag && drag.kind === 'fx' && !nearJunction(e, el, id)) {
              e.preventDefault();
              e.stopPropagation();
              el.classList.remove('fx-target');
              applyFx(id, drag.id);
              dragEnd();
            }
          });
        }
      });

      lane.querySelectorAll<HTMLElement>('.ed-junction').forEach(j =>
        wireJunction(j, j.dataset.junction!),
      );
    });
  }

  /**
   * An fx drop close to the boundary between two abutting clips targets the
   * junction rather than the clip under the pointer.
   */
  function nearJunction(e: DragEvent, el: HTMLElement, itemId: string) {
    const it = item(itemId);
    if (!it) return false;
    const rect = el.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const {prevEnd, nextStart} = limits(it);
    return (
      (px < 16 && Math.abs(prevEnd - it.start) < 0.11 && it.start > 0.05) ||
      (px > rect.width - 16 && Math.abs(nextStart - (it.start + it.dur)) < 0.11)
    );
  }

  function itemPointerDown(e: PointerEvent, el: HTMLElement, id: string) {
    if (e.button !== 0) return;
    const it = item(id);
    if (!it) return;
    e.stopPropagation();
    select(id);

    const handle = (e.target as HTMLElement).closest('[data-handle]') as HTMLElement | null;
    capture(el, e);

    if (handle) {
      trimDrag(e, el, it, handle.dataset.handle!);
      return;
    }

    const sx = e.clientX;
    const sy = e.clientY;
    const os = it.start;
    const tr = project!.tracks.find(t => t.id === it.trackId)!;
    let moved = false;
    let targetTrack = it.trackId;
    let targetStart = it.start;

    const lanes = [...box!.timelineCanvas.querySelectorAll<HTMLElement>('.ed-lane')].filter(l => {
      const lt = project!.tracks.find(t => t.id === l.dataset.lane);
      return lt && lt.type === tr.type;
    });

    const mv = (ev: PointerEvent) => {
      if (Math.abs(ev.clientX - sx) > 3 || Math.abs(ev.clientY - sy) > 8) moved = true;
      if (!moved) return;
      targetStart = snapTime(Math.max(0, os + (ev.clientX - sx) / pps), id);
      for (const l of lanes) {
        const r = l.getBoundingClientRect();
        if (ev.clientY >= r.top - 3 && ev.clientY <= r.bottom + 3) targetTrack = l.dataset.lane!;
      }
      el.style.left = `${targetStart * pps}px`;
      el.style.opacity = targetTrack !== it.trackId ? '0.65' : '';
    };
    const up = () => {
      el.removeEventListener('pointermove', mv);
      el.removeEventListener('pointerup', up);
      if (!moved) return;
      begin();
      it.start = targetStart;
      it.trackId = targetTrack;
      resolve(it);
      commit();
    };
    el.addEventListener('pointermove', mv);
    el.addEventListener('pointerup', up);
  }

  function trimDrag(e: PointerEvent, el: HTMLElement, it: EdItem, side: string) {
    const sx = e.clientX;
    const os = it.start;
    const od = it.dur;
    const oo = it.offset || 0;
    const {prevEnd, nextStart} = limits(it);
    const srcDur = it.clipId ? sourceDur(it) : Infinity;
    const maxD = Math.min(srcDur - oo, nextStart - os);
    let final: Partial<EdItem> | null = null;

    const mv = (ev: PointerEvent) => {
      const dt = (ev.clientX - sx) / pps;
      if (side === 'l') {
        let d = snapTime(os + dt, it.id) - os;
        d = Math.max(Math.max(-oo, prevEnd - os), Math.min(od - 0.5, d));
        final = {start: round(os + d), dur: round(od - d), offset: round(oo + d)};
        el.style.left = `${final.start! * pps}px`;
        el.style.width = `${Math.max(8, final.dur! * pps)}px`;
      } else {
        let nd = snapTime(os + od + dt, it.id) - os;
        nd = Math.max(0.5, Math.min(maxD, nd));
        final = {dur: round(nd)};
        el.style.width = `${Math.max(8, final.dur! * pps)}px`;
      }
    };
    const up = () => {
      el.removeEventListener('pointermove', mv);
      el.removeEventListener('pointerup', up);
      if (!final) return;
      begin();
      Object.assign(it, final);
      commit();
    };
    el.addEventListener('pointermove', mv);
    el.addEventListener('pointerup', up);
  }

  // ── library drops ───────────────────────────────────────────────
  function laneDragOver(e: DragEvent, lane: HTMLElement, trackId: string) {
    if (!drag) return;
    const tr = project!.tracks.find(t => t.id === trackId)!;
    const rect = lane.getBoundingClientRect();
    const px = e.clientX - rect.left;

    if (drag.kind === 'fx') {
      if (tr.type !== 'video') return;
      const j = junctionAt(trackId, px);
      if (j) {
        e.preventDefault();
        setDropHint({trackId, junctionX: j.x});
      } else if (dropHint && dropHint.junctionX !== undefined) {
        clearDropHints();
      }
      return;
    }
    if (drag.kind === 'text') {
      e.preventDefault();
      const tt = project!.tracks.find(x => x.type === 'text')!;
      setDropHint({trackId: tt.id, t: snapTime(px / pps), w: 4 * pps});
      return;
    }
    if (tr.type === 'text') return;
    e.preventDefault();
    const dur = (clips().find(c => c.slug === drag!.id)?.duration ?? 4) as number;
    setDropHint({trackId, t: snapTime(px / pps), w: dur * pps});
  }

  function laneDrop(e: DragEvent, lane: HTMLElement, trackId: string) {
    const d = drag;
    if (!d) return;
    const tr = project!.tracks.find(t => t.id === trackId)!;
    const rect = lane.getBoundingClientRect();
    const px = e.clientX - rect.left;

    if (d.kind === 'fx') {
      if (tr.type !== 'video') return;
      const j = junctionAt(trackId, px);
      if (j) {
        e.preventDefault();
        applyFx(j.id, d.id);
      }
      dragEnd();
      return;
    }
    const t = snapTime(px / pps);
    if (d.kind === 'text') {
      e.preventDefault();
      addText(d.id, {t});
      dragEnd();
      return;
    }
    if (tr.type === 'text') return;
    e.preventDefault();
    addClip(d.id, trackId, t, tr.type === 'audio');
    dragEnd();
  }

  function junctionAt(trackId: string, px: number) {
    const sorted = items(trackId).filter(i => i.kind === 'clip');
    let best: {x: number; id: string} | null = null;
    for (let k = 1; k < sorted.length; k++) {
      const a = sorted[k - 1];
      const b = sorted[k];
      if (Math.abs(a.start + a.dur - b.start) < 0.11) {
        const jx = b.start * pps;
        if (Math.abs(px - jx) < 16 && (!best || Math.abs(px - jx) < Math.abs(px - best.x))) {
          best = {x: jx, id: b.id};
        }
      }
    }
    return best;
  }

  function setDropHint(hint: {trackId: string; t?: number; w?: number; junctionX?: number}) {
    clearDropHints();
    dropHint = hint;
    const lane = box?.timelineCanvas.querySelector<HTMLElement>(
      `.ed-lane[data-lane="${CSS.escape(hint.trackId)}"]`,
    );
    if (!lane) return;
    const el = document.createElement('div');
    if (hint.junctionX !== undefined) {
      el.className = 'ed-junction-hint';
      el.style.left = `${hint.junctionX - 3}px`;
    } else {
      el.className = 'ed-drop-hint';
      el.style.left = `${hint.t! * pps}px`;
      el.style.width = `${hint.w}px`;
    }
    lane.appendChild(el);
  }

  function clearDropHints() {
    dropHint = null;
    document.querySelectorAll('.ed-drop-hint, .ed-junction-hint').forEach(el => el.remove());
    document.querySelectorAll('.ed-item.fx-target').forEach(el => el.classList.remove('fx-target'));
  }

  /**
   * The pill is a child of the marker, so moving the pointer onto it keeps the
   * marker hovered and nothing is re-rendered mid-click.
   */
  function wireJunction(el: HTMLElement, itemId: string) {
    const close = () => el.classList.remove('open');
    el.addEventListener('pointerdown', e => {
      e.stopPropagation();
      el.classList.add('open');
    });
    el.addEventListener('contextmenu', e => {
      e.preventDefault();
      e.stopPropagation();
    });
    el.addEventListener('mouseenter', () => el.classList.add('open'));
    el.addEventListener('mouseleave', close);

    el.querySelectorAll<HTMLElement>('[data-bump]').forEach(btn => {
      btn.onclick = ev => {
        ev.preventDefault();
        ev.stopPropagation();
        const it = item(itemId);
        if (!it || !it.trans) return;
        const cap = Math.min(3, round(it.dur * 0.8));
        it.trans.len = round(
          Math.max(0.2, Math.min(cap, it.trans.len + parseFloat(btn.dataset.bump!))),
        );
        el.querySelector('.fx-len')!.textContent = `${it.trans.len.toFixed(1)}s`;
        scheduleSave();
        renderPreviewFrame(true);
      };
    });
    const rm = el.querySelector<HTMLElement>('[data-rm]');
    if (rm) {
      rm.onclick = ev => {
        ev.preventDefault();
        ev.stopPropagation();
        const it = item(itemId);
        if (!it) return;
        close();
        begin();
        delete it.trans;
        commit();
      };
    }
  }

  // ── context menus ───────────────────────────────────────────────
  type MenuEntry = {label: string; fn: () => void; kbd?: string; dis?: boolean; danger?: boolean};

  function menu(x: number, y: number, entries: Array<MenuEntry | '-'>) {
    document.getElementById('ed-menu')?.remove();
    const el = document.createElement('div');
    el.id = 'ed-menu';
    el.className = 'ed-menu';
    el.innerHTML = entries
      .map((en, i) =>
        en === '-'
          ? '<div class="ed-menu-sep"></div>'
          : `<button class="ed-menu-item${en.danger ? ' danger' : ''}" data-i="${i}" ${en.dis ? 'disabled' : ''}>
        <span>${escHtml(en.label)}</span>${en.kbd ? `<span class="kbd-hint">${escHtml(en.kbd)}</span>` : ''}
      </button>`,
      )
      .join('');
    document.body.appendChild(el);

    el.querySelectorAll<HTMLElement>('.ed-menu-item').forEach(btn => {
      btn.onclick = e => {
        e.stopPropagation();
        const en = entries[parseInt(btn.dataset.i!, 10)];
        el.remove();
        if (en && en !== '-' && !en.dis) en.fn();
      };
    });

    // offsetWidth/offsetHeight, not a client rect: the menu animates in with a
    // scale(), and a live rect would report the transformed box.
    el.style.left = `${Math.max(8, Math.min(x, window.innerWidth - el.offsetWidth - 8))}px`;
    el.style.top = `${Math.max(8, Math.min(y, window.innerHeight - el.offsetHeight - 8))}px`;
    const dismiss = (e: Event) => {
      if (!el.contains(e.target as Node)) {
        el.remove();
        document.removeEventListener('pointerdown', dismiss, true);
      }
    };
    window.setTimeout(() => document.addEventListener('pointerdown', dismiss, true), 0);
  }

  function openItemMenu(e: MouseEvent, id: string) {
    e.preventDefault();
    e.stopPropagation();
    select(id);
    const it = item(id);
    if (!it) return;
    const canSplit = playhead > it.start + 0.2 && playhead < it.start + it.dur - 0.2;
    menu(e.clientX, e.clientY, [
      {label: translate('editor.menuSplit'), kbd: 'S', fn: split, dis: !canSplit},
      {label: translate('editor.menuDetach'), fn: detachAudio, dis: !snapshot().canDetach},
      '-',
      {label: translate('editor.menuCopy'), kbd: 'Ctrl C', fn: copySel},
      {label: translate('editor.menuPaste'), kbd: 'Ctrl V', fn: paste, dis: !clipboard},
      {label: translate('editor.menuDuplicate'), kbd: 'Ctrl D', fn: duplicate},
      '-',
      {label: translate('editor.menuDelete'), kbd: 'Del', fn: remove, danger: true},
    ]);
  }

  function openTrackMenu(e: MouseEvent, trackId: string) {
    e.preventDefault();
    e.stopPropagation();
    const tr = project!.tracks.find(t => t.id === trackId);
    if (!tr) return;
    const removable =
      tr.type !== 'text' && !(tr.type === 'video' && videoTracks().length <= 1);
    menu(e.clientX, e.clientY, [
      {label: translate('editor.menuAddVideoTrack'), fn: () => addTrack('video')},
      {label: translate('editor.menuAddAudioTrack'), fn: () => addTrack('audio')},
      {label: translate('editor.menuPaste'), kbd: 'Ctrl V', fn: paste, dis: !clipboard},
      '-',
      {
        label: translate('editor.menuRemoveTrack', {name: tr.label}),
        fn: () => removeTrack(trackId),
        danger: true,
        dis: !removable,
      },
    ]);
  }

  // ── library list ────────────────────────────────────────────────
  function renderLibrary() {
    if (!box) return;
    const scroll = box.libraryScroll;
    if (tab === 'library') {
      const q = query.trim().toLowerCase();
      let list = clips().filter(c => (c.duration ?? 0) > 0);
      if (q) list = list.filter(c => `${c.name} ${c.game || ''}`.toLowerCase().includes(q));
      const cards = list
        .map(c => {
          const media = c.thumb_url
            ? `<img src="${escAttr(c.thumb_url)}" loading="lazy" alt="" draggable="false">`
            : `<div class="ed-lib-ph">${edGlyph(ED_ICONS.film, 24)}</div>`;
          const meta = [c.width ? `${c.width}x${c.height}` : '', c.size ? formatBytes(c.size) : '']
            .filter(Boolean)
            .join(' · ');
          return `
      <div class="ed-lib-clip" draggable="true" data-slug="${escAttr(c.slug)}"
           title="${escAttr(translate('editor.libClipHint'))}">
        <div class="ed-lib-thumb">
          ${media}
          ${c.game ? `<span class="ed-lib-game">${escHtml(c.game.toUpperCase())}</span>` : ''}
          <span class="ed-lib-dur">${fmtS(c.duration ?? 0)}</span>
        </div>
        <div class="ed-lib-name">${escHtml(clipTitle(c))}</div>
        <div class="ed-lib-meta">${escHtml(meta)}</div>
      </div>`;
        })
        .join('');
      scroll.innerHTML = `<div class="ed-lib-grid">${
        cards ||
        `<div class="ed-lib-empty">${
          q
            ? `${escHtml(translate('editor.libNoMatch'))}<br><span>${escHtml(translate('editor.libNoMatchBody'))}</span>`
            : `${escHtml(translate('editor.libEmpty'))}<br><span>${escHtml(translate('editor.libEmptyBody'))}</span>`
        }</div>`
      }</div>`;
    } else if (tab === 'effects') {
      scroll.innerHTML = ED_FX_ROWS();
    } else {
      scroll.innerHTML = ED_TEXT_ROWS();
    }
    wireLibrary();
  }

  const ED_FX_ROWS = () =>
    ED_FX
      .map(
        fx => `
      <div class="ed-fx-row" draggable="true" data-fx="${fx.id}">
        <span class="ed-fx-icon">${edGlyph(fx.glyph, 16)}</span>
        <div class="ed-fx-copy">
          <div class="ed-fx-name">${escHtml(fxName(fx.id))}</div>
          <div class="ed-fx-desc">${escHtml(translate(`editor.fx.${fx.id}.desc`))}</div>
        </div>
        <span class="ed-fx-len">${fx.len.toFixed(1)}s</span>
      </div>`,
      )
      .join('');

  const ED_TEXT_ROWS = () =>
    ED_TEXT_PRESETS.map(
      p => `
      <div class="ed-text-row" draggable="true" data-preset="${p.id}">
        <div class="ed-text-hd">
          <span class="ed-text-kind">${escHtml(translate(`editor.textPreset.${p.id}.name`).toUpperCase())}</span>
          <span>${ED_FONTS[p.font].label}</span>
        </div>
        <div class="ed-text-sample" style="font-family:${ED_FONTS[p.font].stack};font-weight:${p.weight};font-size:${Math.min(20, p.size / 2.8)}px;color:${p.color};${p.font === 'display' ? 'letter-spacing:-.02em;' : ''}">${escHtml(translate(`editor.textPreset.${p.id}.sample`))}</div>
      </div>`,
    ).join('');

  function wireLibrary() {
    if (!box) return;
    box.libraryScroll.querySelectorAll<HTMLElement>('.ed-lib-clip').forEach(el => {
      const slug = el.dataset.slug!;
      el.addEventListener('dragstart', e => dragStart(e, 'clip', slug));
      el.addEventListener('dragend', dragEnd);
      el.addEventListener('dblclick', () => quickAdd(slug));
    });
    box.libraryScroll.querySelectorAll<HTMLElement>('.ed-fx-row').forEach(el => {
      el.addEventListener('dragstart', e => dragStart(e, 'fx', el.dataset.fx!));
      el.addEventListener('dragend', dragEnd);
    });
    box.libraryScroll.querySelectorAll<HTMLElement>('.ed-text-row').forEach(el => {
      el.addEventListener('dragstart', e => dragStart(e, 'text', el.dataset.preset!));
      el.addEventListener('dragend', dragEnd);
    });
  }

  function dragStart(ev: DragEvent, kind: string, id: string) {
    drag = {kind, id};
    ev.dataTransfer!.effectAllowed = 'copy';
    ev.dataTransfer!.setData('text/plain', `vice-ed:${kind}:${id}`);

    const ghost = document.createElement('div');
    ghost.className = 'clip-drag-ghost';
    if (kind === 'clip') {
      const c = clips().find(x => x.slug === id);
      ghost.innerHTML =
        c && c.thumb_url
          ? `<img src="${escAttr(c.thumb_url)}" alt=""><span class="clip-drag-ghost-name">${escHtml(c.name || id)}</span>`
          : `<span class="clip-drag-ghost-name">${escHtml(id)}</span>`;
    } else {
      const label =
        kind === 'fx'
          ? (edFx(id) ? fxName(id) : id)
          : (ED_TEXT_PRESETS.some(x => x.id === id) ? translate(`editor.textPreset.${id}.name`) : id);
      ghost.innerHTML = `<span class="clip-drag-ghost-name">${escHtml(label)}</span>`;
    }
    document.body.appendChild(ghost);
    dragGhost = ghost;
    ev.dataTransfer!.setDragImage(ghost, 24, 20);
  }

  function dragEnd() {
    drag = null;
    if (dragGhost) {
      dragGhost.remove();
      dragGhost = null;
    }
    clearDropHints();
  }

  // Text presets dropped straight onto the preview land at the pointer.
  function wireStage() {
    if (!box) return;
    const stage = box.stage;
    stage.addEventListener('dragover', e => {
      if (drag && drag.kind === 'text') e.preventDefault();
    });
    stage.addEventListener('drop', e => {
      if (!drag || drag.kind !== 'text') return;
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      addText(drag.id, {
        t: playhead,
        x: ((e.clientX - r.left) / r.width) * 100,
        y: ((e.clientY - r.top) / r.height) * 100,
      });
      dragEnd();
    });
    stage.addEventListener('pointerdown', e => {
      const target = e.target as HTMLElement;
      if (target === stage || target.tagName === 'VIDEO') select(null);
    });
  }

  // ── keyboard ────────────────────────────────────────────────────
  function onKeyDown(e: KeyboardEvent) {
    if (!mounted || !project) return;
    const tg = e.target as HTMLElement;
    if (
      tg.tagName === 'INPUT' ||
      tg.tagName === 'TEXTAREA' ||
      tg.tagName === 'SELECT' ||
      tg.isContentEditable
    ) {
      return;
    }
    if (document.querySelector('.scrim')) return;
    const mod = e.metaKey || e.ctrlKey;
    if (e.code === 'Space') {
      e.preventDefault();
      setPlaying(!playing);
    } else if (e.key === 'Delete' || e.key === 'Backspace') remove();
    else if (mod && !e.shiftKey && (e.key === 'z' || e.key === 'Z')) {
      e.preventDefault();
      undo();
    } else if (mod && e.shiftKey && (e.key === 'z' || e.key === 'Z')) {
      e.preventDefault();
      redo();
    } else if (mod && (e.key === 'c' || e.key === 'C')) {
      e.preventDefault();
      copySel();
    } else if (mod && (e.key === 'v' || e.key === 'V')) {
      e.preventDefault();
      paste();
    } else if (mod && (e.key === 'd' || e.key === 'D')) {
      e.preventDefault();
      duplicate();
    } else if (!mod && (e.key === 's' || e.key === 'S')) split();
    else if (e.key === 'ArrowLeft') seek(Math.max(0, playhead - (e.shiftKey ? 0.1 : 1)));
    else if (e.key === 'ArrowRight') seek(Math.min(end(), playhead + (e.shiftKey ? 0.1 : 1)));
    else if (e.key === 'Escape') select(null);
    else if (e.key === '+' || e.key === '=') zoom(1.3);
    else if (e.key === '-') zoom(1 / 1.3);
  }

  // ── lifecycle ───────────────────────────────────────────────────
  function mount(containers: EditorContainers) {
    box = containers;
    mounted = true;
    document.addEventListener('keydown', onKeyDown);
    wireStage();
    refreshMissing();
    renderLibrary();
    renderTimeline();
    renderPreviewFrame(true);
  }

  function destroy() {
    mounted = false;
    document.removeEventListener('keydown', onKeyDown);
    setPlaying(false);
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    releasePool();
    stageObserver?.disconnect();
    stageObserver = null;
    document.getElementById('ed-menu')?.remove();
    if (dirty) void saveNow();
    box = null;
  }

  return {
    mount,
    destroy,
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    snapshot,
    load,
    setClips() {
      if (!loaded) return;
      refreshMissing();
      renderLibrary();
      renderTimeline();
    },
    onClipDeleted(slug) {
      if (!project) return;
      const before = project.items.length;
      project.items = project.items.filter(i => i.clipId !== slug);
      if (sel && !item(sel)) sel = null;
      refreshMissing();
      if (before !== project.items.length) {
        renderTimeline();
        renderPreviewFrame(true);
      }
    },
    async onProjectChanged() {
      // Never reload over unsaved local changes still in flight.
      if (!loaded || dirty) return;
      await load();
      if (sel && !item(sel)) sel = null;
      renderTimeline();
      renderPreviewFrame(true);
    },
    saveNow,
    isDirty: () => dirty,
    project: () => project,
    setTab(next) {
      tab = next;
      renderLibrary();
      emit();
    },
    search(next) {
      query = next;
      renderLibrary();
      emit();
    },
    zoom,
    fit,
    seek,
    setPlaying,
    split,
    detachAudio,
    duplicate,
    remove,
    addTrack,
    reset,
    select,
    inspectorChange,
    end,
  };
}
