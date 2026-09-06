import {StrictMode, useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Theme} from '@astryxdesign/core/theme';

import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import './styles/base.css';
import './styles/shell.css';
import './styles/home.css';
import './styles/clips.css';
import './styles/images.css';
import './styles/viewer.css';
import './styles/settings.css';
import './styles/editor.css';

import {initLocale, subscribeLocale, t} from './lib/i18n';
import {setNativeTrayLabels} from './lib/env';
import {accentVars, resolveAccent} from './theme/viceTheme';
import {StoreProvider, useStore} from './state/store';
import {PlaybackProvider} from './state/playback';
import {AppFrame} from './components/AppFrame';
import {Home} from './screens/Home';
import {Clips} from './screens/Clips';
import {Images} from './screens/Images';
import {Settings} from './screens/Settings';
import {Editor} from './screens/Editor';
import {About} from './screens/About';

function syncNativeTrayLabels(): void {
  setNativeTrayLabels(t('tray.openVice'), t('tray.quitVice'));
}

function App() {
  const {state} = useStore();
  const {accent, customAccent, ready, view} = state;
  // One resolve per render, shared by the theme, the ambient wash and the
  // custom properties, so a custom accent cannot be derived three times.
  const {ramp, theme} = resolveAccent(accent, customAccent);
  const [, retranslate] = useState(0);

  useEffect(() => {
    syncNativeTrayLabels();
    return subscribeLocale(() => {
      retranslate(n => n + 1);
      syncNativeTrayLabels();
    });
  }, []);

  // The boot cover is in index.html so it paints before this bundle parses.
  // It goes once there is real data behind it, not merely once React mounted.
  useEffect(() => {
    if (!ready) return;
    const boot = document.getElementById('boot');
    if (!boot) return;
    boot.classList.add('boot-done');
    const remove = () => boot.remove();
    boot.addEventListener('transitionend', remove, {once: true});
    // A missed transitionend must not leave an invisible cover over the app.
    const failsafe = window.setTimeout(remove, 1200);
    return () => window.clearTimeout(failsafe);
  }, [ready]);

  // On the root, not on .vice-app: body and the boot cover both sit outside
  // that element and would otherwise never see the themed background.
  useEffect(() => {
    const root = document.documentElement;
    const vars = accentVars(ramp);
    for (const [key, value] of Object.entries(vars)) root.style.setProperty(key, value);
  }, [ramp]);

  return (
    <Theme theme={theme} mode="dark">
      <div className="vice-ambient" style={accentVars(ramp)} aria-hidden="true" />
      <PlaybackProvider>
        <div className="vice-app" style={accentVars(ramp)}>
          <AppFrame>
            <Screen view={view} />
          </AppFrame>
        </div>
      </PlaybackProvider>
    </Theme>
  );
}

function Screen({view}: {view: string}) {
  if (view === 'home') return <Home />;
  if (view === 'clips') return <Clips />;
  if (view === 'images') return <Images />;
  if (view === 'settings') return <Settings />;
  if (view === 'editor') return <Editor />;
  return <About />;
}

// Before the first render, so no screen paints in English and then switches.
initLocale();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <StoreProvider>
      <App />
    </StoreProvider>
  </StrictMode>,
);
