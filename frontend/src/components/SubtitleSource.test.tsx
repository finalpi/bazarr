/* eslint-disable camelcase */
import { ComponentProps } from "react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Subtitle as EpisodeSubtitle } from "@/pages/Episodes/components";
import MovieSubtitleTable from "@/pages/Movies/Details/table";
import { customRender, screen, waitFor, within } from "@/tests";
import SubtitleSource from "./SubtitleSource";
import SubtitleToolsMenu from "./SubtitleToolsMenu";

const actions = vi.hoisted(() => ({
  captureMenu: vi.fn(),
  download: vi.fn().mockResolvedValue(undefined),
  remove: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/modules/socketio", () => ({ default: { initialize: vi.fn() } }));
vi.mock("@/apis/hooks", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useSystemSettings: () => ({
    data: {
      general: {
        theme: "auto",
        page_size: 50,
        embedded_subs_show_desired: false,
      },
    },
  }),
  useLanguages: () => ({ data: [] }),
  useMovieSubtitleModification: () => ({
    download: { mutateAsync: actions.download, isPending: false },
    remove: { mutateAsync: actions.remove },
  }),
  useEpisodeSubtitleModification: () => ({
    download: { mutateAsync: actions.download, isPending: false },
    remove: { mutateAsync: actions.remove },
  }),
}));
vi.mock("./SubtitleToolsMenu", () => ({
  default: (props: ComponentProps<typeof SubtitleToolsMenu>) => {
    actions.captureMenu(props);
    return (
      <div
        onMouseEnter={() => props.menu?.onOpen?.()}
        onMouseLeave={() => props.menu?.onClose?.()}
      >
        {props.children}
      </div>
    );
  },
}));

function subtitle(overrides: Partial<Subtitle> = {}): Subtitle {
  return {
    code2: "zh",
    name: "Chinese",
    forced: false,
    hi: false,
    path: "/media/episode.zh.srt",
    embedded_track_id: null,
    ...overrides,
  };
}

function renderMovie(value: Subtitle, missing: Subtitle[] = []) {
  const movie = {
    radarrId: 42,
    subtitles: [value],
    missing_subtitles: missing,
  } as Item.Movie;
  return customRender(<MovieSubtitleTable movie={movie} />);
}

function menuProps() {
  return actions.captureMenu.mock.lastCall?.[0] as ComponentProps<
    typeof SubtitleToolsMenu
  >;
}

beforeEach(() => {
  actions.captureMenu.mockClear();
  actions.download.mockClear();
  actions.remove.mockClear();
});

describe("existing subtitle sources", () => {
  it.each([
    ["provider", "opensubtitlescom", "OpenSubtitles.com"],
    ["provider", "subhd", "SubHD"],
    ["provider", "r3sub", "R3Sub"],
    ["provider", "assrt", "Assrt"],
    ["provider", "zimuku", "Zimuku"],
    ["provider", "another-provider", "another-provider"],
    ["embedded", null, "Embedded"],
    ["extracted", "embeddedsubtitles", "Extracted from video"],
    ["uploaded", "manual", "Manual upload"],
    ["translated", null, "Translated"],
    ["unknown", null, "Local / Unknown"],
  ] as const)(
    "shows %s source %s in the movie's dedicated source column",
    async (source_type, source, expected) => {
      renderMovie(subtitle({ source_type, source }));
      expect(
        screen.getByRole("columnheader", { name: "Subtitle Source" }),
      ).toBeVisible();
      await screen.findByText(expected);
      expect(screen.getByText("Chinese")).toBeVisible();
      expect(
        screen.queryByText(/Chinese · (EMBEDDED|LLM)/),
      ).not.toBeInTheDocument();
    },
  );

  it("keeps missing subtitle status and does not report a missing row as an existing source", async () => {
    const user = userEvent.setup();
    renderMovie(subtitle({ source_type: "provider", source: "subhd" }), [
      subtitle({ path: null }),
    ]);
    const missingRow = screen.getByRole("row", { name: /Missing Subtitles/ });
    expect(within(missingRow).getByText("-")).toBeVisible();
    expect(within(missingRow).getByText("Chinese · MISSING")).toBeVisible();
    await user.click(
      within(missingRow).getByRole("button", { name: "Search Subtitle" }),
    );
    await waitFor(() =>
      expect(actions.download).toHaveBeenCalledWith({
        radarrId: 42,
        form: { language: "zh", forced: false, hi: false },
      }),
    );
  });

  it("only infers a legacy embedded track and does not guess an LLM file's provenance", async () => {
    renderMovie(subtitle({ path: "/media/episode.llm.zh.srt" }));
    await screen.findByText("Local / Unknown");
    expect(screen.queryByText("Translated")).not.toBeInTheDocument();
  });

  it("supports a legacy embedded stream index of zero", async () => {
    renderMovie(subtitle({ path: null, embedded_track_id: 0 }));
    await screen.findByText("Embedded");
    expect(menuProps().selections).toEqual([
      {
        type: "movie",
        path: "",
        embeddedTrackId: 0,
        id: 42,
        language: "zh",
        forced: "False",
        hi: "False",
      },
    ]);
  });

  it("keeps TV source text compact beneath the language without adding a source column", async () => {
    const user = userEvent.setup();
    customRender(
      <EpisodeSubtitle
        seriesId={7}
        episodeId={9}
        subtitle={subtitle({
          source_type: "provider",
          source: "opensubtitlescom",
        })}
      />,
    );
    const label = await screen.findByText("OpenSubtitles.com");
    expect(label).toHaveStyle({
      fontSize: "calc(0.625rem * var(--mantine-scale))",
      maxWidth: "calc(5.625rem * var(--mantine-scale))",
    });
    expect(screen.getByText("zh")).toBeVisible();
    expect(screen.queryByRole("columnheader")).not.toBeInTheDocument();
    expect(menuProps().menu?.trigger).toBe("hover");
    const beforeHover = actions.captureMenu.mock.calls.length;
    await user.hover(screen.getByText("zh"));
    await waitFor(() =>
      expect(actions.captureMenu.mock.calls.length).toBeGreaterThan(
        beforeHover,
      ),
    );
    expect(menuProps().selections).toEqual([
      {
        id: 9,
        type: "episode",
        language: "zh",
        path: "/media/episode.zh.srt",
        embeddedTrackId: undefined,
        forced: "False",
        hi: "False",
      },
    ]);
    await menuProps().onAction?.("delete");
    expect(actions.remove).toHaveBeenCalledWith({
      seriesId: 7,
      episodeId: 9,
      form: {
        language: "zh",
        hi: false,
        forced: false,
        path: "/media/episode.zh.srt",
      },
    });
  });

  it("preserves the TV embedded translation selection mapping", async () => {
    customRender(
      <EpisodeSubtitle
        seriesId={7}
        episodeId={9}
        subtitle={subtitle({
          path: null,
          embedded_track_id: 3,
          source_type: "embedded",
        })}
      />,
    );
    await screen.findByText("Embedded");
    expect(screen.getByText("zh")).toBeVisible();
    expect(screen.queryByText("zh · EMBEDDED")).not.toBeInTheDocument();
    expect(menuProps().selections).toEqual([
      {
        id: 9,
        type: "episode",
        language: "zh",
        path: "",
        embeddedTrackId: 3,
        forced: "False",
        hi: "False",
      },
    ]);
  });

  it("does not add a misleading source under missing TV subtitles", async () => {
    customRender(
      <EpisodeSubtitle
        seriesId={7}
        episodeId={9}
        missing
        subtitle={subtitle({ path: null })}
      />,
    );
    expect(screen.getByText("zh · MISSING")).toBeVisible();
    expect(screen.queryByText("Local / Unknown")).not.toBeInTheDocument();
    await menuProps().onAction?.("search");
    expect(actions.download).toHaveBeenCalledWith({
      seriesId: 7,
      episodeId: 9,
      form: { language: "zh", hi: false, forced: false },
    });
  });

  it("renders unknown provider text safely and explains the history provenance without links", async () => {
    const user = userEvent.setup();
    const source = '<img src="source-xss" onerror="alert(1)">';
    customRender(
      <SubtitleSource
        subtitle={subtitle({ source_type: "provider", source })}
      />,
    );
    const label = screen.getByText(source);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    await user.hover(label);
    await screen.findByRole("tooltip");
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "Source recorded in Bazarr subtitle history:",
    );
  });
});
