import { describe, it, expect } from "vitest";
import { updaterErrorMessage } from "./updaterErrors";

const FRIENDLY_NETWORK =
  "Couldn't check for updates. Check your internet connection and try again.";

describe("updaterErrorMessage", () => {
  it("maps reqwest network errors to friendly copy", () => {
    expect(
      updaterErrorMessage(
        new Error(
          "error sending request for url (https://github.com/R1sh1m/Transfera/releases/latest/download/latest.json)",
        ),
        "Failed to check for updates.",
      ),
    ).toBe(FRIENDLY_NETWORK);
  });

  it("maps common connectivity keywords to friendly copy", () => {
    for (const raw of [
      "Failed to fetch latest.json",
      "network error while checking",
      "request timed out",
      "timed out after 30s",
      "DNS lookup failed",
      "connection refused",
      "device offline",
      "get https://example.com/latest.json failed",
    ]) {
      expect(updaterErrorMessage(new Error(raw), "fallback")).toBe(
        FRIENDLY_NETWORK,
      );
    }
  });

  it("keeps the install-ordering friendly copy", () => {
    expect(
      updaterErrorMessage(
        new Error("install called before download has completed"),
        "fallback",
      ),
    ).toBe("The update wasn't fully downloaded. Trying the download again.");
  });

  it("keeps the manifest deserialization friendly copy", () => {
    expect(
      updaterErrorMessage(
        new Error("invalid type: sequence, expected a string"),
        "fallback",
      ),
    ).toBe(
      "Couldn't read the update info from the release server. Please try again later.",
    );
  });

  it("falls back to raw message or fallback for unknown errors", () => {
    expect(updaterErrorMessage(new Error("some odd failure"), "fallback")).toBe(
      "some odd failure",
    );
    expect(updaterErrorMessage(null, "fallback")).toBe("fallback");
  });
});
