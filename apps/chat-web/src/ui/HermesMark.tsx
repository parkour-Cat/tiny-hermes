type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

const LOCKUP = "/tiny-hermes-lockup.png";
const ICON = "/tiny-hermes-icon.png";

/**
 * The two files the operator put in public/: lockup with the
 * word, icon without. They sit as a badge, not a square tile.
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
