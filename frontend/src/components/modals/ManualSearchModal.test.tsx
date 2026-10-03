/* eslint-disable camelcase */
import { ComponentProps } from "react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { useEpisodesProvider, useMoviesProvider } from "@/apis/hooks/providers";
import providerApi from "@/apis/raw/providers";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { EpisodeSearchModal, MovieSearchModal } from "./ManualSearchModal";

vi.mock("@/modules/socketio", () => ({ default: { initialize: vi.fn() } }));

beforeAll(() => {
  // jsdom has no layout scrolling; keyboard selection uses this browser API.
  HTMLElement.prototype.scrollIntoView = vi.fn();
});

const result: SearchResultType = {
  matches: ["title"],
  dont_matches: [],
  language: "zh",
  forced: "False",
  hearing_impaired: "False",
  orig_score: 100,
  provider: "subhd",
  release_info: ["Maria.Holic.CHS.ass"],
  score: 80,
  score_without_hash: 100,
  subtitle: "chosen-subtitle-uuid",
  original_format: "True",
};

function renderSearch(
  download: (
    item: Item.Movie | Item.Episode,
    candidate: SearchResultType,
  ) => Promise<void> = vi.fn().mockResolvedValue(undefined),
  mediaType: "movie" | "episode" = "movie",
) {
  server.use(
    http.get("/api/providers", () =>
      HttpResponse.json({
        data: [
          { name: "subhd", status: "Good", retry: "-" },
          { name: "r3sub", status: "Good", retry: "-" },
          { name: "zimuku", status: "ConfigurationError", retry: "later" },
        ],
      }),
    ),
  );
  const item = (
    mediaType === "movie"
      ? {
          radarrId: 9,
          title: "Maria Holic",
          path: "/movies/Maria.Holic.mkv",
        }
      : {
          sonarrEpisodeId: 47,
          sonarrSeriesId: 9,
          title: "The episode",
          path: "/tv/Maria.Holic.S02E05.mkv",
        }
  ) as Item.Movie | Item.Episode;
  if ("radarrId" in item) {
    customRender(
      <MovieSearchModal
        id="manual-test"
        context={{} as ComponentProps<typeof MovieSearchModal>["context"]}
        innerProps={{ item, query: useMoviesProvider, download }}
      />,
    );
  } else {
    customRender(
      <EpisodeSearchModal
        id="manual-test"
        context={{} as ComponentProps<typeof EpisodeSearchModal>["context"]}
        innerProps={{ item, query: useEpisodesProvider, download }}
      />,
    );
  }
  return { item, download };
}

