import React, { useCallback, useMemo, useRef, useState } from "react";
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Code,
  Collapse,
  Divider,
  Group,
  MultiSelect,
  ScrollArea,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import {
  faCaretDown,
  faClock,
  faDownload,
  faInfoCircle,
  faLayerGroup,
  faRotateLeft,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { UseQueryResult } from "@tanstack/react-query";
import { ColumnDef } from "@tanstack/react-table";
import { isString } from "lodash";
import {
  useClearProviderRejection,
  useEpisodeSeasonPreview,
  useReplaceEpisodeSeasonSubtitles,
  useSystemProviders,
} from "@/apis/hooks/providers";
import { ManualSearchOptions } from "@/apis/raw/providers";
import { Action } from "@/components";
import Language from "@/components/bazarr/Language";
import StateIcon from "@/components/StateIcon";
import PageTable from "@/components/tables/PageTable";
import { withModal } from "@/modules/modals";
import { GetItemId } from "@/utilities";

type SupportType = Item.Movie | Item.Episode;

const unverifiedReasons = new Set([
  "timing_not_confirmed",
  "corrected_timing_not_confirmed",
  "insufficient_speech",
  "validation_timeout",
  "validation_unavailable",
  "reference_inconclusive",
  "reference_unavailable",
  "script_inconclusive",
]);

function isUnverified(rejection: SearchResultType["rejection"]) {
  return rejection ? unverifiedReasons.has(rejection.reason) : false;
}

function rejectionDescription(rejection: SearchResultType["rejection"]) {
  if (!rejection) return "This subtitle did not match the video.";
  if (rejection.reason === "script_inconclusive")
    return "Could not confirm whether this subtitle is Simplified or Traditional Chinese. You can retry.";
  if (isUnverified(rejection))
    return "Available evidence could not confirm subtitle timing. You can retry.";
  if (rejection.detail?.trim()) return rejection.detail.trim();
  /* eslint-disable camelcase -- Persisted backend rejection reason codes. */
  const reasons: Record<string, string> = {
    obvious_fragment:
      "The subtitle contains only a short fragment of the video.",
    parse_loss:
      "The subtitle parser could not read a substantial part of the file.",
    invalid_format: "The downloaded file is not a supported text subtitle.",
    no_dialogue: "The subtitle contains no usable dialogue cues.",
    script_mismatch:
      "The subtitle does not match the requested Chinese script.",
    insufficient_speech:
      "The original audio contains too little dialogue to confirm timing.",
    timing_not_confirmed:
      "Available evidence could not confirm subtitle timing.",
    timing_mismatch:
      "Independent timing evidence confirms this subtitle does not match the video.",
    corrected_timing_not_confirmed:
      "Available evidence could not confirm the corrected subtitle timing.",
    validation_timeout:
      "Original-audio timing validation exceeded its time limit.",
    validation_unavailable: "Original-audio timing validation is unavailable.",
    no_eligible_subtitle: "The package contains no matching subtitle.",
    download_failed: "The subtitle provider could not download this subtitle.",
  };
  /* eslint-enable camelcase */
  return reasons[rejection.reason] || rejection.reason.replaceAll("_", " ");
}

interface Props<T extends SupportType> {
  download: (item: T, result: SearchResultType) => Promise<void>;
  query: (
    id?: number,
    options?: ManualSearchOptions,
    searchId?: number,
  ) => UseQueryResult<SearchResultType[] | undefined>;
  item: T;
  defaultKeyword?: string;
}

function ManualSearchView<T extends SupportType>(props: Props<T>) {
  const { download, query: useSearch, item } = props;

  const [keyword, setKeyword] = useState("");
  const defaultKeyword =
    props.defaultKeyword ?? ("radarrId" in item ? item.title : "");
  const [providers, setProviders] = useState<string[]>([]);
  const [allProviders, setAllProviders] = useState(true);
  const [submitted, setSubmitted] = useState<{
    options: ManualSearchOptions;
    id: number;
  } | null>(null);
  const providerStatus = useSystemProviders();
  const clearRejection = useClearProviderRejection();
  const previewSeason = useEpisodeSeasonPreview();
  const replaceSeason = useReplaceEpisodeSeasonSubtitles();
  const seasonRequestId = useRef(0);
  const [seasonCandidate, setSeasonCandidate] =
    useState<SearchResultType | null>(null);
  const [seasonPreview, setSeasonPreview] =
    useState<SeasonReplacementPreview | null>(null);
  const [seasonError, setSeasonError] = useState<string | null>(null);
  const [seasonQueued, setSeasonQueued] = useState<{
    jobId: number;
    season: number;
    total: number;
    subtitle: string;
  } | null>(null);
  const [queuedSubtitle, setQueuedSubtitle] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const providerOptions = useMemo(
    () =>
      (providerStatus.data ?? []).map(({ name, status }) => ({
        value: name,
        label: status === "Good" ? name : `${name} (${status})`,
        disabled: status !== "Good",
      })),
    [providerStatus.data],
  );

  const itemId = useMemo(() => GetItemId(item), [item]);

  const results = useSearch(
    submitted ? itemId : undefined,
    submitted?.options,
    submitted?.id,
  );

  const haveResult = results.data !== undefined && !results.isError;

  const search = useCallback(() => {
    if (results.isFetching || (!allProviders && providers.length === 0)) return;
    setQueuedSubtitle("");
    setActionError(null);
    seasonRequestId.current += 1;
    setSeasonCandidate(null);
    setSeasonPreview(null);
    setSeasonError(null);
    setSubmitted((current) => ({
      options: {
        keyword: keyword.trim() || undefined,
        providers: allProviders ? undefined : [...providers].sort(),
      },
      id: (current?.id ?? 0) + 1,
    }));
  }, [allProviders, keyword, providers, results.isFetching]);

  const showSeasonPreview = useCallback(
    async (result: SearchResultType) => {
      if (!("sonarrEpisodeId" in item) || result.provider !== "subhd") return;
      const requestId = ++seasonRequestId.current;
      setSeasonCandidate(result);
      setSeasonPreview(null);
      setSeasonError(null);
      try {
        const preview = await previewSeason.mutateAsync({
          episodeId: item.sonarrEpisodeId,
          subtitle: String(result.subtitle),
        });
        if (seasonRequestId.current === requestId) setSeasonPreview(preview);
      } catch (error) {
        if (seasonRequestId.current === requestId)
          setSeasonError(
            error instanceof Error
              ? error.message
              : "Could not load season replacement preview.",
          );
      }
    },
    [item, previewSeason],
  );

  const cancelSeasonPreview = useCallback(() => {
    seasonRequestId.current += 1;
    setSeasonCandidate(null);
    setSeasonPreview(null);
    setSeasonError(null);
  }, []);

  const confirmSeasonReplacement = useCallback(async () => {
    if (
      !("sonarrEpisodeId" in item) ||
      !seasonCandidate ||
      !seasonPreview ||
      replaceSeason.isPending
    )
      return;
    setSeasonError(null);
    try {
      const queued = await replaceSeason.mutateAsync({
        episodeId: item.sonarrEpisodeId,
        form: {
          subtitle: String(seasonCandidate.subtitle),
          hi: seasonCandidate.hearing_impaired,
          forced: seasonCandidate.forced,
          // eslint-disable-next-line camelcase -- Backend request field.
          original_format: seasonCandidate.original_format,
        },
      });
      setSeasonQueued({
        jobId: queued.job_id,
        season: seasonPreview.season,
        total: seasonPreview.total,
        subtitle: String(seasonCandidate.subtitle),
      });
      cancelSeasonPreview();
    } catch (error) {
      setSeasonError(
        error instanceof Error
          ? error.message
          : "Could not queue season replacement.",
      );
    }
  }, [
    item,
    seasonCandidate,
    seasonPreview,
    replaceSeason,
    cancelSeasonPreview,
  ]);

  const ReleaseInfoCell = React.memo(
    ({
      releaseInfo,
      tags,
      rejected,
      rejection,
    }: {
      releaseInfo: string[];
      tags?: string[] | null;
      rejected?: boolean;
      rejection?: SearchResultType["rejection"];
    }) => {
      const [open, setOpen] = useState(false);

      const items = useMemo(
        () => releaseInfo.slice(1).map((v, idx) => <Text key={idx}>{v}</Text>),
        [releaseInfo],
      );
      const labels = useMemo(
        () =>
          Array.isArray(tags)
            ? [
                ...new Set(
                  tags
                    .filter((tag) => typeof tag === "string")
                    .map((tag) => tag.trim())
                    .filter(Boolean),
                ),
              ]
            : [],
        [tags],
      );

      return (
        <Stack gap={4}>
          {releaseInfo.length === 0 ? (
            <Text c="dimmed">Cannot get release info</Text>
          ) : (
            <Stack gap={0} onClick={() => setOpen((o) => !o)}>
              <Text className="table-primary" span>
                {releaseInfo[0]}
                {releaseInfo.length > 1 && (
                  <FontAwesomeIcon
                    icon={faCaretDown}
                    rotation={open ? 180 : undefined}
                  ></FontAwesomeIcon>
                )}
              </Text>
              <Collapse expanded={open}>
                <>{items}</>
              </Collapse>
            </Stack>
          )}
          {labels.length > 0 && (
            <Group gap={4} wrap="wrap">
              {labels.map((label) => (
                <Badge key={label} size="xs" variant="light" color="gray">
                  {label}
                </Badge>
              ))}
            </Group>
          )}
          {rejected && (
            <Stack gap={2}>
              <Badge
                size="xs"
                variant="light"
                color={isUnverified(rejection) ? "yellow" : "red"}
              >
                {isUnverified(rejection) ? "Not verified" : "Not matched"}
              </Badge>
              <Text
                size="xs"
                c={isUnverified(rejection) ? "dimmed" : "red"}
                title={rejectionDescription(rejection)}
              >
                {rejectionDescription(rejection)}
              </Text>
            </Stack>
          )}
        </Stack>
      );
    },
  );

  const columns = useMemo<ColumnDef<SearchResultType>[]>(
    () => [
      {
        header: "Score",
        accessorKey: "score",
        cell: ({
          row: {
            original: { score },
          },
        }) => {
          return <Text className="table-no-wrap">{score}%</Text>;
        },
      },
      {
        header: "Language",
        accessorKey: "language",
        cell: ({
          row: {
            original: { language, hearing_impaired: hi, forced },
          },
        }) => {
          const lang: Language.Info = {
            code2: language,
            hi: hi === "True",
            forced: forced === "True",
            name: "",
          };
          return (
            <Badge>
              <Language.Text value={lang}></Language.Text>
            </Badge>
          );
        },
      },
      {
        header: "Provider",
        accessorKey: "provider",
        cell: ({
          row: {
            original: { provider, url },
          },
        }) => {
          const value = provider;

          if (url) {
            return (
              <Anchor
                className="table-no-wrap"
                href={url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {value}
              </Anchor>
            );
          } else {
            return <Text>{value}</Text>;
          }
        },
      },
      {
        header: "Release",
        accessorKey: "release_info",
        cell: ({
          row: {
            original: { release_info: releaseInfo, tags, rejected, rejection },
          },
        }) => {
          return (
            <ReleaseInfoCell
              releaseInfo={releaseInfo}
              tags={tags}
              rejected={rejected}
              rejection={rejection}
            />
          );
        },
      },
      {
        header: "Uploader",
        accessorKey: "uploader",
        cell: ({
          row: {
            original: { uploader },
          },
        }) => {
          return <Text className="table-no-wrap">{uploader ?? "-"}</Text>;
        },
      },
      {
        header: "Match",
        accessorKey: "matches",
        cell: (row) => {
          const { matches, dont_matches: dont } = row.row.original;
          return (
            <StateIcon
              matches={matches}
              dont={dont}
              isHistory={false}
            ></StateIcon>
          );
        },
      },
      {
        header: "Get",
        accessorKey: "subtitle",
        cell: ({ row }) => {
          const result = row.original;
          const blocked =
            result.rejected === true && !isUnverified(result.rejection);
          const subtitleId = String(result.subtitle);
          const isQueued = queuedSubtitle === subtitleId;
          return (
            <Group gap={4} wrap="nowrap">
              <Action
                label={isQueued ? "Queued" : "Download"}
                icon={isQueued ? faClock : faDownload}
                color="gray"
                disabled={item === null || blocked || isQueued}
                onClick={async () => {
                  if (!item || blocked) return;
                  setActionError(null);
                  try {
                    // HTTP 204 confirms queue submission, not a saved subtitle.
                    await download(item, result);
                    setQueuedSubtitle(subtitleId);
                  } catch (error) {
                    setActionError(
                      error instanceof Error
                        ? error.message
                        : "Could not queue subtitle download.",
                    );
                  }
                }}
              />
              {blocked && (
                <Action
                  label="Allow retry"
                  icon={faRotateLeft}
                  color="gray"
                  disabled={itemId === undefined || clearRejection.isPending}
                  isLoading={
                    clearRejection.isPending &&
                    clearRejection.variables?.subtitle === subtitleId
                  }
                  onClick={async () => {
                    if (itemId === undefined) return;
                    setActionError(null);
                    try {
                      await clearRejection.mutateAsync({
                        mediaType: "radarrId" in item ? "movie" : "episode",
                        id: itemId,
                        subtitle: subtitleId,
                      });
                      if (queuedSubtitle === subtitleId) setQueuedSubtitle("");
                      await results.refetch();
                    } catch (error) {
                      setActionError(
                        error instanceof Error
                          ? error.message
                          : "Could not allow subtitle retry.",
                      );
                    }
                  }}
                />
              )}
              {"sonarrEpisodeId" in item && result.provider === "subhd" && (
                <Action
                  label="Replace season"
                  icon={faLayerGroup}
                  color="gray"
                  disabled={
                    previewSeason.isPending ||
                    replaceSeason.isPending ||
                    seasonQueued?.subtitle === subtitleId
                  }
                  isLoading={
                    previewSeason.isPending &&
                    previewSeason.variables?.subtitle === subtitleId
                  }
                  onClick={() => void showSeasonPreview(result)}
                />
              )}
            </Group>
          );
        },
      },
    ],
    [
      download,
      item,
      itemId,
      ReleaseInfoCell,
      queuedSubtitle,
      clearRejection,
      results,
      previewSeason,
      replaceSeason,
      seasonQueued,
      showSeasonPreview,
    ],
  );

  const bSceneNameAvailable =
    isString(item.sceneName) && item.sceneName.length !== 0;

  const searchButtonText = useMemo(() => {
    if (results.isFetching) {
      return "Searching";
    }

    return submitted ? "Search Again" : "Search";
  }, [results.isFetching, submitted]);

  return (
    <Stack>
      <Alert
        title="Resource"
        color="gray"
        icon={<FontAwesomeIcon icon={faInfoCircle}></FontAwesomeIcon>}
      >
        <Text size="sm">{item?.path}</Text>
        <Divider hidden={!bSceneNameAvailable} my="xs"></Divider>
        <Code hidden={!bSceneNameAvailable}>{item?.sceneName}</Code>
      </Alert>
      <TextInput
        label="Search keyword"
        description="Use a title or alternative name. Leave blank for automatic matching."
        placeholder={defaultKeyword || "Movie or series title"}
        value={keyword}
        maxLength={200}
        disabled={results.isFetching}
        onChange={(event) => setKeyword(event.currentTarget.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            search();
          }
        }}
      />
      <Checkbox
        label="All enabled providers"
        checked={allProviders}
        disabled={results.isFetching}
        onChange={(event) => setAllProviders(event.currentTarget.checked)}
      />
      <MultiSelect
        label="Subtitle providers"
        description="Select providers for this search. Keywords apply to SubHD, R3Sub, Assrt and Zimuku."
        placeholder="Choose providers"
        searchable
        clearable
        data={providerOptions}
        value={providers}
        disabled={
          allProviders || providerStatus.isFetching || results.isFetching
        }
        onChange={setProviders}
        error={
          !allProviders && providers.length === 0
            ? "Select at least one provider"
            : undefined
        }
      />
      {results.isError && (
        <Alert color="red" title="Search failed">
          {results.error.message}
        </Alert>
      )}
      {actionError && (
        <Alert color="red" title="Subtitle action failed">
          {actionError}
        </Alert>
      )}
      <Collapse expanded={haveResult && !results.isFetching}>
        <PageTable
          autoScroll={false}
          tableStyles={{ emptyText: "No result", placeholder: 10 }}
          columns={columns}
          data={results.data ?? []}
        ></PageTable>
      </Collapse>
      {seasonCandidate && (
        <Alert
          color={seasonError ? "red" : "yellow"}
          title="Season replacement preview"
        >
          <Stack gap="xs">
            {previewSeason.isPending && (
              <Text size="sm">Loading season preview…</Text>
            )}
            {seasonError && (
              <Text size="sm" c="red">
                {seasonError}
              </Text>
            )}
            {seasonPreview && (
              <>
                <Text size="sm">
                  {seasonPreview.title} — Season {seasonPreview.season}:{" "}
                  {seasonPreview.total} episodes
                </Text>
                <Group gap="xs">
                  <Text size="sm">Subtitle language:</Text>
                  <Badge>
                    <Language.Text
                      value={{
                        code2:
                          seasonPreview.language ?? seasonCandidate.language,
                        name: "",
                        hi: seasonCandidate.hearing_impaired === "True",
                        forced: seasonCandidate.forced === "True",
                      }}
                    />
                  </Badge>
                </Group>
                <Text size="sm">
                  Replaces existing external subtitles in this language after
                  backing them up. Each episode is validated against its
                  original audio. Episodes that fail validation keep their
                  existing subtitles. Other seasons are not changed.
                </Text>
                <ScrollArea.Autosize mah={160}>
                  <Stack gap={2}>
                    {seasonPreview.episodes.map((episode) => (
                      <Text size="xs" key={episode.episode_id}>
                        E{String(episode.episode).padStart(2, "0")} —{" "}
                        {episode.title} ({episode.existing_subtitles} existing
                        external subtitles)
                      </Text>
                    ))}
                  </Stack>
                </ScrollArea.Autosize>
              </>
            )}
            <Group gap="xs" justify="flex-end">
              <Button
                variant="default"
                size="xs"
                disabled={replaceSeason.isPending}
                onClick={cancelSeasonPreview}
              >
                Cancel
              </Button>
              {seasonPreview && (
                <Button
                  color="yellow"
                  size="xs"
                  loading={replaceSeason.isPending}
                  disabled={
                    seasonPreview.total === 0 || previewSeason.isPending
                  }
                  onClick={() => void confirmSeasonReplacement()}
                >
                  Replace season {seasonPreview.season} ({seasonPreview.total}{" "}
                  episodes)
                </Button>
              )}
            </Group>
          </Stack>
        </Alert>
      )}
      {seasonQueued && (
        <Alert color="gray" title="Season replacement queued">
          <Text size="sm">
            Season {seasonQueued.season}: {seasonQueued.total} episodes queued
            as job #{seasonQueued.jobId}. Follow progress in Jobs Manager.
          </Text>
        </Alert>
      )}
      <Divider></Divider>
      <Button
        loading={results.isFetching}
        disabled={!allProviders && providers.length === 0}
        fullWidth
        onClick={search}
      >
        {searchButtonText}
      </Button>
    </Stack>
  );
}

export const MovieSearchModal = withModal<Props<Item.Movie>>(
  ManualSearchView,
  "movie-manual-search",
  { title: "Search Subtitles", size: "calc(100vw - 4rem)" },
);
export const EpisodeSearchModal = withModal<Props<Item.Episode>>(
  ManualSearchView,
  "episode-manual-search",
  { title: "Search Subtitles", size: "calc(100vw - 4rem)" },
);
