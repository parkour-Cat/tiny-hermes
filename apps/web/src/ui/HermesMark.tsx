type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

/**
 * A listener in three-quarter profile.
 *
 * Bloodline: hime bangs, over-ear cups, face cut from the hair. Headphones
 * stay hardware — a band and two cups — not a media-control badge and not a
 * coin. Drawn here; a raster of someone else's icon is still theirs.
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
      <path
        className="th-hermes-ink"
        d="M32 9c2.6-1.3 8.2-3.2 12-3 3.8.2 9 1.8 12 4 3 2.2 5.6 6.2 7 10 1.4 3.8 2.2 9.5 2 14-.2 4.5-1.2 10.2-3 14-1.8 3.8-6.1 6.8-8 10-1.9 3.2-2.1 9-4 10-1.9 1-6.4-5-8-4-1.6 1-.4 7.8-2 10-1.6 2.2-6.1 4.6-8 4-1.9-.6-4.3-5.1-4-8 .3-2.9 6.3-7.8 6-10-.3-2.2-5.8-1.8-8-4-2.2-2.2-5-6.5-6-10-1-3.5-.6-8.2 0-12 .6-3.8 2.7-8.8 4-12 1.3-3.2 2.7-5.9 4-8 1.3-2.1 1.4-3.7 4-5Z"
      />
      <path
        className="th-hermes-face"
        d="M27 27c2.6-1.2 7.4-2 11-1 3.6 1 5.4 2.8 7 6 1.6 3.2 1.6 6 1 10-.6 4-1.6 7-4 10-2.4 3-4.8 4.6-8 5-3.2.4-5.6-1.2-8-3-2.4-1.8-2.8-3.6-4-6-1.2-2.4-2.2-4-2-6 .2-2 2-2 3-4 1-2 1.2-3.8 2-6 .8-2.2-.6-3.8 2-6Z"
      />
      <path
        className="th-hermes-bangs"
        d="M22 15c1.4-2.2 6-3.1 10-4 4-.9 8-1.5 12-1 4 .5 8.6 1.5 10 4 1.4 2.5.2 7.5-2 10-2.2 2.5-6.4 3.3-10 4-3.6.7-6.8.9-10 0-3.2-.9-6.2-2.7-8-5-1.8-2.3-3.4-5.8-2-8Z"
      />
      {detailed ? (
        <path
          className="th-hermes-paper th-hermes-fine"
          d="M34 11.2c3.8-1 8.2-.2 10.8 2-3.2.2-6.2 1-8.6 2.4-1.6-1.6-2.4-3.4-2.2-4.4Z"
        />
      ) : null}
      <path className="th-hermes-band" d="M19 33C24 14 38 9 51 11c12 1 21 11 24 24" />
      <circle className="th-hermes-cup" cx="19" cy="33.5" r="5.6" />
      <circle className="th-hermes-paper" cx="19" cy="33.5" r="2.3" />
      <circle className="th-hermes-cup" cx="65.5" cy="39" r="11" />
      <circle className="th-hermes-paper" cx="65.5" cy="39" r="5.6" />
      {detailed ? (
        <>
          <ellipse className="th-hermes-eye th-hermes-fine" cx="31" cy="39" rx="2.5" ry="3.1" />
          <path className="th-hermes-lash th-hermes-fine" d="M28.6 35.2c1.6-1.6 4.4-2 6.6-1" />
        </>
      ) : null}
    </svg>
  );
}
