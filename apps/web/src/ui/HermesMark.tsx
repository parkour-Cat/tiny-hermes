type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

const LOCKUP = "/tiny-hermes-lockup.png";
const ICON = "/tiny-hermes-icon.png";

/**
 * The two artworks sit as a badge, not a square tile.
 *
 * The mark is clipped to a circle. The named lockup keeps the
 * pill under the ring; only the field around the badge is gone.
 */
export function HermesMark({ size = 28, variant = "mark" }: HermesMarkProps) {
  const lockup = variant !== "mark";
  return (
    <span className={`th-hermes-wrap th-hermes-wrap-${variant}`} style={{ width: size }}>
      <img
        className={`th-hermes th-hermes-${variant}`}
        src={lockup ? LOCKUP : ICON}
        width={size}
        height={size}
        alt={variant === "hero" ? "TINY-HERMES" : ""}
        draggable={false}
      />
    </span>
  );
}
