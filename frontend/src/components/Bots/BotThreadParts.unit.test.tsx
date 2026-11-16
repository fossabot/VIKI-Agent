import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import {
  BotRecipient,
  BotTag,
  BotThreadListFaces,
  BotThreadStrip,
  type ThreadBot,
} from "./BotThreadParts";
import { BotHuesContext } from "./botHues";

afterEach(cleanup);

const identity = (name: string): ProfileIdentityV1 => ({
  name,
  purpose: "",
  avatar_asset_id: null,
  accent: "slate",
  shape: "rounded",
});

const bot = (key: string, name: string, profileId = key): ThreadBot => ({
  key,
  name,
  profileId,
  identity: identity(name),
});

it("tags a Bot with its hue attribute and shows its name as text", () => {
  const { container } = render(<BotTag bot={bot("p1", "Chief")} />);
  const tag = container.querySelector(".bot-tag");
  expect(tag?.getAttribute("data-bot-hue")).toBe("orange"); // the chief seat's fixed hue
  expect(container.querySelector(".bot-tag-name")?.textContent).toBe("Chief");
});

it("reads a stored hue from a BotHuesContext provider entry, for a non-seat Bot", () => {
  const { container } = render(
    <BotHuesContext.Provider value={{ hues: { p9: "pink" }, icons: {} }}>
      <BotTag bot={bot("k", "Nona", "p9")} />
    </BotHuesContext.Provider>,
  );
  expect(container.querySelector(".bot-tag")?.getAttribute("data-bot-hue")).toBe("pink");
});

it("lights only the working Bot's strip face, with a needs-you face only for its key", () => {
  const bots = [bot("a", "Alpha"), bot("b", "Beta"), bot("c", "Gamma")];
  const { container } = render(<BotThreadStrip bots={bots} lit="b" needsYou={new Set(["c"])} />);
  expect(container.querySelector(".bot-thread-strip")?.getAttribute("aria-hidden")).toBe("true");
  const faces = [...container.querySelectorAll(".bot-thread-strip-face")];
  expect(faces.map((face) => face.getAttribute("data-lit"))).toEqual(["false", "true", "false"]);
  expect(faces[1]?.querySelector(".bot-face")?.hasAttribute("data-state")).toBe(false);
  expect(faces[1]?.querySelector(".bot-face-dot")).toBeNull();
  expect(faces[2]?.querySelector(".bot-face")?.getAttribute("data-state")).toBe("needs_you");
  expect(faces[2]?.querySelector(".bot-face-dot")).not.toBeNull();
});

it("dims every strip face while no Bot works", () => {
  const { container } = render(
    <BotThreadStrip bots={[bot("a", "Alpha"), bot("b", "Beta")]} lit={null} needsYou={new Set()} />,
  );
  const faces = [...container.querySelectorAll(".bot-thread-strip-face")];
  expect(faces.map((face) => face.getAttribute("data-lit"))).toEqual(["false", "false"]);
});

it("renders no compact faces for an empty participant list", () => {
  const { container } = render(<BotThreadListFaces bots={[]} />);
  expect(container.querySelectorAll(".bot-thread-list-face")).toHaveLength(0);
  expect(container.querySelector(".bot-thread-list-more")).toBeNull();
});

it("renders three compact decorative faces and accessible names", () => {
  const bots = [bot("a", "Alpha"), bot("b", "Beta"), bot("c", "Gamma")];
  const { container } = render(<BotThreadListFaces bots={bots} />);
  const faces = [...container.querySelectorAll(".bot-thread-list-face")];
  expect(faces).toHaveLength(3);
  expect(faces.map((face) => (face.querySelector(".bot-face") as HTMLElement).style.width)).toEqual(
    ["18px", "18px", "18px"],
  );
  expect(
    faces.every((face) => face.querySelector(".bot-face")?.getAttribute("aria-hidden") === "true"),
  ).toBe(true);
  expect(container.querySelector(".sr-only")?.textContent).toBe("Alpha, Beta, Gamma");
  expect(container.querySelector(".bot-thread-list-more")).toBeNull();
});

it("shows the exact overflow count and names every participant", () => {
  const bots = [
    bot("a", "Alpha"),
    bot("b", "Beta"),
    bot("c", "Gamma"),
    bot("d", "Delta"),
    bot("e", "Epsilon"),
  ];
  const { container } = render(<BotThreadListFaces bots={bots} />);
  expect(container.querySelectorAll(".bot-thread-list-face")).toHaveLength(3);
  expect(container.querySelector(".bot-thread-list-more")?.textContent).toBe("+2");
  expect(container.querySelector(".bot-thread-list-more")?.getAttribute("aria-hidden")).toBe(
    "true",
  );
  expect(container.querySelector(".sr-only")?.textContent).toBe(
    "Alpha, Beta, Gamma, Delta, Epsilon",
  );
});

it("renders nothing for no Bots", () => {
  const { container } = render(
    <BotRecipient label="Send to" bots={[]} value="" onChange={() => {}} />,
  );
  expect(container.firstChild).toBeNull();
});

it("names the select from the label and lists each Bot as an option", () => {
  const bots = [bot("a", "Alpha"), bot("b", "Beta")];
  render(<BotRecipient label="Send to" bots={bots} value="a" onChange={() => {}} />);
  const select = screen.getByRole("combobox", { name: "Send to" }) as HTMLSelectElement;
  expect(select.value).toBe("a");
  expect(Array.from(select.options).map((option) => option.text)).toEqual(["Alpha", "Beta"]);
  expect(select.disabled).toBe(false);
});

it("fires the change callback with the picked Bot's key", () => {
  const bots = [bot("a", "Alpha"), bot("b", "Beta")];
  const onChange = vi.fn();
  render(<BotRecipient label="Send to" bots={bots} value="a" onChange={onChange} />);
  fireEvent.change(screen.getByRole("combobox", { name: "Send to" }), {
    target: { value: "b" },
  });
  expect(onChange).toHaveBeenCalledWith("b");
});

it("colours the recipient by the chosen Bot's provider-assigned hue, and can disable the select", () => {
  const bots = [bot("a", "Alpha"), bot("b", "Beta")];
  const { container } = render(
    <BotHuesContext.Provider value={{ hues: { b: "amber" }, icons: {} }}>
      <BotRecipient label="Send to" bots={bots} value="b" disabled onChange={() => {}} />
    </BotHuesContext.Provider>,
  );
  expect(container.querySelector(".bot-recipient")?.getAttribute("data-bot-hue")).toBe("amber");
  expect((screen.getByRole("combobox", { name: "Send to" }) as HTMLSelectElement).disabled).toBe(
    true,
  );
});
