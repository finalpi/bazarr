import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import server from "@/tests/mocks/node";
import providers from "./providers";

vi.mock("@/modules/socketio", () => ({ default: { initialize: vi.fn() } }));

describe("manual subtitle search parameters", () => {
  it("keeps an unspecified search compatible with the existing episode API", async () => {
    let params: URLSearchParams | undefined;
    server.use(
      http.get("/api/providers/episodes", ({ request }) => {
        params = new URL(request.url).searchParams;
        return HttpResponse.json({ data: [] });
      }),
    );

    await providers.episodes(42);
    expect(params?.get("episodeid")).toBe("42");
    expect(params?.has("keyword")).toBe(false);
    expect(params?.has("providers")).toBe(false);
  });

  it("sends exact Unicode keywords and repeated provider names for both media types", async () => {
    const captured: URLSearchParams[] = [];
    server.use(
      http.get(/\/api\/providers\/(episodes|movies)$/, ({ request }) => {
        captured.push(new URL(request.url).searchParams);
        return HttpResponse.json({ data: [] });
      }),
    );
    const options = {
      keyword: "  玛利亚狂热 & S01E01  ",
      providers: ["subhd", "r3sub"],
    };

    await providers.episodes(42, options);
    await providers.movies(9, options);

    expect(captured[0].get("episodeid")).toBe("42");
    expect(captured[1].get("radarrid")).toBe("9");
    for (const params of captured) {
      expect(params.get("keyword")).toBe("玛利亚狂热 & S01E01");
      expect(params.getAll("providers")).toEqual(["subhd", "r3sub"]);
      expect(params.has("providers[]")).toBe(false);
    }
  });
});
