import {createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode} from 'react';

import {api} from '../lib/api';
import {IS_NATIVE, keepRunning, quitVice} from '../lib/env';
import {Tutorial} from './Tutorial';
import {UpdateNotice} from './UpdateNotice';
import {IconMinimize, IconPower} from './Icons';
import {useStore} from '../state/store';
import {SideNav} from './SideNav';
import {StatusIsland} from './StatusIsland';
import {Banners} from './Banners';
import {t} from '../lib/i18n';

/** Matches --melt in shell.css. The arcs are drawn at this size. */
const MELT = 32;

/**
 * The window frame: navigation on the left, content on the right, and one
 * divider between them that curves outward at each end.
 *
 * Responsive contract: the full frame above 900px. At 900px and below the
 * navigation collapses to a rail and the melt is dropped, because there is no
 * longer a wide side for the curve to melt into.
 */
/*
 * Home's three arrangements, by the content width left after the frame's own
 * padding. Wide adds the Images panel beside the fixed 1180 column. Rail comes
 * once there is room for three thumbnail columns and a quick-settings rail as
 * well (1180 + 760 + 380, plus two gaps), and moves the quick settings out to it.
 */
const WIDE_HOME = 1580;
const RAIL_HOME = 2368;

export type HomeLayout = 'narrow' | 'wide' | 'rail';
const HomeLayoutContext = createContext<HomeLayout>('narrow');
export const useHomeLayout = () => useContext(HomeLayoutContext);

export function AppFrame({children}: {children: ReactNode}) {
  const {state} = useStore();
  // Both of these screens have their own controls in the bottom right.
  const quitHidden = state.view === 'editor' || state.view === 'settings';
  const [tutorial, setTutorial] = useState(false);
  const [updateOpen, setUpdateOpen] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const [homeLayout, setHomeLayout] = useState<HomeLayout>('narrow');

  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content || state.view !== 'home') return;
    const measure = () => {
      const style = getComputedStyle(content);
      // Exclude padding, but not the scrollbar: hiding it must not move the breakpoint.
      const width = content.offsetWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      setHomeLayout(width >= RAIL_HOME ? 'rail' : width >= WIDE_HOME ? 'wide' : 'narrow');
    };
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    measure();
    return () => observer.disconnect();
  }, [state.view]);

  // First run only. The flag is kept on the daemon as well as locally,
  // because the native window's localStorage does not survive a restart on
  // every QtWebEngine build, which made the tutorial reappear every launch.
  useEffect(() => {
    if (localStorage.getItem('vice_tutorial_shown')) return;
    let cancelled = false;
    void api
      .getAppState()
      .then(s => {
        if (cancelled) return;
        if (s.tutorial_seen) localStorage.setItem('vice_tutorial_shown', '1');
        else setTutorial(true);
      })
      .catch(() => !cancelled && setTutorial(true));
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="frame">
      <SideNav onShowTutorial={() => setTutorial(true)} onShowUpdate={() => setUpdateOpen(true)} />
      <Melt />
      <div className="frame-content" ref={contentRef}
        data-home-wide={state.view === 'home' && homeLayout !== 'narrow' || undefined}
        data-home-rail={state.view === 'home' && homeLayout === 'rail' || undefined}
        data-native={IS_NATIVE || undefined}>
        <Banners />
        <HomeLayoutContext.Provider value={homeLayout}>{children}</HomeLayoutContext.Provider>
      </div>
      <StatusIsland />
      {IS_NATIVE ? <QuitRow hidden={quitHidden} /> : null}
      <Tutorial open={tutorial} onClose={() => setTutorial(false)} />
      <UpdateNotice forceOpen={updateOpen} onClose={() => setUpdateOpen(false)} />
    </div>
  );
}

/**
 * The divider. Two quarter arcs and a straight run between them.
 *
 * Each arc leaves the frame edge horizontally and arrives at the divider
 * vertically, so the tangents match at both ends and neither joint reads as a
 * corner. The stroke fades out as it approaches the edge, which is what makes
 * it melt rather than simply stop; the tint underneath does not fade, so there
 * is no seam where the navigation surface meets the straight run.
 */
function Melt() {
  return (
    <div className="melt-group" aria-hidden="true">
      <svg width="0" height="0" className="melt-defs">
        <defs>
          <linearGradient id="vice-melt-fade" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#fff" stopOpacity="0" />
            <stop offset=".55" stopColor="#fff" stopOpacity=".05" />
            <stop offset="1" stopColor="#fff" stopOpacity=".10" />
          </linearGradient>
        </defs>
      </svg>

      <div className="melt melt-top">
        <MeltArc />
      </div>
      <div className="melt-line" />
      <div className="melt melt-bottom">
        <MeltArc />
      </div>
    </div>
  );
}

function MeltArc() {
  return (
    <svg width={MELT} height={MELT} viewBox={`0 0 ${MELT} ${MELT}`}>
      <path
        d={`M${MELT} 0 A${MELT} ${MELT} 0 0 0 0 ${MELT} L0 0 Z`}
        fill="var(--vice-nav-tint)"
      />
      <path
        d={`M${MELT} 0 A${MELT} ${MELT} 0 0 0 0 ${MELT}`}
        fill="none"
        stroke="url(#vice-melt-fade)"
        strokeWidth="1"
      />
    </svg>
  );
}

/**
 * Native only. Closing the window leaves the daemon recording, which is the
 * point, so the two outcomes are named rather than left to a close button.
 */
function QuitRow({hidden}: {hidden: boolean}) {
  return (
    <div className="quit-row" data-hidden={hidden || undefined} aria-hidden={hidden}>
      <button type="button" className="quit-btn" onClick={keepRunning}>
        <IconMinimize size={14} />
        <span>{t('frame.minimize')}</span>
      </button>
      <button
        type="button"
        className="quit-btn quit-btn-danger"
        onClick={() => {
          if (window.confirm(t('frame.quitConfirm'))) {
            quitVice();
          }
        }}>
        <IconPower size={14} />
        <span>{t('frame.quit')}</span>
      </button>
    </div>
  );
}
