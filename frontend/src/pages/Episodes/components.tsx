import { FunctionComponent, useMemo, useState } from "react";
import { Badge, MantineColor, Tooltip } from "@mantine/core";
import { useEpisodeSubtitleModification } from "@/apis/hooks";
import Language from "@/components/bazarr/Language";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { toPython } from "@/utilities";

interface Props {
  seriesId: number;
  episodeId: number;
  missing?: boolean;
  subtitle: Subtitle;
}

export const Subtitle: FunctionComponent<Props> = ({
  seriesId,
  episodeId,
  missing = false,
  subtitle,
}) => {
  const { remove, download } = useEpisodeSubtitleModification();

  const [opened, setOpen] = useState(false);

  const embedded = subtitle.path === null && subtitle.embedded_track_id != null;
  const disabled = subtitle.path === null;

  const variant: MantineColor | undefined = useMemo(() => {
    if (opened && !disabled) {
      return "highlight";
    } else if (missing) {
      return "warning";
    } else if (disabled) {
      return "disabled";
    }
  }, [disabled, missing, opened]);

  const selections = useMemo<FormType.ModifySubtitle[]>(() => {
    const list: FormType.ModifySubtitle[] = [];

    if (subtitle.path || embedded) {
      list.push({
        id: episodeId,
        type: "episode",
        language: subtitle.code2,
        path: subtitle.path ?? "",
        embeddedTrackId: subtitle.embedded_track_id ?? undefined,
        forced: toPython(subtitle.forced),
        hi: toPython(subtitle.hi),
      });
    }

    return list;
  }, [
    embedded,
    episodeId,
    subtitle.code2,
    subtitle.embedded_track_id,
    subtitle.forced,
    subtitle.hi,
    subtitle.path,
  ]);

  const ctx = (
    <Badge variant={variant}>
      <Language.Text value={subtitle} long={false}></Language.Text>
    </Badge>
  );

  return (
    <SubtitleToolsMenu
      menu={{
        trigger: "hover",
        onOpen: () => setOpen(true),
        onClose: () => setOpen(false),
      }}
      selections={selections}
      onAction={async (action) => {
        if (action === "search") {
          await download.mutateAsync({
            seriesId,
            episodeId,
            form: {
              language: subtitle.code2,
              hi: subtitle.hi,
              forced: subtitle.forced,
            },
          });
        } else if (action === "delete" && subtitle.path) {
          await remove.mutateAsync({
            seriesId,
            episodeId,
            form: {
              language: subtitle.code2,
              hi: subtitle.hi,
              forced: subtitle.forced,
              path: subtitle.path,
            },
          });
        }
      }}
    >
      {embedded ? (
        <Tooltip.Floating label="Embedded Subtitle">{ctx}</Tooltip.Floating>
      ) : (
        ctx
      )}
    </SubtitleToolsMenu>
  );
};
