import { Text, Tooltip } from "@mantine/core";

const providerNames: Record<string, string> = {
  opensubtitlescom: "OpenSubtitles.com",
  subhd: "SubHD",
  r3sub: "R3Sub",
  assrt: "Assrt",
  zimuku: "Zimuku",
};

export function subtitleSourceLabel(subtitle: Subtitle): string {
  switch (subtitle.source_type) {
    case "provider": {
      const provider = subtitle.source?.trim();
      return provider
        ? (providerNames[provider.toLowerCase()] ?? provider)
        : "Local / Unknown";
    }
    case "embedded":
      return "Embedded";
    case "extracted":
      return "Extracted from video";
    case "uploaded":
      return "Manual upload";
    case "translated":
      return "Translated";
    case "unknown":
      return "Local / Unknown";
    default:
      return subtitle.embedded_track_id != null
        ? "Embedded"
        : "Local / Unknown";
  }
}

interface Props {
  subtitle: Subtitle;
  compact?: boolean;
  missing?: boolean;
}

export default function SubtitleSource({
  subtitle,
  compact = false,
  missing = false,
}: Props) {
  if (missing) {
    return compact ? null : <Text c="dimmed">-</Text>;
  }
  const label = subtitleSourceLabel(subtitle);
  const detail =
    label === "Embedded"
      ? "Subtitle track embedded in the video."
      : label === "Local / Unknown"
        ? "No matching subtitle source record was found in Bazarr history."
        : `Source recorded in Bazarr subtitle history: ${label}.`;
  return (
    <Tooltip label={detail} withArrow>
      <Text
        component="span"
        display="block"
        maw={compact ? 90 : 150}
        truncate
        c={compact ? "dimmed" : undefined}
        fz={compact ? 10 : "sm"}
        lh={compact ? 1.15 : undefined}
        ta={compact ? "center" : undefined}
      >
        {label}
      </Text>
    </Tooltip>
  );
}
