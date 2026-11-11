import {useCallback} from 'react';

import {api} from './api';
import {CUSTOM_ACCENT_KEY, useStore} from '../state/store';
import {ACCENTS, type AccentName} from '../theme/accents';
import {customAccent as deriveCustomTheme, type AccentChoice} from '../theme/viceTheme';

/**
 * Choosing an accent, in one place.
 *
 * Settings and the tutorial both offer the swatches, and both have to do the
 * same four things: apply it, remember it locally, remember the seed if it was
 * a custom one, and tell the daemon so shared clips carry the same colour.
 * They were two copies of three of those before custom made it four.
 */
export function useAccentChoice(): {
  accent: AccentChoice;
  seed: string | null;
  choose: (name: AccentName) => void;
  chooseCustom: (seed: string) => void;
  chooseMatugen: () => void;
} {
  const {state, dispatch} = useStore();

  const remember = useCallback((key: string, value: string) => {
    try {
      localStorage.setItem(key, value);
    } catch (err) {
      // A build that refuses storage. The accent still applies to this
      // session, it just will not survive a restart.
      console.debug('Saving the accent failed', err);
    }
  }, []);

  // Share-page embeds carry the same accent, so the Discord strip on a shared
  // clip matches the app it came from.
  const pushEmbedColour = useCallback((base: string) => {
    void api
      .saveConfig({sharing: {embed_color: base}})
      .catch(err => console.debug('Saving the embed colour failed', err));
  }, []);

  const choose = useCallback(
    (name: AccentName) => {
      dispatch({type: 'setAccent', accent: name});
      remember('vice-theme', name);
      pushEmbedColour(ACCENTS[name].base);
    },
    [dispatch, remember, pushEmbedColour],
  );

  const chooseCustom = useCallback(
    (seed: string) => {
      const {ramp} = deriveCustomTheme(seed);
      dispatch({type: 'setAccent', accent: 'custom', custom: seed});
      remember('vice-theme', 'custom');
      remember(CUSTOM_ACCENT_KEY, seed);
      // The boot cover paints before the bundle parses, so it cannot derive
      // anything. These two are the whole of what it needs, cached here at the
      // one moment the scheme is known.
      remember('vice-custom-bg', ramp.bg);
      remember('vice-custom-base', ramp.base);
      // The derived primary, not the seed. The seed is what the user pointed
      // at; the accent is what Material 3 made of it, and that is the colour
      // the app is actually wearing.
      pushEmbedColour(ramp.base);
    },
    [dispatch, remember, pushEmbedColour],
  );

  // The cover's colours and the embed colour are kept in step by App, since
  // matugen can change them while the window is open.
  const chooseMatugen = useCallback(() => {
    dispatch({type: 'setAccent', accent: 'matugen'});
    remember('vice-theme', 'matugen');
  }, [dispatch, remember]);

  return {accent: state.accent, seed: state.customAccent, choose, chooseCustom, chooseMatugen};
}
