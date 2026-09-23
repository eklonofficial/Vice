import {useEffect, useLayoutEffect, useRef, useState} from 'react';

export interface MenuItem {
  id: string;
  label?: string;
  mark?: string;
  disabled?: boolean;
  danger?: boolean;
  /** A rule between groups. Carries no label and cannot be chosen. */
  separator?: boolean;
  onSelect?: () => void;
}

/**
 * A menu anchored to the pointer, clamped so it never opens off screen.
 * Dismisses on outside click, Escape, scroll or resize.
 */
export interface QuickAction {
  id: string;
  /** Shown as the button's face. An emoji, not a label. */
  glyph: string;
  title: string;
  active?: boolean;
  onSelect: () => void;
}

export function ContextMenu({
  at,
  heading,
  items,
  quick,
  emptyLabel,
  onClose,
}: {
  at: {x: number; y: number};
  heading: string;
  items: MenuItem[];
  /** A strip of one-tap actions above the list. A row, because eight emoji
   *  stacked as menu items is a scroll, not a picker. */
  quick?: QuickAction[];
  emptyLabel: string;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState(at);

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    // offsetWidth/offsetHeight, not a client rect: the menu plays a scale()
    // entry animation, and a client rect reports the transformed box, so the
    // clamp would under-correct by however far the animation has left to run.
    const margin = 8;
    setPos({
      x: Math.max(margin, Math.min(at.x, window.innerWidth - node.offsetWidth - margin)),
      y: Math.max(margin, Math.min(at.y, window.innerHeight - node.offsetHeight - margin)),
    });
  }, [at]);

  useEffect(() => {
    const dismiss = (e: Event) => {
      if ((e.type === 'pointerdown' || e.type === 'scroll') && e.target instanceof Node && ref.current?.contains(e.target)) {
        return;
      }
      onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('pointerdown', dismiss, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', dismiss);
    window.addEventListener('scroll', dismiss, true);
    return () => {
      document.removeEventListener('pointerdown', dismiss, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', dismiss);
      window.removeEventListener('scroll', dismiss, true);
    };
  }, [onClose]);

  const choosable = items.filter(item => !item.separator);

  return (
    <div className="ctx-menu" ref={ref} style={{left: pos.x, top: pos.y}} role="menu">
      <p className="ctx-heading">{heading}</p>
      {quick?.length ? (
        <div className="ctx-quick">
          {quick.map(action => (
            <button
              key={action.id}
              type="button"
              className="ctx-quick-btn"
              data-active={action.active || undefined}
              title={action.title}
              aria-label={action.title}
              onClick={() => {
                action.onSelect();
                onClose();
              }}>
              {action.glyph}
            </button>
          ))}
        </div>
      ) : null}
      {choosable.length === 0 ? (
        <p className="ctx-empty">{emptyLabel}</p>
      ) : (
        items.map(item =>
          item.separator ? (
            <div className="ctx-sep" key={item.id} role="separator" />
          ) : (
            <button
              key={item.id}
              type="button"
              className="ctx-item"
              data-danger={item.danger || undefined}
              role="menuitem"
              disabled={item.disabled}
              onClick={() => {
                item.onSelect?.();
                onClose();
              }}>
              <span className="ctx-mark">{item.mark ?? ''}</span>
              <span>{item.label}</span>
            </button>
          ),
        )
      )}
    </div>
  );
}
