import { describe, expect, it } from "vitest";
import { isMac, platformKind } from "./platform";

describe("platformKind", () => {
  it("prefers userAgentData.platform over platform", () => {
    expect(platformKind({ userAgentData: { platform: "macOS" }, platform: "Win32" })).toBe("mac");
  });

  it("falls back to navigator.platform when userAgentData is absent", () => {
    expect(platformKind({ platform: "MacIntel" })).toBe("mac");
    expect(platformKind({ platform: "Linux x86_64" })).toBe("linux");
    expect(platformKind({ platform: "Win32" })).toBe("windows");
  });

  it("falls back to navigator.platform when userAgentData.platform is empty", () => {
    expect(platformKind({ userAgentData: { platform: "" }, platform: "MacIntel" })).toBe("mac");
  });

  it("matches the other Linux desktop spellings case-insensitively", () => {
    expect(platformKind({ platform: "X11" })).toBe("linux");
    expect(platformKind({ platform: "CrOS x86_64" })).toBe("linux");
  });

  it("anything else, including jsdom's own empty string, reads as windows", () => {
    expect(platformKind({ platform: "" })).toBe("windows");
    expect(platformKind({ platform: "iPhone" })).toBe("windows");
    expect(platformKind()).toBe("windows");
  });
});

describe("isMac", () => {
  it("is true only for the mac platform kind", () => {
    expect(isMac({ platform: "MacIntel" })).toBe(true);
    expect(isMac({ platform: "Win32" })).toBe(false);
    expect(isMac({ platform: "Linux x86_64" })).toBe(false);
    expect(isMac()).toBe(false);
  });
});
