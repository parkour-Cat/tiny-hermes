import { useEffect } from "react";
import type { RefObject } from "react";

/** Nonmodal popovers share keyboard entry and return focus without stealing outside clicks. */
export function useDismiss(
  open: boolean,
  onClose: () => void,
  root: RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    if (!open) {
      return;
    }
    const container = root.current;
    const trigger = container?.querySelector<HTMLElement>("[aria-haspopup]");
    const panel = container?.querySelector<HTMLElement>('[role="menu"], [role="listbox"], [role="dialog"]');
    const choices = () => Array.from(panel?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href]') ?? []);
    const items = choices();
    (items.find((item) => item.getAttribute("aria-selected") === "true") ?? items[0])?.focus();
    function onKey(event: KeyboardEvent): void {
      if (!container?.contains(event.target as Node)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        trigger?.focus();
        onClose();
      } else if (panel?.matches('[role="menu"], [role="listbox"]') &&
        ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        const options = choices();
        if (!options.length) return;
        event.preventDefault();
        const current = options.indexOf(document.activeElement as HTMLElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 :
          (current + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
        options[next]?.focus();
      }
    }
    function onFocus(event: FocusEvent): void {
      if (!container?.contains(event.target as Node)) onClose();
    }
    function onPointer(event: MouseEvent): void {
      if (root.current !== null && !root.current.contains(event.target as Node)) {
        onClose();
      }
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("focusin", onFocus);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("focusin", onFocus);
      if (trigger?.isConnected && (panel?.contains(document.activeElement) || document.activeElement === document.body)) {
        trigger.focus();
      }
    };
  }, [open, onClose, root]);
}
