import React, { FunctionComponent, useEffect, useMemo } from "react";
import {
  Button,
  Divider,
  MantineColor,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import {
  faCheck,
  faCircleNotch,
  faInfoCircle,
  faTimes,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { isString, uniqBy } from "lodash";
import {
  useEpisodesBySeriesId,
  useEpisodeSubtitleModification,
  useLanguages,
  useSubtitleInfos,
} from "@/apis/hooks";
import { subtitlesTypeOptions } from "@/components/forms/uploadFormSelectorTypes";
import { Action, Selector } from "@/components/inputs";
import SimpleTable from "@/components/tables/SimpleTable";
import TextPopover from "@/components/TextPopover";
import { useModals, withModal } from "@/modules/modals";
import { useArrayAction, useSelectorOptions } from "@/utilities";
import FormUtils from "@/utilities/form";
import {
  useLanguageProfileBy,
  useProfileItemsToLanguages,
} from "@/utilities/languages";
import { detectSubtitleLanguage } from "@/utilities/subtitleLanguage";

type SubtitleFile = {
  file: File;
  language: Language.Info | null;
  forced: boolean;
  hi: boolean;
  episode: Item.Episode | null;
  validateResult?: SubtitleValidateResult;
};

type SubtitleValidateResult = {
  state: "valid" | "warning" | "error";
  messages?: string;
};

const validator = (file: SubtitleFile): SubtitleValidateResult => {
  if (file.language === null) {
    return {
      state: "error",
      messages: "Language is not selected",
    };
  } else if (file.episode === null) {
    return {
      state: "error",
      messages: "Episode is not selected",
    };
  } else {
    const { subtitles } = file.episode;
    const existing = subtitles.find(
      (v) => v.code2 === file.language?.code2 && isString(v.path),
    );
    if (existing !== undefined) {
      return {
        state: "warning",
        messages: "Override existing subtitle",
      };
    }
  }

  return {
    state: "valid",
  };
};

interface Props {
  files: File[];
  series: Item.Series;
  onComplete?: VoidFunction;
}

const SeriesUploadForm: FunctionComponent<Props> = ({
  series,
  files,
  onComplete,
}) => {
  const modals = useModals();
  const episodes = useEpisodesBySeriesId(series.sonarrSeriesId);
  const episodeOptions = useSelectorOptions(
    episodes.data ?? [],
    (v) => `(${v.season}x${v.episode}) ${v.title}`,
    (v) => v.sonarrEpisodeId.toString(),
  );

  const profile = useLanguageProfileBy(series.profileId);
  const profileLanguages = useProfileItemsToLanguages(profile);
  const allLanguages = useLanguages();
  const languages = useMemo(
    () =>
      uniqBy(
        [
          ...profileLanguages,
          ...(allLanguages.data?.map<Language.Info>((language) => ({
            code2: language.code2,
            name: language.name,
          })) ?? []),
        ],
        "code2",
      ),
    [allLanguages.data, profileLanguages],
  );
  const languageOptions = useSelectorOptions(
    uniqBy(languages, "code2"),
    (v) => v.name,
    (v) => v.code2,
  );

  const defaultLanguage = useMemo(
    () => (profileLanguages.length > 0 ? profileLanguages[0] : null),
    [profileLanguages],
  );

  const form = useForm({
    initialValues: {
      files: files
        .map<SubtitleFile>((file) => ({
          file,
          language: defaultLanguage,
          forced: defaultLanguage?.forced ?? false,
          hi: defaultLanguage?.hi ?? false,
          episode: null,
        }))
        .map<SubtitleFile>((file) => ({
          ...file,
          validateResult: validator(file),
        })),
    },
    validate: {
      files: FormUtils.validation(
        (values: SubtitleFile[]) =>
          values.find(
            (v: SubtitleFile) =>
              v.language === null ||
              v.episode === null ||
              v.validateResult === undefined ||
              v.validateResult.state === "error",
          ) === undefined,
        "Some files cannot be uploaded, please check",
      ),
    },
  });

  const action = useArrayAction<SubtitleFile>((fn) => {
    form.setValues((values) => {
      const newFiles = fn(values.files ?? []);
      newFiles.forEach((v) => {
        v.validateResult = validator(v);
      });
      return { ...values, files: newFiles };
    });
  });

  const names = useMemo(() => files.map((v) => v.name), [files]);
  const infos = useSubtitleInfos(names);
  const detectedLanguages = useMemo(
    () => Promise.all(files.map((file) => detectSubtitleLanguage(file))),
    [files],
  );

  // Auto assign episode and language when the filename or content is clear.
  useEffect(() => {
    if (infos.data !== undefined) {
      void detectedLanguages.then((languageCodes) => {
        action.update((item) => {
          const info = infos.data.find((v) => v.filename === item.file.name);
          const fileIndex = files.findIndex((file) => file === item.file);
          const detectedCode =
            languageCodes[fileIndex] ?? info?.subtitle_language;
          const language =
            languages.find((value) => value.code2 === detectedCode) ??
            item.language;

          if (!info) return { ...item, language };

          const episodeCandidates =
            episodes.data?.filter((value) => value.episode === info.episode) ??
            [];
          const episode =
            episodeCandidates.find((value) => value.season === info.season) ??
            (info.season === 0 && episodeCandidates.length === 1
              ? episodeCandidates[0]
              : item.episode);

          return { ...item, episode, language };
        });
      });
    }
  }, [action, detectedLanguages, episodes.data, files, infos.data, languages]);

  const ValidateResultCell = ({
    validateResult,
  }: {
    validateResult: SubtitleValidateResult | undefined;
  }) => {
    const icon = useMemo(() => {
      switch (validateResult?.state) {
        case "valid":
          return faCheck;
        case "warning":
          return faInfoCircle;
        case "error":
          return faTimes;
        default:
          return faCircleNotch;
      }
    }, [validateResult?.state]);

    const color = useMemo<MantineColor | undefined>(() => {
      switch (validateResult?.state) {
        case "valid":
          return "green";
        case "warning":
          return "yellow";
        case "error":
          return "red";
        default:
          return undefined;
      }
    }, [validateResult?.state]);

    return (
      <TextPopover text={validateResult?.messages}>
        <Text c={color} inline>
          <FontAwesomeIcon icon={icon}></FontAwesomeIcon>
        </Text>
      </TextPopover>
    );
  };

  const columns = useMemo<ColumnDef<SubtitleFile>[]>(
    () => [
      {
        id: "validateResult",
        cell: ({
          row: {
            original: { validateResult },
          },
        }) => {
          return <ValidateResultCell validateResult={validateResult} />;
        },
      },
      {
        header: "File",
        id: "filename",
        accessorKey: "file",
        cell: ({
          row: {
            original: {
              file: { name },
            },
          },
        }) => {
          return <Text className="table-primary">{name}</Text>;
        },
      },
      {
        header: () => (
          <Selector
            {...languageOptions}
            value={null}
            placeholder="Language"
            onChange={(value) => {
              if (value) {
                action.update((item) => {
                  return { ...item, language: value };
                });
              }
            }}
          ></Selector>
        ),
        accessorKey: "language",
        cell: ({ row: { original, index } }) => {
          return (
            <Selector
              {...languageOptions}
              className="table-select"
              value={original.language}
              onChange={(item) => {
                action.mutate(index, { ...original, language: item });
              }}
            ></Selector>
          );
        },
      },
      {
        header: () => (
          <Selector
            options={subtitlesTypeOptions}
            value={null}
            placeholder="Type"
            onChange={(value) => {
              if (value) {
                action.update((item) => {
                  switch (value) {
                    case "hi":
                      return { ...item, hi: true, forced: false };
                    case "forced":
                      return { ...item, hi: false, forced: true };
                    case "normal":
                      return { ...item, hi: false, forced: false };
                    default:
                      return item;
                  }
                });
              }
            }}
          ></Selector>
        ),
        accessorKey: "type",
        cell: ({ row: { original, index } }) => {
          return (
            <Select
              value={
                subtitlesTypeOptions.find((s) => {
                  if (original.hi) {
                    return s.value === "hi";
                  }

                  if (original.forced) {
                    return s.value === "forced";
                  }

                  return s.value === "normal";
                })?.value
              }
              data={subtitlesTypeOptions}
              onChange={(value) => {
                if (value) {
                  action.mutate(index, {
                    ...original,
                    hi: value === "hi",
                    forced: value === "forced",
                  });
                }
              }}
            ></Select>
          );
        },
      },
      {
        id: "episode",
        header: "Episode",
        accessorKey: "episode",
        cell: ({ row: { original, index } }) => {
          return (
            <Selector
              {...episodeOptions}
              searchable
              className="table-select"
              value={original.episode}
              onChange={(item) => {
                action.mutate(index, { ...original, episode: item });
              }}
            ></Selector>
          );
        },
      },
      {
        id: "action",
        cell: ({ row: { index } }) => {
          return (
            <Action
              label="Remove"
              icon={faTrash}
              c="red"
              onClick={() => action.remove(index)}
            ></Action>
          );
        },
      },
    ],
    [action, episodeOptions, languageOptions],
  );

  const { upload } = useEpisodeSubtitleModification();

  return (
    <form
      onSubmit={form.onSubmit(({ files }) => {
        const { sonarrSeriesId: seriesId } = series;

        for (const value of files) {
          const { file, hi, forced, language, episode } = value;

          if (language === null || episode === null) {
            throw new Error(
              "Invalid language or episode. This shouldn't happen, please report this bug.",
            );
          }

          const { code2 } = language;
          const { sonarrEpisodeId: episodeId } = episode;

          upload.mutate({
            seriesId,
            episodeId,
            form: {
              file,
              language: code2,
              hi,
              forced,
            },
          });
        }

        onComplete?.();
        modals.closeSelf();
      })}
    >
      <Stack className="table-long-break">
        <Text size="sm" c="dimmed">
          Language and episode are detected from each filename and subtitle
          content. Use the selectors to correct a result, or the column header
          to apply one language or type to every file.
        </Text>
        <SimpleTable columns={columns} data={form.values.files}></SimpleTable>
        <Divider></Divider>
        <Button type="submit">Upload</Button>
      </Stack>
    </form>
  );
};

export const SeriesUploadModal = withModal(
  SeriesUploadForm,
  "upload-series-subtitles",
  { title: "Upload Subtitles", size: "xl" },
);

export default SeriesUploadForm;
