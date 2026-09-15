import { describe, expect, it } from "vitest";
import { getErrorMessage } from "./errors";

describe("getErrorMessage", () => {
  it("shows backend payload validation details", () => {
    expect(
      getErrorMessage(
        {
          message: "Input payload validation failed",
          errors: {
            embeddedTrackId: ["invalid literal for int() with base 10"],
          },
        },
        "You have disconnected from the server",
      ),
    ).toBe(
      "Input payload validation failed: invalid literal for int() with base 10",
    );
  });
});
