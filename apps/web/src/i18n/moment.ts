/**
 * A server timestamp, written out in the reader's own time zone.
 *
 * The platform records instants in UTC and sends them as ISO-8601. Showing
 * that string unchanged would ask an operator to do time-zone arithmetic while
 * reading a queue; showing a local time without seconds would hide the gaps
 * that matter when Runs are seconds apart. So: local zone, seconds kept.
 */
export function moment(value: string): string {
  return new Date(value).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
