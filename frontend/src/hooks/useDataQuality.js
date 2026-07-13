import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "../api/queryKeys";
import { getDataQuality } from "../api/stocks";


export function useDataQuality(symbol, open, pollSeconds = 5) {
  const query = useQuery({
    queryKey: queryKeys.dataQuality(symbol),
    queryFn: ({ signal }) => getDataQuality(symbol, signal),
    enabled: Boolean(open && symbol),
    refetchInterval: open ? pollSeconds * 1000 : false,
    refetchIntervalInBackground: false,
    placeholderData: (previous) => previous,
  });
  return {
    quality: query.data || null,
    loading: query.isPending || query.isFetching,
    error: query.error?.message || "",
    reload: query.refetch,
  };
}
