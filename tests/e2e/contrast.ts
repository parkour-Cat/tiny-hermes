import { expect, type Locator } from "@playwright/test";

/** WCAG relative luminance for the opaque foreground/background of a control. */
export async function expectReadableControl(control: Locator): Promise<void> {
  await expect(control).toBeVisible();
  await expect.poll(async () => control.evaluate((element) => {
    const style = getComputedStyle(element);
    const luminance = (color: string) => {
      const rgba = color.match(/[\d.]+/g)?.map(Number) ?? [];
      if (rgba.length < 3 || (rgba[3] !== undefined && rgba[3] !== 1)) {
        throw new Error("This contrast check requires opaque computed colors");
      }
      const [r, g, b] = rgba.slice(0, 3).map((value) => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
    };
    const foreground = luminance(style.color);
    const background = luminance(style.backgroundColor);
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  }), { timeout: 3000 }).toBeGreaterThanOrEqual(4.5);
}
