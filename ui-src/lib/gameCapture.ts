import {t} from './i18n';
import type {Status} from './types';

/**
 * Why the Game Capture switch is unavailable, as the line to show under it, or
 * null when it can be used. Window pinning is GSR-only (status.backend is what
 * actually runs, not the configured "auto"), and GSR only pins a window in an
 * X11 session (#261, #274).
 */
export function gameCaptureBlocked(status: Pick<Status, 'backend' | 'window_capture_unavailable'>): string | null {
  if (status.backend !== 'gpu-screen-recorder') return t('home.gameCaptureUnsupported');
  if (status.window_capture_unavailable) return t('home.gameCaptureNeedsX11');
  return null;
}
