import { useId } from "react";

type HermesMarkProps = {
  size?: number;
  variant?: "mark" | "hero" | "empty";
};

function Star({ x, y }: { x: number; y: number }) {
  return (
    <path
      className="th-hermes-star"
      d={`M${x} ${y - 7}l1.8 5.2 5.2 1.8-5.2 1.8L${x} ${y + 7}l-1.8-5.2L${x - 7} ${y}l5.2-1.8Z`}
    />
  );
}

/**
 * The lockup the console actually uses: orange ring, listener, white
 * cans with a pause bar, and — on the hero — TINY-HERMES on a pill.
 */
export function HermesMark({ size = 28, variant = "mark" }: HermesMarkProps) {
  const clipId = `th-clip-${useId().replaceAll(":", "")}`;
  const lockup = variant === "hero";
  const height = lockup ? Math.round((size * 260) / 240) : size;
  return (
    <svg
      className={`th-hermes th-hermes-${variant}`}
      width={size}
      height={height}
      viewBox={lockup ? "0 0 240 260" : "0 0 240 240"}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <circle className="th-hermes-field" cx="120" cy="118" r="104" />
      <circle className="th-hermes-ring" cx="120" cy="118" r="104" />
      <clipPath id={clipId}>
        <circle cx="120" cy="118" r="99" />
      </clipPath>
      <g clipPath={`url(#${clipId})`}>
        <path
          className="th-hermes-ink"
          d="M70 40c26-18 70-16 90 14 14 20 14 48 2 70-6 12-4 30 6 40 4 4 2 14-6 16-16 6-34-4-44-16-8 14-24 26-40 22-14-4-20-18-16-30 2-8-4-16-12-22C34 118 32 92 44 74c10-16 16-22 26-34Z"
        />
        <path
          className="th-hermes-face"
          d="M78 78c-10 10-12 28-4 42 7 12 22 18 34 12 11-5 16-18 14-30-2-12-12-22-26-26-6-2-13-2-18 2Z"
        />
        <path
          className="th-hermes-bangs"
          d="M68 54c24-16 62-16 84 0-10 8-22 16-32 24-14 4-30 4-42-2-5-6-8-14-10-22Z"
        />
        <path className="th-hermes-ink" d="M104 150c0 18-14 34-30 40-7 2-12-4-10-11 6-14 18-24 28-31 4 0 10 0 12 2Z" />
      </g>
      <path className="th-hermes-band-edge" d="M48 106C60 54 94 38 128 40c32 2 58 24 66 54" />
      <path className="th-hermes-band" d="M48 106C60 54 94 38 128 40c32 2 58 24 66 54" />
      <circle className="th-hermes-cup-edge" cx="47" cy="106" r="17" />
      <circle className="th-hermes-cup" cx="47" cy="106" r="13" />
      <circle className="th-hermes-cup-edge" cx="180" cy="112" r="30" />
      <circle className="th-hermes-cup" cx="180" cy="112" r="24.5" />
      <rect className="th-hermes-pause" x="170.2" y="99" width="7" height="26" rx="2.2" />
      <rect className="th-hermes-pause" x="182.8" y="99" width="7" height="26" rx="2.2" />
      {variant !== "mark" ? (
        <>
          <ellipse className="th-hermes-eye th-hermes-fine" cx="94" cy="106" rx="10" ry="13" />
          <ellipse className="th-hermes-paper th-hermes-fine" cx="90.5" cy="102" rx="3.4" ry="4" />
          <path className="th-hermes-lash th-hermes-fine" d="M84 94c7-7 18-8 26-3" />
        </>
      ) : null}
      {lockup ? (
        <>
          <rect className="th-hermes-banner" x="16" y="204" width="208" height="46" rx="23" />
          <Star x={38} y={227} />
          <Star x={202} y={227} />
          <text className="th-hermes-lockup" x="120" y="234">
            TINY-HERMES
          </text>
        </>
      ) : null}
    </svg>
  );
}
