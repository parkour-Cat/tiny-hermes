import { Typography } from "antd";

/** Longer than this and a value is an identifier nobody reads, only copies. */
const WHOLE = 20;

/** 表格里的 ID 一律截断，完整值挂在 title 上（§4.1）。截的是文本本身而不是
 *  CSS 省略号：省略号只在有宽度可量的地方生效，而一个 36 位的 UUID 在窄屏上
 *  折成两行占掉整屏最宽的一格——那个值几乎没有人会读。 */
export function shortenId(value: string): string {
  return value.length > WHOLE ? `${value.slice(0, 8)}…` : value;
}

/** An identifier in a table cell: truncated, hoverable to the full value,
 *  and copyable — copying is the only thing anyone does with one. */
export function ShortId({ value }: { value: string }) {
  return (
    <Typography.Text title={value} copyable={{ text: value }}>
      {shortenId(value)}
    </Typography.Text>
  );
}
