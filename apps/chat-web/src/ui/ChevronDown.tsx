export function ChevronDown({ size = 14 }: { size?: number }) {
  return (
    <svg
      className="chevron"
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden
    >
      <path
        d="M3.6 6.1 L8 10.4 L12.4 6.1"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.35"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
