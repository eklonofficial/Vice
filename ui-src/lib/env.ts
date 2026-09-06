/**
 * Facts about the window we are running in, decided once at module load.
 *
 * Every one of these was a bug report before it was a constant, so the
 * reasoning is kept with the code rather than in a commit message.
 */

/**
 * The pywebview native window. vice-app passes ?native=1 in the URL because
 * window.pywebview is only injected after DOMContentLoaded, which is too late
 * to get the quit row right on the first paint.
 */
export const IS_NATIVE: boolean = (() => {
  try {
    if (new URLSearchParams(location.search).get('native') === '1') return true;
  } catch {
    // A malformed query string is not a reason to fail the check below.
  }
  return typeof (window as {pywebview?: unknown}).pywebview !== 'undefined';
})();

/**
 * vice-app appends sw=1 when GPU compositing failed and the window relaunched
 * in software mode. This only covers the failure vice-app can see; the frame
 * probe catches the silent one.
 */
export const IS_SOFTWARE_RENDER: boolean = (() => {
  try {
    return new URLSearchParams(location.search).get('sw') === '1';
  } catch {
    return false;
  }
})();

/**
 * Whether this engine can decode H.264 at all. Every Vice clip is H.264, but
 * QtWebEngine builds without proprietary codecs (the PyPI PyQt6-WebEngine
 * wheels) silently render video as a blank rectangle, so the UI has to say
 * what is wrong instead of showing grey (#79).
 */
export const H264_SUPPORTED: boolean = (() => {
  try {
    return document.createElement('video').canPlayType('video/mp4; codecs="avc1.640028"') !== '';
  } catch {
    return true; // Assume capable: a false banner is worse than no banner.
  }
})();

/**
 * H.265 clips usually cannot decode in the native WebEngine. When they cannot,
 * the UI asks the daemon for an H.264 preview proxy instead of the raw file.
 */
export const HEVC_SUPPORTED: boolean = (() => {
  try {
    const v = document.createElement('video');
    return (
      v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"') !== '' ||
      v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') !== ''
    );
  } catch {
    return false;
  }
})();

interface PyWebView {
  api: {
    keep_running: () => void;
    quit_app: () => void;
    set_tray_labels?: (openLabel: string, quitLabel: string) => void;
    open_url?: (url: string) => void;
    log_debug?: (msg: string) => void;
  };
}

/**
 * Forward a diagnostic to the browser console and, in the native window, to
 * vice.log, so a reporter's log holds the whole timeline rather than half of
 * it. Playback failures are the case that needs this: they leave no trace on
 * the daemon side at all.
 */
export function nativeLog(msg: string): void {
  console.debug('[vice]', msg);
  try {
    const bridge = (window as unknown as {pywebview?: PyWebView}).pywebview;
    if (IS_NATIVE && bridge?.api?.log_debug) bridge.api.log_debug(String(msg));
  } catch (err) {
    console.debug('The native log bridge threw', err);
  }
}

/**
 * Open a link in the user's real browser. The native window has no chrome to
 * get back from, so a link followed inside it is a trap.
 */
export function openExternal(url: string | undefined): void {
  if (!url) return;
  try {
    const bridge = (window as unknown as {pywebview?: PyWebView}).pywebview;
    if (IS_NATIVE && bridge?.api?.open_url) {
      bridge.api.open_url(String(url));
      return;
    }
  } catch (err) {
    console.debug('The native open_url bridge threw', err);
  }
  window.open(url, '_blank', 'noopener');
}

/** Keep the native tray actions in the same language as the web UI. */
export function setNativeTrayLabels(openLabel: string, quitLabel: string): void {
  if (!IS_NATIVE) return;

  const apply = (): boolean => {
    try {
      const bridge = (window as unknown as {pywebview?: PyWebView}).pywebview;
      if (!bridge?.api?.set_tray_labels) return false;
      bridge.api.set_tray_labels(String(openLabel), String(quitLabel));
      return true;
    } catch (err) {
      console.debug('The native tray label bridge threw', err);
      return true;
    }
  };

  if (apply()) return;
  // pywebview documents that its JS API may arrive after the page itself.
  window.addEventListener('pywebviewready', () => { apply(); }, {once: true});
}

/** Hide the window but leave the daemon recording. Native only. */
export function keepRunning(): void {
  if (IS_NATIVE) (window as unknown as {pywebview: PyWebView}).pywebview.api.keep_running();
}

/** Stop the daemon and close. Falls back to HTTP outside the native window. */
export function quitVice(): void {
  if (IS_NATIVE) {
    (window as unknown as {pywebview: PyWebView}).pywebview.api.quit_app();
  } else {
    void fetch('/api/quit', {method: 'POST'}).catch(() => {});
  }
}
