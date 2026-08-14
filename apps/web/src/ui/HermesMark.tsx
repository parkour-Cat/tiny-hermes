type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

const LOCKUP = "/tiny-hermes-lockup.png";
const ICON = "/tiny-hermes-icon.png";

/**
 * Two artworks the operator sent:
 * - lockup: the named badge (✦ TINY-HERMES ✦) for login and empty
 * - icon: the same listener without the word, for the sider and tab
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
