import { Input, Space } from "antd";
import { useT } from "../i18n/locale";

export function timeBounds(from: string, through: string): { since?: string; until?: string } {
  const start = from ? new Date(`${from}T00:00:00`) : null;
  const end = through ? new Date(`${through}T00:00:00`) : null;
  if (end) end.setDate(end.getDate() + 1);
  return {
    ...(start && !Number.isNaN(start.valueOf()) ? { since: start.toISOString() } : {}),
    ...(end && !Number.isNaN(end.valueOf()) ? { until: end.toISOString() } : {}),
  };
}

export function TimeRangeFilter({ from, through, onChange }: {
  from: string; through: string; onChange: (from: string, through: string) => void;
}) {
  const t = useT();
  return <Space wrap>
    <label>{t("timeFrom")} <Input type="date" aria-label={t("timeFrom")} value={from} max={through || undefined} onChange={(event) => onChange(event.target.value, through)} /></label>
    <label>{t("timeThrough")} <Input type="date" aria-label={t("timeThrough")} value={through} min={from || undefined} onChange={(event) => onChange(from, event.target.value)} /></label>
  </Space>;
}
