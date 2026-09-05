type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

const LOCKUP = "/tiny-hermes-lockup.png";
const ICON = "/tiny-hermes-icon.png";

/**
 * The two artwork files in `public/`, the same ones the chat page uses: the
 * lockup with the word for a hero or an empty state, the silent icon for
 * the sider and the tab. Artwork, not a redraw — the console once drew its
 * own listener and it was a different character.
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
