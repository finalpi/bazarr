/* eslint-disable camelcase */
import { ComponentProps } from "react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { useMoviesProvider } from "@/apis/hooks/providers";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { MovieSearchModal } from "./ManualSearchModal";

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

function renderSearch(download = vi.fn().mockResolvedValue(undefined)) {
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
  const item = {
    radarrId: 9,
    title: "Maria Holic",
    path: "/movies/Maria.Holic.mkv",
  } as Item.Movie;
  customRender(
    <MovieSearchModal
      id="manual-test"
      context={{} as ComponentProps<typeof MovieSearchModal>["context"]}
      innerProps={{ item, query: useMoviesProvider, download }}
    />,
  );
  return { item, download };
}

describe("manual subtitle search", () => {
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
});
