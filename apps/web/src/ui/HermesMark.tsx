type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

const LOCKUP = "/tiny-hermes-lockup.png";
const ICON = "/tiny-hermes-icon.png";

/**
 * The lockup the operator actually sent: orange ring, blob listener,
 * one white eye, pause on the right cup, TINY-HERMES on the pill.
 *
 * This is the artwork, not a redraw of a different character.
 */
export function HermesMark({ size = 28, variant = "mark" }: HermesMarkProps) {
  const lockup = variant !== "mark";
  return (
    <img
      className={`th-hermes th-hermes-${variant}`}
      src={lockup ? LOCKUP : ICON}
      width={size}
      height={size}
      alt={variant === "hero" ? "TINY-HERMES" : ""}
      draggable={false}
    />
  );
}
