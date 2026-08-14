const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Route params that cannot be a platform id must not become API calls. */
export function asId(value: string | undefined): string | null {
  return value !== undefined && UUID.test(value) ? value : null;
}
