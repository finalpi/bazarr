import { describe, expect, it } from "vitest";
import { detectSubtitleLanguageFromText } from "./subtitleLanguage";

describe("detectSubtitleLanguageFromText", () => {
  it("uses common subtitle group filename markers", () => {
    expect(detectSubtitleLanguageFromText("show.[01].SC.ass", "")).toBe("zh");
    expect(detectSubtitleLanguageFromText("show.S01E01.CHT.srt", "")).toBe(
      "zt",
    );
  });

  it("detects languages from subtitle dialogue", () => {
    expect(
      detectSubtitleLanguageFromText(
        "episode.ass",
        "这不是一个男人的问题 但是我们还可以继续说下去",
      ),
    ).toBe("zh");
    expect(
      detectSubtitleLanguageFromText(
        "episode.ass",
        "這不是一個男人的問題 但是我們還可以繼續說下去",
      ),
    ).toBe("zt");
    expect(
      detectSubtitleLanguageFromText(
        "episode.srt",
        "This is a complete English subtitle sentence with enough letters.",
      ),
    ).toBe("en");
  });
});
