import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  generateAIAnalysis,
  getLatestAIAnalysis,
  submitAIAnalysisFeedback,
} from "../api/ai";
import { queryKeys } from "../api/queryKeys";


export function hasRunningAiAnalysis(response) {
  return Boolean(response?.running?.unheld || response?.running?.held);
}


export function mergeAiAnalysisResponse(current, next) {
  if (!current || !next) return next;
  return {
    ...next,
    analyses: {
      unheld: next.analyses?.unheld || current.analyses?.unheld || null,
      held: next.analyses?.held || current.analyses?.held || null,
    },
    rule_based: {
      unheld: next.rule_based?.unheld || current.rule_based?.unheld || null,
      held: next.rule_based?.held || current.rule_based?.held || null,
    },
  };
}


export function useAIAnalysis(stock, open, pollSeconds = 10) {
  const queryClient = useQueryClient();
  const queryKey = queryKeys.aiAnalysis(stock.symbol);
  const cachedResponse = queryClient.getQueryData(queryKey);
  const [actionError, setActionError] = useState("");
  const [feedbackStatus, setFeedbackStatus] = useState("");

  const generationMutation = useMutation({
    mutationKey: ["ai-analysis", stock.symbol, "generate"],
    mutationFn: ({ forceRefresh }) => generateAIAnalysis(stock.symbol, forceRefresh),
    onSuccess: (payload) => {
      queryClient.setQueryData(queryKey, (current) => mergeAiAnalysisResponse(current, payload));
    },
  });
  const feedbackMutation = useMutation({
    mutationKey: ["ai-analysis", stock.symbol, "feedback"],
    mutationFn: ({ mode, payload }) => submitAIAnalysisFeedback(stock.symbol, mode, payload),
  });
  const runningFromCache = hasRunningAiAnalysis(cachedResponse);
  const analysisQuery = useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      const payload = await getLatestAIAnalysis(stock.symbol, signal);
      return mergeAiAnalysisResponse(queryClient.getQueryData(queryKey), payload);
    },
    enabled: Boolean(open || runningFromCache || generationMutation.isPending),
    staleTime: 5000,
    refetchInterval: (query) => hasRunningAiAnalysis(query.state.data) ? pollSeconds * 1000 : false,
    refetchIntervalInBackground: false,
    placeholderData: (previous) => previous,
  });
  const analysisResponse = analysisQuery.data || cachedResponse || null;
  const running = generationMutation.isPending || hasRunningAiAnalysis(analysisResponse);

  const generate = useCallback(async () => {
    if (running) return null;
    setActionError("");
    const hasExistingAnalysis = Boolean(
      analysisResponse?.analyses?.unheld || analysisResponse?.analyses?.held,
    );
    try {
      return await generationMutation.mutateAsync({ forceRefresh: hasExistingAnalysis });
    } catch (error) {
      setActionError(error.message);
      return null;
    }
  }, [analysisResponse, generationMutation, running]);

  const submitFeedback = useCallback(async (mode, analysisId, rating, tags = []) => {
    if (!analysisId || feedbackMutation.isPending) return null;
    setFeedbackStatus("");
    try {
      const result = await feedbackMutation.mutateAsync({
        mode,
        payload: { analysis_id: analysisId, rating, tags },
      });
      setFeedbackStatus("已記錄回饋");
      return result;
    } catch (error) {
      setFeedbackStatus(error.message);
      return null;
    }
  }, [feedbackMutation]);

  return {
    analysisResponse,
    loading: generationMutation.isPending || (open && analysisQuery.isFetching && !analysisResponse),
    running,
    error: actionError || analysisQuery.error?.message || "",
    generate,
    submitFeedback,
    feedbackStatus,
    feedbackSubmitting: feedbackMutation.isPending,
  };
}
