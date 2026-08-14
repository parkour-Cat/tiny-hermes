type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

/**
 * A coin, not a watermark.
 *
 * Bloodline: three-quarter profile, hime bangs, over-ear cups. The near cup
 * is a pause valve — this console's claim. The disc is the reason it reads
 * as a mark at 16px; a free silhouette on paper disappears in a screenshot.
 * Drawn here. A raster of someone else's icon is still theirs.
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
      <circle className="th-hermes-disc" cx="40" cy="40" r="40" />
      <circle className="th-hermes-coin" cx="40" cy="40" r="36.5" />
      <path
        className="th-hermes-figure"
        d="M26 8c14.8-3.2 31.6 3.6 36.6 18 3.4 9.8 1.6 21.2-4.8 29-2 2.4-2.2 6.6-.4 9.2 2.2 3.4.6 7.6-3 9.2-5.4 2.2-12 0-15.2-5.2-1.6 3.2-5.2 6.2-9.4 6.2-3.2 0-5.4-2.4-5.8-5.4 1.2-3.2 1-6.6-.8-9.4C19.4 54 18 48.8 18.6 44c-2.6-2-3.2-6-1.2-8.8C21.4 22.4 22.6 12.2 26 8Z"
      />
      <path
        className="th-hermes-face"
        d="M22 25.4h16.4c3.8 2 6 7.6 5 13.2-1.2 7.4-6.2 13.6-12.6 14.6-4 .6-7.6-1.4-9.4-5-1.6-3.2-2.4-6.6-1.4-10 .4-1.6-.6-3.2-2-4 1.2-2.6 2.2-6.4 4-8.8Z"
      />
      <path
        className="th-hermes-bangs"
        d="M20 15.2c9.4-5.8 23-6 32.4-.8L49 26.4c-8.2 3.2-18.6 3.2-26.6 0-1.6-3.2-2.6-7.4-2.4-11.2Z"
      />
      {detailed ? (
        <path
          className="th-hermes-paper th-hermes-fine"
          d="M30.6 12.2c4.2-1.2 9 0 11.8 2.6-3.8 0-7.2.8-10 2.6-1.8-1.8-2.4-4-1.8-5.2Z"
        />
      ) : null}
      <path className="th-hermes-band" d="M13.8 35.2C18.6 17.8 33 9.2 48 10c13.4.8 24.2 9.6 27.2 22.4" />
      <circle className="th-hermes-cup" cx="14" cy="36.2" r="6.4" />
      <circle className="th-hermes-paper" cx="14" cy="36.2" r="2.6" />
      <circle className="th-hermes-cup" cx="64.8" cy="41" r="14" />
      <circle className="th-hermes-rim" cx="64.8" cy="41" r="10" />
      <circle className="th-hermes-paper" cx="64.8" cy="41" r="7.2" />
      <rect className="th-hermes-pause" x="60.8" y="35.2" width="3.4" height="11.6" rx="1" />
      <rect className="th-hermes-pause" x="65.6" y="35.2" width="3.4" height="11.6" rx="1" />
      {detailed ? (
        <>
          <ellipse className="th-hermes-eye th-hermes-fine" cx="28.2" cy="38.2" rx="2.8" ry="3.4" />
          <path className="th-hermes-lash th-hermes-fine" d="M25.6 33.8c1.6-1.6 4.4-2.2 6.6-1.2" />
        </>
      ) : null}
    </svg>
  );
}
