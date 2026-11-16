/** Each tile is a button that hands its VIKI folder to `onOpen`; without `onOpen` it is inert. */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { VaultTiles } from "./VaultTiles";

afterEach(cleanup);

const counts = { knowledge: 3, sources: 2, inbox: 1 };

test.each([
  ["knowledge", "30-knowledge"],
  ["sources", "40-sources"],
  ["inbox", "00-inbox"],
])("the %s tile opens the vault on %s", (tile, folder) => {
  const onOpen = vi.fn();
  const { container } = render(<VaultTiles counts={counts} onOpen={onOpen} />);
  fireEvent.click(container.querySelector(`button[data-tile="${tile}"]`) as HTMLElement);
  expect(onOpen).toHaveBeenCalledExactlyOnceWith(folder);
});

test("without onOpen every tile is a disabled button that opens nothing", () => {
  const { container } = render(<VaultTiles counts={counts} />);
  const tiles = [...container.querySelectorAll<HTMLButtonElement>("button.tile")];
  expect(tiles).toHaveLength(3);
  expect(tiles.every((tile) => tile.disabled)).toBe(true);
});
