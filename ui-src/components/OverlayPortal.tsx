import {createContext, useContext, useState, type ReactNode} from 'react';
import {createPortal} from 'react-dom';

const OverlayTarget = createContext<HTMLElement | null>(null);

/** Keep dialogs outside scroll containers without losing the app's theme. */
export function OverlayRoot({children}: {children: ReactNode}) {
  const [target, setTarget] = useState<HTMLDivElement | null>(null);
  return (
    <OverlayTarget.Provider value={target}>
      {children}
      <div ref={setTarget} className="overlay-root" />
    </OverlayTarget.Provider>
  );
}

export function OverlayPortal({children}: {children: ReactNode}) {
  const target = useContext(OverlayTarget);
  return target ? createPortal(children, target) : null;
}
