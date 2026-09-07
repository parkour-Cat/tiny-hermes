import { expect, type Locator } from "@playwright/test";

/** WCAG text contrast on solid surfaces, compositing transparent ancestor colors. */
export async function expectReadableControl(control: Locator): Promise<void> {
  await expect(control).toBeVisible();
  await expect.poll(async () => control.evaluate((element) => {
    const style = getComputedStyle(element);
    const parse = (color: string) => color.match(/[\d.]+/g)?.map(Number) ?? [];
    const composite = (front: number[], back: number[]) => back.map((value, index) =>
      (front[index] ?? 0) * (front[3] ?? 1) + value * (1 - (front[3] ?? 1)));
    const ancestors: Element[] = [];
    for (let node: Element | null = element; node; node = node.parentElement) ancestors.unshift(node);
    const backgroundColor = ancestors.reduce((color, node) =>
      composite(parse(getComputedStyle(node).backgroundColor), color), [255, 255, 255]);
    const luminance = (color: number[]) => {
      const [r, g, b] = color.slice(0, 3).map((value) => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
    };
    const foreground = luminance(composite(parse(style.color), backgroundColor));
    const background = luminance(backgroundColor);
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  }), { timeout: 3000 }).toBeGreaterThanOrEqual(4.5);
}
