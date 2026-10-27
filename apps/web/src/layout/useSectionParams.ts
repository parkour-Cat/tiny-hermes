import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

/**
 * Search params for a page that is one section of a grouped page.
 *
 * The section is the address's hash (records#usage). useSearchParams' setter
 * rewrites the address without it, which drops the reader back on the first
 * section the moment they touch a filter.
 */
export function useSectionParams(): [URLSearchParams, (next: URLSearchParams) => void] {
  const [params] = useSearchParams();
  const { hash } = useLocation();
  const navigate = useNavigate();
  return [params, (next) => navigate({ search: next.toString(), hash }, { replace: true })];
}
