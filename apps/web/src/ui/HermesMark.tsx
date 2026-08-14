type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

/**
 * The listener, restated for a runtime you can pause.
 *
 * Bloodline: three-quarter profile, hime bangs, over-ear cups. This project's
 * turn is the near cup — a valve with a pause bar, not a chat mascot. Drawn
 * here rather than imported; a raster of someone else's icon is still theirs.
 */
export function HermesMark({ size = 28, variant = "mark" }: HermesMarkProps) {
  const detailed = variant !== "mark";
  return (
    <svg
      className={`th-hermes th-hermes-${variant}`}
      width={size}
      height={size}
      viewBox="0 0 80 80"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {variant === "hero" ? <circle className="th-hermes-seal" cx="40" cy="40" r="38.5" /> : null}
      <path
        className="th-hermes-ink"
        d="M26.5 10.2c14-3 28.2 4.4 32.6 17.8 3 9 1.2 19.2-4.6 26.2-2 2.4-2.4 6.4-.6 9.2 2.4 3.8 1.2 8-2.8 9.8-5.6 2.4-12.2-.6-15.2-5.8-2.4 4-7.2 7.2-12.4 6-3.6-.8-6-4-6-7.6.2-3.4-1.4-6.6-3.8-8.8C9.3 52.4 8.6 45 11.4 39c-2.2-2.6-2-6.6.6-9.2C16.8 19.2 21.4 12.4 26.5 10.2Z"
      />
      <path
        className="th-hermes-paper th-hermes-face"
        d="M23 26.8h15.2c3.2 1.6 5.4 6.2 4.8 11.2-.8 6.2-4.8 11.4-10.2 12.4-3.4.6-6.6-1-8.4-3.8-1.6-2.4-2.6-4.6-2-7.2.2-1.6-.8-3-2-3.8 1.2-2.2 2-6.2 2.6-8.8Z"
      />
      <path
        className="th-hermes-ink th-hermes-bangs"
        d="M20.8 16.4c8.4-5.6 20.6-6 29.2-1.2L47.6 26c-7.2 3.2-16.4 3.4-23.6.6-1.8-2.8-2.8-6.4-3.2-10.2Z"
      />
      {detailed ? (
        <path
          className="th-hermes-paper th-hermes-fine"
          d="M30.4 13.8c3.8-1 8.2 0 10.8 2.2-3.4 0-6.6.8-9.2 2.4-1.6-1.6-2.2-3.6-1.6-4.6Z"
        />
      ) : null}
      <path className="th-hermes-band" d="M14.6 35.4C19 19.2 32.4 11 46.4 11.6c12.6.6 23 8.8 26.2 20.8" />
      <circle className="th-hermes-cup" cx="14.4" cy="36.6" r="5.8" />
      <circle className="th-hermes-paper" cx="14.4" cy="36.6" r="2.4" />
      <circle className="th-hermes-cup" cx="65.6" cy="40.6" r="13.2" />
      <circle className="th-hermes-rim" cx="65.6" cy="40.6" r="9.4" />
      <circle className="th-hermes-paper" cx="65.6" cy="40.6" r="6.8" />
      <rect className="th-hermes-pause" x="61.8" y="35.2" width="3.1" height="10.8" rx="0.9" />
      <rect className="th-hermes-pause" x="66.3" y="35.2" width="3.1" height="10.8" rx="0.9" />
      {detailed ? (
        <>
          <ellipse className="th-hermes-ink th-hermes-fine" cx="28.6" cy="38.4" rx="2.5" ry="3.2" />
          <path className="th-hermes-lash th-hermes-fine" d="M26.2 34.4c1.5-1.5 4-2 6-1.1" />
        </>
      ) : null}
    </svg>
  );
}
