import { expect, test } from "vitest";
import { readCss } from "../../testing/css";

const css = readCss(import.meta.url, "./ProfilesView.css");
const lookCss = readCss(import.meta.url, "./BotLookField.css");

test("profile input rules exclude native checkboxes and radios", () => {
  const inputRule = css.match(/\.profile-detail input[^{}]*\{[^{}]*\}/s)?.[0] ?? "";
  expect(inputRule).not.toBe("");
  expect(inputRule).toMatch(/width:\s*100%/);
  expect(inputRule).toContain('input:not([type="checkbox"]):not([type="radio"])');
  expect(inputRule).toContain(".profile-detail select");
  expect(inputRule).toContain(".profile-detail textarea");
});

test("profile button rules exclude shared and Bot controls while retaining styles", () => {
  const buttonRules = css.match(/\.profile-detail\s+button[^{}]*\{[^{}]*\}/g) ?? [];
  expect(buttonRules).toHaveLength(3);
  for (const buttonRule of buttonRules) {
    expect(buttonRule).toContain(":not(.btn)");
    expect(buttonRule).toContain(":not(.btn *)");
    expect(buttonRule).toContain(":not(.bot-look *)");
    expect(buttonRule).toContain(":not(.bot-hue-row *)");
    expect(buttonRule).toMatch(/:not\(\s*\.bot-icon-dialog\s+\*\s*\)/);
  }
  expect(buttonRules[0]).toMatch(/padding:\s*7px 12px/);
  expect(buttonRules[1]).toMatch(/opacity:\s*0\.5/);
  expect(buttonRules[2]).toMatch(/outline:\s*2px solid var\(--or\)/);
});

test("compact overview layout is limited to narrow Bot settings", () => {
  expect(css).toContain("@container profiles (max-width: 520px)");
  expect(css).toContain(".profiles-view.bot-settings .profile-overview-configuration > div");
  expect(css).toContain(".profiles-view.bot-settings .bot-settings-selected > .profile-overview");
  expect(css).not.toMatch(/^\s*\.profile-overview-configuration > div\s*\{/m);
});

test("compact Look controls use the full row and keep status below them", () => {
  expect(lookCss).toMatch(
    /\.bot-look-compact\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s,
  );
  expect(lookCss).toMatch(/\.bot-look-compact\s+\.bot-look-status\s*\{[^}]*grid-column:\s*1/s);
});
