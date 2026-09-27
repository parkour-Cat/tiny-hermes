import type { SkillUsage } from "../api/types";

/** A usage record as the values `fill` substitutes into its sentence. */
export function usageValues(usage: SkillUsage): Record<string, string> {
  return {
    runs: String(usage.runs),
    completed: String(usage.completed),
    failed: String(usage.failed),
  };
}