describe("manual subtitle search", () => {
  it.each([
    "timing_not_confirmed",
    "corrected_timing_not_confirmed",
    "validation_timeout",
    "reference_unavailable",
    "script_inconclusive",
  ])(
    "does not turn stale %s evidence into a permanent mismatch",
    async (reason) => {
      const user = userEvent.setup();
      server.use(
        http.get("/api/providers/movies", () =>
          HttpResponse.json({
            data: [
              {
                ...result,
                rejected: true,
                rejection: {
                  id: 8,
                  reason,
                  detail: "Old incorrect mismatch wording",
                },
              },
            ],
          }),
        ),
      );
      const { download } = renderSearch();
      await user.click(screen.getByRole("button", { name: "Search" }));
      await screen.findByText("Not verified");
      expect(screen.queryByText("Not matched")).not.toBeInTheDocument();
      expect(
        screen.queryByText("Old incorrect mismatch wording"),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Allow retry" }),
      ).not.toBeInTheDocument();
      const button = screen.getByRole("button", { name: "Download" });
      expect(button).toBeEnabled();
      await user.click(button);
      expect(download).toHaveBeenCalledOnce();
    },
  );

  it("submits only on Search, uses the chosen provider and can repeat the same search", async () => {
    const user = userEvent.setup();
    const requests: URLSearchParams[] = [];
    server.use(
      http.get("/api/providers/movies", ({ request }) => {
        requests.push(new URL(request.url).searchParams);
        return HttpResponse.json({ data: [result] });
      }),
    );
    renderSearch();
    const keyword = screen.getByRole("textbox", { name: "Search keyword" });
    await user.type(keyword, "玛利亚狂热");
    expect(requests).toHaveLength(0);

    await user.click(
      screen.getByRole("checkbox", { name: "All enabled providers" }),
    );
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
    await waitFor(() =>
      expect(
        screen.getByRole("combobox", { name: "Subtitle providers" }),
      ).toBeEnabled(),
    );
    await user.type(
      screen.getByRole("combobox", { name: "Subtitle providers" }),
      "subhd",
    );
    await screen.findByText("subhd");
    await user.keyboard("{ArrowDown}{Enter}");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Maria.Holic.CHS.ass");
    expect(requests).toHaveLength(1);
    expect(requests[0].get("keyword")).toBe("玛利亚狂热");
    expect(requests[0].getAll("providers")).toEqual(["subhd"]);

    await user.click(screen.getByRole("button", { name: "Search Again" }));
    await waitFor(() => expect(requests).toHaveLength(2));
    await screen.findByText("Maria.Holic.CHS.ass");
    await user.clear(keyword);
    await user.type(keyword, "Maria Holic");
    expect(requests).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Search Again" }));
    await waitFor(() => expect(requests).toHaveLength(3));
    expect(requests[2].get("keyword")).toBe("Maria Holic");
  });

  it("uses the default search and passes the selected candidate to Download", async () => {
    const user = userEvent.setup();
    let params: URLSearchParams | undefined;
    server.use(
      http.get("/api/providers/movies", ({ request }) => {
        params = new URL(request.url).searchParams;
        return HttpResponse.json({ data: [result] });
      }),
    );
    const { item, download } = renderSearch();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Maria.Holic.CHS.ass");
    expect(params?.has("keyword")).toBe(false);
    expect(params?.has("providers")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Download" }));
    await waitFor(() => expect(download).toHaveBeenCalledWith(item, result));
  });

  it("shows website tags as trimmed, deduplicated badges and preserves the complete download candidate", async () => {
    const user = userEvent.setup();
    const tagged: SearchResultType = {
      ...result,
      tags: [
        "其他来源",
        " 双语 ",
        "双语",
        "简体",
        "繁体",
        "英语",
        "ASS",
        "SRT",
        "SUP",
        " ",
      ],
    };
    server.use(
      http.get("/api/providers/movies", () =>
        HttpResponse.json({ data: [tagged] }),
      ),
    );
    const { item, download } = renderSearch();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("其他来源");
    for (const label of ["简体", "繁体", "英语", "ASS", "SRT", "SUP"]) {
      expect(screen.getByText(label)).toBeVisible();
    }
    expect(screen.getAllByText("双语")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Download" }));
    await waitFor(() => expect(download).toHaveBeenCalledWith(item, tagged));
  });

  it("keeps tags visible when release information is empty", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/providers/movies", () =>
        HttpResponse.json({
          data: [{ ...result, release_info: [], tags: ["双语", "SRT"] }],
        }),
      ),
    );
    renderSearch();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Cannot get release info");
    expect(screen.getByText("双语")).toBeVisible();
    expect(screen.getByText("SRT")).toBeVisible();
  });

  it("renders tag markup as literal text and keeps release details expandable", async () => {
    const user = userEvent.setup();
    const markup = '<img src="tag-xss" onerror="alert(1)">';
    server.use(
      http.get("/api/providers/movies", () =>
        HttpResponse.json({
          data: [
            {
              ...result,
              release_info: ["Release title", "More release details"],
              tags: [markup],
            },
          ],
        }),
      ),
    );
    renderSearch();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText(markup);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    await user.click(screen.getByText("Release title"));
    await waitFor(() =>
      expect(screen.getByText("More release details")).toBeVisible(),
    );
  });

  it("supports null tags on older or other provider results", async () => {
    const user = userEvent.setup();
    const legacy: SearchResultType = { ...result, tags: null };
    server.use(
      http.get("/api/providers/movies", () =>
        HttpResponse.json({ data: [legacy] }),
      ),
    );
    const { item, download } = renderSearch();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Maria.Holic.CHS.ass");
    await user.click(screen.getByRole("button", { name: "Download" }));
    await waitFor(() => expect(download).toHaveBeenCalledWith(item, legacy));
  });

  it("shows a readable rejection and disables its download without adding a status column", async () => {
    const user = userEvent.setup();
    const rejected: SearchResultType = {
      ...result,
      rejected: true,
      rejection: { id: 31, reason: "obvious_fragment" },
    };
    server.use(
      http.get("/api/providers/movies", () =>
        HttpResponse.json({ data: [rejected] }),
      ),
    );
    const { download } = renderSearch();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Not matched");
    expect(
      screen.getByText(
        "The subtitle contains only a short fragment of the video.",
      ),
    ).toBeVisible();
    const downloadButton = screen.getByRole("button", { name: "Download" });
    expect(downloadButton).toBeDisabled();
    await user.click(downloadButton);
    expect(download).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Allow retry" })).toBeEnabled();
    expect(
      screen.queryByRole("columnheader", { name: "Status" }),
    ).not.toBeInTheDocument();
  });

  it.each(["movie", "episode"] as const)(
    "clears %s rejection with the media ID and search UUID, then refreshes without downloading",
    async (mediaType) => {
      const user = userEvent.setup();
      let rejected = true;
      const searchRequests: URLSearchParams[] = [];
      const clearRequests: { form: FormData; params: URLSearchParams }[] = [];
      const path =
        mediaType === "movie"
          ? "/api/providers/movies"
          : "/api/providers/episodes";
      server.use(
        http.get(path, ({ request }) => {
          searchRequests.push(new URL(request.url).searchParams);
          return HttpResponse.json({
            data: [
              {
                ...result,
                rejected,
                rejection: rejected
                  ? {
                      id: 31,
                      reason: "timing_mismatch",
                      detail: "Audio timing did not match this video.",
                    }
                  : null,
              },
            ],
          });
        }),
        http.delete(path, async ({ request }) => {
          clearRequests.push({
            form: await request.formData(),
            params: new URL(request.url).searchParams,
          });
          rejected = false;
          return new HttpResponse(null, { status: 204 });
        }),
      );
      const { item, download } = renderSearch(undefined, mediaType);
      await user.type(
        screen.getByRole("textbox", { name: "Search keyword" }),
        "Known alias",
      );
      await user.click(screen.getByRole("button", { name: "Search" }));
      await screen.findByText("Not matched");
      expect(
        screen.getByText("Audio timing did not match this video."),
      ).toBeVisible();
      await user.click(screen.getByRole("button", { name: "Allow retry" }));
      await waitFor(() =>
        expect(screen.queryByText("Not matched")).not.toBeInTheDocument(),
      );
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Download" })).toBeEnabled(),
      );
      expect(clearRequests).toHaveLength(1);
      expect(
        clearRequests[0].form.get(
          mediaType === "movie" ? "radarrid" : "episodeid",
        ),
      ).toBe(mediaType === "movie" ? "9" : "47");
      expect(clearRequests[0].form.get("subtitle")).toBe(
        "chosen-subtitle-uuid",
      );
      expect(clearRequests[0].form.has("seriesid")).toBe(false);
      expect(searchRequests).toHaveLength(2);
      expect(searchRequests[1].get("keyword")).toBe("Known alias");
      expect(download).not.toHaveBeenCalled();
      await user.click(screen.getByRole("button", { name: "Download" }));
      await waitFor(() =>
        expect(download).toHaveBeenCalledWith(item, {
          ...result,
          rejected: false,
          rejection: null,
        }),
      );
    },
  );

  it("keeps a failed retry excluded and displays the error", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/providers/movies", () =>
        HttpResponse.json({
          data: [
            {
              ...result,
              rejected: true,
              rejection: { id: 1, reason: "timing_mismatch" },
            },
          ],
        }),
      ),
      http.delete("/api/providers/movies", () =>
        HttpResponse.json(
          { message: "Could not clear the rejection" },
          { status: 500 },
        ),
      ),
    );
    const { download } = renderSearch();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Not matched");
    await user.click(screen.getByRole("button", { name: "Allow retry" }));
    await screen.findByText("Subtitle action failed");
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
    expect(screen.getByText("Not matched")).toBeVisible();
    expect(download).not.toHaveBeenCalled();
  });

  it("renders rejection details as text even with empty release information", async () => {
    const user = userEvent.setup();
    const markup = '<img src="rejection-xss" onerror="alert(1)">';
    server.use(
      http.get("/api/providers/movies", () =>
        HttpResponse.json({
          data: [
            {
              ...result,
              release_info: [],
              rejected: true,
              rejection: { id: 1, reason: "invalid_format", detail: markup },
            },
          ],
        }),
      ),
    );
    renderSearch();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText(markup);
    expect(screen.getByText("Cannot get release info")).toBeVisible();
    expect(screen.getByText("Not matched")).toBeVisible();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("treats the real download HTTP 204 response as queued instead of saved", async () => {
    const user = userEvent.setup();
    const queued = vi.fn();
    server.use(
      http.get("/api/providers/movies", () =>
        HttpResponse.json({ data: [result] }),
      ),
      http.post("/api/providers/movies", async ({ request }) => {
        const body = await request.formData();
        queued(
          new URL(request.url).searchParams.get("radarrid"),
          body.get("subtitle"),
        );
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderSearch(async (item, candidate) => {
      await providerApi.downloadMovieSubtitle((item as Item.Movie).radarrId, {
        language: candidate.language,
        provider: candidate.provider,
        subtitle: String(candidate.subtitle),
        hi: candidate.hearing_impaired,
        forced: candidate.forced,
        original_format: candidate.original_format,
      });
    });
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Maria.Holic.CHS.ass");
    await user.click(screen.getByRole("button", { name: "Download" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Queued" })).toBeDisabled(),
    );
    expect(queued).toHaveBeenCalledWith("9", "chosen-subtitle-uuid");
    expect(
      screen.queryByRole("button", { name: "Downloaded" }),
    ).not.toBeInTheDocument();
  });
});
