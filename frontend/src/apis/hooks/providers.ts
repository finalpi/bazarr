import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { ManualSearchOptions } from "@/apis/raw/providers";

export function useSystemProviders(history?: boolean) {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Providers, history ?? false],
    queryFn: () => api.providers.providers(history),
  });
}

export function useMoviesProvider(
  radarrId?: number,
  options?: ManualSearchOptions,
  searchId = 0,
) {
  return useQuery({
    queryKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Movies,
      radarrId,
      options,
      searchId,
    ],

    queryFn: () => {
      if (radarrId) {
        return api.providers.movies(radarrId, options);
      }

      return [];
    },

    staleTime: 0,
    enabled: radarrId !== undefined,
    placeholderData: () => undefined,
  });
}

export function useEpisodesProvider(
  episodeId?: number,
  options?: ManualSearchOptions,
  searchId = 0,
) {
  return useQuery({
    queryKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Episodes,
      episodeId,
      options,
      searchId,
    ],

    queryFn: () => {
      if (episodeId) {
        return api.providers.episodes(episodeId, options);
      }

      return [];
    },

    staleTime: 0,
    enabled: episodeId !== undefined,
    placeholderData: () => undefined,
  });
}

export function useResetProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Providers],
    mutationFn: () => api.providers.reset(),

    onSuccess: () => {
      client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Providers],
      });
    },
  });
}

export function useClearProviderRejection() {
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Providers, "clear-rejection"],
    mutationFn: (param: {
      mediaType: "movie" | "episode";
      id: number;
      subtitle: string;
    }) =>
      param.mediaType === "movie"
        ? api.providers.clearMovieRejection(param.id, param.subtitle)
        : api.providers.clearEpisodeRejection(param.id, param.subtitle),
  });
}

export function useEpisodeSeasonPreview() {
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Providers, "season-preview"],
    mutationFn: (param: { episodeId: number; subtitle: string }) =>
      api.providers.previewEpisodeSeasonReplacement(
        param.episodeId,
        param.subtitle,
      ),
  });
}

export function useReplaceEpisodeSeasonSubtitles() {
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Providers, "season-replacement"],
    mutationFn: (param: {
      episodeId: number;
      form: Pick<
        FormType.ManualDownload,
        "subtitle" | "hi" | "forced" | "original_format"
      >;
    }) =>
      api.providers.replaceEpisodeSeasonSubtitles(param.episodeId, param.form),
  });
}

export function useDownloadEpisodeSubtitles() {
  const client = useQueryClient();

  return useMutation({
    mutationKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Subtitles,
      QueryKeys.Episodes,
    ],

    mutationFn: (param: {
      seriesId: number;
      episodeId: number;
      form: FormType.ManualDownload;
    }) =>
      api.providers.downloadEpisodeSubtitle(
        param.seriesId,
        param.episodeId,
        param.form,
      ),

    onSuccess: (_, param) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.Series, param.seriesId],
      });
    },
  });
}

export function useDownloadMovieSubtitles() {
  const client = useQueryClient();

  return useMutation({
    mutationKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Subtitles,
      QueryKeys.Movies,
    ],

    mutationFn: (param: { radarrId: number; form: FormType.ManualDownload }) =>
      api.providers.downloadMovieSubtitle(param.radarrId, param.form),

    onSuccess: (_, param) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies, param.radarrId],
      });
    },
  });
}
