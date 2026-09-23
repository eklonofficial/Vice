import type {Clip} from '../lib/types';

export interface EdTrack {
  id: string;
  type: 'video' | 'audio' | 'text';
  label: string;
}

export interface EdTransition {
  fx: string;
  len: number;
}

export interface EdItem {
  id: string;
  kind: 'clip' | 'audio' | 'text';
  trackId: string;
  start: number;
  dur: number;
  clipId?: string;
  offset?: number;
  muted?: boolean;
  audioStream?: number;
  volume?: number;
  trans?: EdTransition;
  text?: string;
  font?: string;
  size?: number;
  weight?: number;
  color?: string;
  x?: number;
  y?: number;
}

export interface EdProject {
  version: number;
  tracks: EdTrack[];
  items: EdItem[];
}

export type EdTab = 'library' | 'effects' | 'text';

/** What the React chrome renders from. Recomputed on every engine change. */
export interface EdSnapshot {
  ready: boolean;
  tab: EdTab;
  query: string;
  playing: boolean;
  playhead: number;
  duration: number;
  pps: number;
  selected: EdItem | null;
  canSplit: boolean;
  canDetach: boolean;
  canDuplicate: boolean;
  canDelete: boolean;
  canUndo: boolean;
  canRedo: boolean;
  /** Set while the daemon is transcoding an H.265 proxy for a clip on stage. */
  preparing: boolean;
  empty: boolean;
}

export interface EditorDeps {
  clips: () => Clip[];
  notify: (title: string, tone?: 'accent' | 'error') => void;
  accent: () => string;
}
