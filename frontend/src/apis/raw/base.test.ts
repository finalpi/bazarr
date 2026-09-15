import { describe, expect, it } from "vitest";
import { createFormData } from "./formdata";

describe("createFormData", () => {
  it("omits undefined optional fields instead of sending a literal value", () => {
    const form = createFormData({
      id: 42,
      type: "episode",
      language: "zh",
      path: "/tv/example.en.srt",
      embeddedTrackId: undefined,
    });

    expect(form).toBeDefined();
    expect(form?.get("id")).toBe("42");
    expect(form?.has("embeddedTrackId")).toBe(false);
  });
});
