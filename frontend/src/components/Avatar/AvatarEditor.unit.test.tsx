import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type AssetsApi, AssetsApiError } from "../../clients/media/assetsApi";
import {
  type AvatarManifest,
  type AvatarsApi,
  AvatarsApiError,
} from "../../clients/media/avatarsApi";
import { MediaContext } from "../../context/mediaContext";
import { AvatarEditor, headerPixels } from "./AvatarEditor";
import { DEFAULT_IDENTITY, IdentityContext } from "./identity";

afterEach(cleanup);

const SOURCE_ID = "11111111-2222-4333-8444-555555555555";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const UNUSABLE = /cannot become an avatar/;
const TOO_MANY_PIXELS = /20\s*megapixels/;

class FakeBitmap {
  close = vi.fn();
  constructor(
    public width: number,
    public height: number,
  ) {}
}

function fakeDecode(bitmaps: readonly FakeBitmap[]) {
  let index = 0;
  return vi.fn(async () => bitmaps[index++] as unknown as ImageBitmap);
}

/** A small PNG header the default measure reads; the injected decode stands in for the image. */
function pngFile(name: string): File {
  return new File([pngHeaderBytes(400, 300)], name, { type: "image/png" });
}

function chooseInput(): HTMLInputElement {
  return screen.getByLabelText(/choose image/i) as HTMLInputElement;
}

function overlay(): HTMLElement {
  return screen.getByRole("group", { name: /crop/i });
}

async function openWithBitmap(width: number, height: number): Promise<void> {
  const decode = fakeDecode([new FakeBitmap(width, height)]);
  render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
  fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
  await waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
}

describe("AvatarEditor", () => {
  it("decodes a chosen file with no upload and shows the samples and crop", async () => {
    const decode = fakeDecode([new FakeBitmap(400, 300)]);
    const onSaved = vi.fn();
    render(<AvatarEditor onSaved={onSaved} onCancel={vi.fn()} decode={decode} />);

    const file = pngFile("photo.png");
    fireEvent.change(chooseInput(), { target: { files: [file] } });

    await waitFor(() => expect(decode).toHaveBeenCalledWith(file));
    await screen.findByText("50, 0, 300");
    for (const size of [24, 32, 64, 128]) {
      screen.getByText(String(size));
    }
    const saveButton = screen.getByRole("button", { name: /save/i }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(true);
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("closes the previous bitmap when a second file is chosen", async () => {
    const first = new FakeBitmap(400, 300);
    const second = new FakeBitmap(200, 200);
    const decode = fakeDecode([first, second]);
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);

    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    await waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
    fireEvent.change(chooseInput(), { target: { files: [pngFile("b.png")] } });
    await waitFor(() => expect(decode).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(first.close).toHaveBeenCalled());
    expect(second.close).not.toHaveBeenCalled();
    await screen.findByText("0, 0, 200");
  });

  it("closes the bitmap on unmount", async () => {
    const bitmap = new FakeBitmap(400, 300);
    const decode = fakeDecode([bitmap]);
    const { unmount } = render(
      <AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    await waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
    unmount();
    expect(bitmap.close).toHaveBeenCalledTimes(1);
  });

  it("Cancel and Escape call onCancel; no upload function is called", () => {
    const onCancel = vi.fn();
    const onSaved = vi.fn();
    render(<AvatarEditor onSaved={onSaved} onCancel={onCancel} decode={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("the crop overlay is a focusable, described group", async () => {
    await openWithBitmap(400, 300);
    const group = overlay();
    expect(group.tabIndex).toBe(0);
    const describedBy = group.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy as string)?.textContent).toMatch(/1 pixel/);
  });

  it("+ zooms in on the overlay while keeping the crop centred, and ArrowRight/Shift+ArrowDown nudge it", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");

    const group = overlay();
    fireEvent.keyDown(group, { key: "+" });
    await screen.findByText("65, 15, 270");
    // The zoomed crop keeps the same centre as the original.
    expect(65 + 270 / 2).toBe(50 + 300 / 2);
    expect(15 + 270 / 2).toBe(0 + 300 / 2);

    fireEvent.keyDown(group, { key: "ArrowRight" });
    await screen.findByText("66, 15, 270");

    fireEvent.keyDown(group, { key: "ArrowDown", shiftKey: true });
    await screen.findByText("66, 25, 270");
  });

  it("an unhandled key does not change the crop", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");
    fireEvent.keyDown(overlay(), { key: "a" });
    screen.getByText("50, 0, 300");
  });

  it("Ctrl+plus is left to the browser: the crop is unchanged and the default is not prevented", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");
    const event = new KeyboardEvent("keydown", {
      key: "+",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    overlay().dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    screen.getByText("50, 0, 300");
  });

  it("the Zoom in and Zoom out buttons update the crop", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");

    fireEvent.click(screen.getByRole("button", { name: /zoom in/i }));
    await screen.findByText("65, 15, 270");

    fireEvent.click(screen.getByRole("button", { name: /zoom out/i }));
    await screen.findByText("52, 2, 297");
  });

  it("Rotate right cycles the rotation through 90/180/270/0 and swaps the grid", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");

    const rotateRight = () => screen.getByRole("button", { name: /rotate right/i });
    fireEvent.click(rotateRight());
    await screen.findByText("0, 50, 300");
    fireEvent.click(rotateRight());
    await screen.findByText("50, 0, 300");
    fireEvent.click(rotateRight());
    await screen.findByText("0, 50, 300");
    fireEvent.click(rotateRight());
    await screen.findByText("50, 0, 300");
  });

  it("Rotate left also swaps the grid", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");
    fireEvent.click(screen.getByRole("button", { name: /rotate left/i }));
    await screen.findByText("0, 50, 300");
  });

  it("a pointer drag moves the crop by the pointer delta over the preview scale", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");

    // Preview scale for a 400x300 bitmap in the 256px box is 256/400 = 0.64.
    const group = overlay();
    fireEvent.pointerDown(group, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(group, { pointerId: 1, clientX: 116, clientY: 100 });
    await screen.findByText("75, 0, 300");
  });

  it("a pointercancel ends the drag; a later pointermove moves nothing", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");

    const group = overlay();
    fireEvent.pointerDown(group, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.pointerCancel(group, { pointerId: 1 });
    fireEvent.pointerMove(group, { pointerId: 1, clientX: 116, clientY: 100 });
    screen.getByText("50, 0, 300");
  });

  it("a lostpointercapture ends the drag; a later pointermove moves nothing", async () => {
    await openWithBitmap(400, 300);
    await screen.findByText("50, 0, 300");

    const group = overlay();
    fireEvent.pointerDown(group, { pointerId: 1, clientX: 100, clientY: 100 });
    fireEvent.lostPointerCapture(group, { pointerId: 1 });
    fireEvent.pointerMove(group, { pointerId: 1, clientX: 116, clientY: 100 });
    screen.getByText("50, 0, 300");
  });

  it("drops a slower decode that lands after a newer choice", async () => {
    const slow = deferred<ImageBitmap>();
    const late = new FakeBitmap(400, 300);
    const decode = vi
      .fn<(file: File) => Promise<ImageBitmap>>()
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(new FakeBitmap(200, 200) as unknown as ImageBitmap);
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    // Waits for the first choice's own decode to start (its measure resolved while it was still
    // current) before a newer choice supersedes it: a decode already running is what this test
    // drops, distinct from a still-pending measure, which starts no decode at all.
    await waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
    fireEvent.change(chooseInput(), { target: { files: [pngFile("b.png")] } });
    await screen.findByText("0, 0, 200");

    slow.resolve(late as unknown as ImageBitmap);
    await waitFor(() => expect(late.close).toHaveBeenCalledTimes(1));
    screen.getByText("0, 0, 200");
  });

  it("a failed decode shows the editor's own copy", async () => {
    const decode = vi.fn(async () => {
      throw new Error("decoder detail");
    });
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/cannot become an avatar/);
    expect(alert.textContent).not.toContain("decoder detail");
  });

  it("refuses a file over 5 MiB without decoding it", async () => {
    const decode = vi.fn();
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
    const big = pngFile("big.png");
    Object.defineProperty(big, "size", { value: 5 * 1024 * 1024 + 1 });
    fireEvent.change(chooseInput(), { target: { files: [big] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/5 MiB/);
    expect(decode).not.toHaveBeenCalled();
  });

  it("refuses a decoded image over 20,000,000 pixels and keeps nothing", async () => {
    const bitmap = new FakeBitmap(5000, 5000);
    const decode = fakeDecode([bitmap]);
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/20\s*megapixels/);
    expect(bitmap.close).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("group", { name: /crop/i })).toBeNull();
  });

  it("a measure over 20,000,000 pixels shows too_many_pixels and never decodes", async () => {
    const decode = vi.fn();
    const measurePixels = vi.fn(async () => 20_000_001);
    render(
      <AvatarEditor
        onSaved={vi.fn()}
        onCancel={vi.fn()}
        decode={decode}
        measurePixels={measurePixels}
      />,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/20\s*megapixels/);
    expect(decode).not.toHaveBeenCalled();
  });

  it("a measure at or under 20,000,000 pixels decodes as before", async () => {
    const decode = fakeDecode([new FakeBitmap(400, 300)]);
    const measurePixels = vi.fn(async () => 20_000_000);
    render(
      <AvatarEditor
        onSaved={vi.fn()}
        onCancel={vi.fn()}
        decode={decode}
        measurePixels={measurePixels}
      />,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    await waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
    await screen.findByText("50, 0, 300");
  });

  it("with no injected measurePixels, a real PNG header declaring 23000x23000 refuses before decoding", async () => {
    const decode = vi.fn();
    const file = new File([pngHeaderBytes(23000, 23000)], "bomb.png", { type: "image/png" });
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
    fireEvent.change(chooseInput(), { target: { files: [file] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/20\s*megapixels/);
    expect(decode).not.toHaveBeenCalled();
  });

  // Each input declares 23000x23000; the default measure reads the whole file, so no padding
  // hides a JPEG's frame header, and a layout it cannot read is refused rather than decoded.
  it.each([
    ["an RLE8 BMP named .png", "bomb.png", rle8BmpBytes(23000, 23000), UNUSABLE],
    ["a GIF", "bomb.gif", gifBytes(23000, 23000), UNUSABLE],
    [
      "a JPEG behind one COM segment",
      "bomb.jpg",
      jpegBytes([paddingSegment(0xfe, 0xffff), sofSegment(0xc0, 23000, 23000)]),
      TOO_MANY_PIXELS,
    ],
    [
      "a JPEG with FF 00 after SOI",
      "bomb.jpg",
      jpegBytes([[0xff, 0x00], sofSegment(0xc0, 23000, 23000)]),
      UNUSABLE,
    ],
    [
      "a JPEG with FF 00 hiding a true SOF0 behind a fabricated small one",
      "bomb.jpg",
      ff00StuffedZeroBombBytes(),
      UNUSABLE,
    ],
    [
      "a JPEG with a stuffed zero after an FF fill (FF FF 00) hiding a true SOF0",
      "bomb.jpg",
      ffFillStuffedZeroBombBytes(),
      UNUSABLE,
    ],
    [
      "a JPEG whose SOF sits behind 128 KiB of APP15",
      "bomb.jpg",
      jpegBytes([
        paddingSegment(0xef, 0xffff),
        paddingSegment(0xef, 0xffff),
        sofSegment(0xc0, 23000, 23000),
      ]),
      TOO_MANY_PIXELS,
    ],
  ])(
    "with no injected measurePixels, %s is refused before decoding",
    async (_label, name, bytes, copy) => {
      const decode = vi.fn();
      render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
      fireEvent.change(chooseInput(), { target: { files: [new File([bytes], name)] } });
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toMatch(copy);
      expect(decode).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["PNG", pngHeaderBytes(400, 300)],
    ["JPEG", jpegBytes([app0Segment(), sofSegment(0xc0, 300, 400)])],
    ["WebP", webpBytes("VP8L", vp8lPayload(400, 300))],
  ])("with no injected measurePixels, a small %s decodes", async (_label, bytes) => {
    const decode = fakeDecode([new FakeBitmap(400, 300)]);
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
    fireEvent.change(chooseInput(), { target: { files: [new File([bytes], "small")] } });
    await screen.findByText("50, 0, 300");
    expect(decode).toHaveBeenCalledTimes(1);
  });

  it("a measure that resolves after a newer choice starts no decode and sets no notice", async () => {
    const slowMeasure = deferred<number | null>();
    const measurePixels = vi
      .fn<(file: File) => Promise<number | null>>()
      .mockReturnValueOnce(slowMeasure.promise)
      .mockResolvedValueOnce(400 * 300);
    const decode = fakeDecode([new FakeBitmap(200, 200)]);
    render(
      <AvatarEditor
        onSaved={vi.fn()}
        onCancel={vi.fn()}
        decode={decode}
        measurePixels={measurePixels}
      />,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    fireEvent.change(chooseInput(), { target: { files: [pngFile("b.png")] } });
    await screen.findByText("0, 0, 200");

    slowMeasure.resolve(400 * 300);
    await settle();
    expect(decode).toHaveBeenCalledTimes(1);
    screen.getByText("0, 0, 200");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a measure that rejects shows unreadable, not unusable, with no unhandled rejection", async () => {
    const measurePixels = vi.fn(async () => {
      throw new DOMException("stale file handle", "NotReadableError");
    });
    const decode = vi.fn();
    render(
      <AvatarEditor
        onSaved={vi.fn()}
        onCancel={vi.fn()}
        decode={decode}
        measurePixels={measurePixels}
      />,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/could not be read/);
    expect(decode).not.toHaveBeenCalled();
  });

  it("a stale measure that resolves to null after a newer choice sets no notice", async () => {
    const slowMeasure = deferred<number | null>();
    const measurePixels = vi
      .fn<(file: File) => Promise<number | null>>()
      .mockReturnValueOnce(slowMeasure.promise)
      .mockResolvedValueOnce(400 * 300);
    const decode = fakeDecode([new FakeBitmap(200, 200)]);
    render(
      <AvatarEditor
        onSaved={vi.fn()}
        onCancel={vi.fn()}
        decode={decode}
        measurePixels={measurePixels}
      />,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    fireEvent.change(chooseInput(), { target: { files: [pngFile("b.png")] } });
    await screen.findByText("0, 0, 200");

    slowMeasure.resolve(null);
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a stale measure that resolves over the pixel limit after a newer choice sets no notice", async () => {
    const slowMeasure = deferred<number | null>();
    const measurePixels = vi
      .fn<(file: File) => Promise<number | null>>()
      .mockReturnValueOnce(slowMeasure.promise)
      .mockResolvedValueOnce(400 * 300);
    const decode = fakeDecode([new FakeBitmap(200, 200)]);
    render(
      <AvatarEditor
        onSaved={vi.fn()}
        onCancel={vi.fn()}
        decode={decode}
        measurePixels={measurePixels}
      />,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    fireEvent.change(chooseInput(), { target: { files: [pngFile("b.png")] } });
    await screen.findByText("0, 0, 200");

    slowMeasure.resolve(20_000_001);
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a stale measure that rejects after a newer choice sets no notice", async () => {
    const slowMeasure = deferred<number | null>();
    const measurePixels = vi
      .fn<(file: File) => Promise<number | null>>()
      .mockReturnValueOnce(slowMeasure.promise)
      .mockResolvedValueOnce(400 * 300);
    const decode = fakeDecode([new FakeBitmap(200, 200)]);
    render(
      <AvatarEditor
        onSaved={vi.fn()}
        onCancel={vi.fn()}
        decode={decode}
        measurePixels={measurePixels}
      />,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    fireEvent.change(chooseInput(), { target: { files: [pngFile("b.png")] } });
    await screen.findByText("0, 0, 200");

    slowMeasure.reject(new DOMException("stale file handle", "NotReadableError"));
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a stale decode that rejects after a newer choice decoded sets no notice", async () => {
    const slow = deferred<ImageBitmap>();
    const decode = vi
      .fn<(file: File) => Promise<ImageBitmap>>()
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(new FakeBitmap(200, 200) as unknown as ImageBitmap);
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    await waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
    fireEvent.change(chooseInput(), { target: { files: [pngFile("b.png")] } });
    await screen.findByText("0, 0, 200");

    slow.reject(new Error("decoder detail"));
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a file refused for size supersedes a still-pending decode; the refusal stays", async () => {
    const slow = deferred<ImageBitmap>();
    const decode = vi.fn<(file: File) => Promise<ImageBitmap>>().mockReturnValueOnce(slow.promise);
    render(<AvatarEditor onSaved={vi.fn()} onCancel={vi.fn()} decode={decode} />);
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    // Waits for the first choice's own decode to start, as the drop test above does, so the
    // oversized choice supersedes a decode already running rather than a still-pending measure.
    await waitFor(() => expect(decode).toHaveBeenCalledTimes(1));

    const big = pngFile("big.png");
    Object.defineProperty(big, "size", { value: 5 * 1024 * 1024 + 1 });
    fireEvent.change(chooseInput(), { target: { files: [big] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/5 MiB/);

    slow.resolve(new FakeBitmap(400, 300) as unknown as ImageBitmap);
    await settle();
    expect(screen.queryByText("50, 0, 300")).toBeNull();
    expect(screen.getByRole("alert").textContent).toMatch(/5 MiB/);
  });
});

/** Lets every pending promise continuation run. */
function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function manifest(avatarId: string): AvatarManifest {
  const view = (size: number) => ({ size, byte_size: 1, sha256: "0".repeat(64) });
  return {
    avatar_id: avatarId,
    master: view(512),
    variants: [24, 32, 64, 128, 256].map(view),
    total_bytes: 6,
    created_at: "2026-09-27T00:00:00Z",
  };
}

function clients() {
  const upload = vi.fn<AssetsApi["upload"]>(async () => ({
    asset_id: SOURCE_ID,
    media_type: "image/png",
    byte_size: 3,
    width: 400,
    height: 300,
    name: "a.png",
  }));
  const transform = vi.fn<AvatarsApi["transform"]>(async (request) => manifest(request.avatar_id));
  const variant = vi.fn<AvatarsApi["variant"]>(async () => new Blob([]));
  const assets: AssetsApi = { upload, read: vi.fn() };
  const avatars: AvatarsApi = { transform, variant };
  return { upload, transform, variant, assets, avatars };
}

type Clients = ReturnType<typeof clients>;

function editor(c: Clients, onSaved: (id: string) => void, onCancel = vi.fn()): ReactElement {
  return (
    <MediaContext.Provider value={c.assets}>
      <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, avatars: c.avatars }}>
        <AvatarEditor
          onSaved={onSaved}
          onCancel={onCancel}
          decode={fakeDecode([new FakeBitmap(400, 300)])}
        />
      </IdentityContext.Provider>
    </MediaContext.Provider>
  );
}

async function openForSave(c: Clients, onSaved = vi.fn(), onCancel = vi.fn()) {
  const view = render(editor(c, onSaved, onCancel));
  fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
  await screen.findByText("50, 0, 300");
  return view;
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /save/i }) as HTMLButtonElement;
}

function transformedId(c: Clients, call: number): string {
  return c.transform.mock.calls[call]?.[0].avatar_id ?? "";
}

describe("AvatarEditor save", () => {
  it("uploads with the avatar purpose, transforms the crop and rotation, then reports the id", async () => {
    const c = clients();
    const onSaved = vi.fn();
    await openForSave(c, onSaved);
    fireEvent.click(screen.getByRole("button", { name: /rotate right/i }));
    fireEvent.keyDown(overlay(), { key: "ArrowDown" });
    await screen.findByText("0, 51, 300");

    fireEvent.click(saveButton());
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(c.upload).toHaveBeenCalledTimes(1);
    expect(c.upload.mock.calls[0]?.[1]).toMatchObject({ purpose: "avatar" });
    expect(c.upload.mock.calls[0]?.[1]?.signal).toBeInstanceOf(AbortSignal);
    const request = c.transform.mock.calls[0]?.[0];
    expect(request).toEqual({
      avatar_id: expect.stringMatching(UUID),
      source_asset_id: SOURCE_ID,
      crop: { x: 0, y: 51, width: 300, height: 300 },
      rotation: 90,
    });
    expect(onSaved).toHaveBeenCalledWith(request?.avatar_id);
  });

  it("a retry after a failure reuses the id and the uploaded source", async () => {
    const c = clients();
    c.transform.mockRejectedValueOnce(new AvatarsApiError(503, "unavailable"));
    const onSaved = vi.fn();
    await openForSave(c, onSaved);

    fireEvent.click(saveButton());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/could not save/i);
    expect(onSaved).not.toHaveBeenCalled();

    fireEvent.click(saveButton());
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(c.upload).toHaveBeenCalledTimes(1);
    expect(transformedId(c, 1)).toBe(transformedId(c, 0));
    expect(c.transform.mock.calls[1]?.[0].source_asset_id).toBe(SOURCE_ID);
  });

  it("a changed crop mints a new id but keeps the file's source", async () => {
    const c = clients();
    c.transform.mockRejectedValueOnce(new AvatarsApiError(503, "unavailable"));
    await openForSave(c);
    fireEvent.click(saveButton());
    await screen.findByRole("alert");

    fireEvent.keyDown(overlay(), { key: "ArrowRight" });
    await screen.findByText("51, 0, 300");
    fireEvent.click(saveButton());
    await waitFor(() => expect(c.transform).toHaveBeenCalledTimes(2));
    expect(transformedId(c, 1)).not.toBe(transformedId(c, 0));
    expect(c.upload).toHaveBeenCalledTimes(1);
  });

  it("a command_conflict drops the minted id, so a retry of the same target mints a new one", async () => {
    const c = clients();
    c.transform.mockRejectedValueOnce(new AvatarsApiError(409, "command_conflict"));
    const onSaved = vi.fn();
    await openForSave(c, onSaved);

    fireEvent.click(saveButton());
    await screen.findByRole("alert");
    fireEvent.click(saveButton());
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(transformedId(c, 1)).not.toBe(transformedId(c, 0));
  });

  it("choosing a new file supersedes a save in flight; the failed choice's notice stays", async () => {
    const c = clients();
    const upload = deferred<Awaited<ReturnType<AssetsApi["upload"]>>>();
    c.upload.mockReturnValueOnce(upload.promise);
    const decode = vi
      .fn<(file: File) => Promise<ImageBitmap>>()
      .mockResolvedValueOnce(new FakeBitmap(400, 300) as unknown as ImageBitmap)
      .mockRejectedValueOnce(new Error("bad file"));
    const onSaved = vi.fn();
    render(
      <MediaContext.Provider value={c.assets}>
        <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, avatars: c.avatars }}>
          <AvatarEditor onSaved={onSaved} onCancel={vi.fn()} decode={decode} />
        </IdentityContext.Provider>
      </MediaContext.Provider>,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    await screen.findByText("50, 0, 300");
    fireEvent.click(saveButton());
    await waitFor(() => expect(c.upload).toHaveBeenCalledTimes(1));
    const uploadSignal = c.upload.mock.calls[0]?.[1]?.signal;

    fireEvent.change(chooseInput(), { target: { files: [pngFile("b.png")] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/cannot become an avatar/);
    expect(uploadSignal?.aborted).toBe(true);

    upload.resolve({
      asset_id: SOURCE_ID,
      media_type: "image/png",
      byte_size: 3,
      width: 400,
      height: 300,
      name: "a.png",
    });
    await settle();
    expect(c.transform).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    screen.getByText("50, 0, 300");
  });

  it("Cancel during the upload aborts it and never reports", async () => {
    const c = clients();
    const upload = deferred<Awaited<ReturnType<AssetsApi["upload"]>>>();
    c.upload.mockReturnValueOnce(upload.promise);
    const onSaved = vi.fn();
    const onCancel = vi.fn();
    await openForSave(c, onSaved, onCancel);
    fireEvent.click(saveButton());
    await waitFor(() => expect(c.upload).toHaveBeenCalledTimes(1));
    const uploadSignal = c.upload.mock.calls[0]?.[1]?.signal;

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(uploadSignal?.aborted).toBe(true);
    expect(onCancel).toHaveBeenCalledTimes(1);
    upload.resolve({
      asset_id: SOURCE_ID,
      media_type: "image/png",
      byte_size: 3,
      width: 400,
      height: 300,
      name: "a.png",
    });
    await settle();
    expect(c.transform).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("Cancel during the transform aborts its signal and closes the bitmap", async () => {
    const c = clients();
    const transform = deferred<AvatarManifest>();
    c.transform.mockReturnValueOnce(transform.promise);
    const bitmap = new FakeBitmap(400, 300);
    const onSaved = vi.fn();
    render(
      <MediaContext.Provider value={c.assets}>
        <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, avatars: c.avatars }}>
          <AvatarEditor onSaved={onSaved} onCancel={vi.fn()} decode={fakeDecode([bitmap])} />
        </IdentityContext.Provider>
      </MediaContext.Provider>,
    );
    fireEvent.change(chooseInput(), { target: { files: [pngFile("a.png")] } });
    await screen.findByText("50, 0, 300");
    fireEvent.click(saveButton());
    await waitFor(() => expect(c.transform).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(c.transform.mock.calls[0]?.[1]?.aborted).toBe(true);
    await waitFor(() => expect(bitmap.close).toHaveBeenCalledTimes(1));
    transform.resolve(manifest(transformedId(c, 0)));
    await settle();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("a completion after unmount does nothing", async () => {
    const c = clients();
    const transform = deferred<AvatarManifest>();
    c.transform.mockReturnValueOnce(transform.promise);
    const onSaved = vi.fn();
    const { unmount } = await openForSave(c, onSaved);
    fireEvent.click(saveButton());
    await waitFor(() => expect(c.transform).toHaveBeenCalledTimes(1));

    unmount();
    expect(c.transform.mock.calls[0]?.[1]?.aborted).toBe(true);
    transform.resolve(manifest(transformedId(c, 0)));
    await settle();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("a completion after a crop change is ignored and Save is offered again", async () => {
    const c = clients();
    const transform = deferred<AvatarManifest>();
    c.transform.mockReturnValueOnce(transform.promise);
    const onSaved = vi.fn();
    await openForSave(c, onSaved);
    fireEvent.click(saveButton());
    await waitFor(() => expect(c.transform).toHaveBeenCalledTimes(1));
    expect(saveButton().disabled).toBe(true);

    fireEvent.keyDown(overlay(), { key: "ArrowRight" });
    await screen.findByText("51, 0, 300");
    transform.resolve(manifest(transformedId(c, 0)));
    await settle();
    expect(onSaved).not.toHaveBeenCalled();
    expect(saveButton().disabled).toBe(false);
  });

  it.each([
    ["transform_failed", new AvatarsApiError(422, "transform_failed"), /cannot become an avatar/],
    ["decode_failed", new AssetsApiError(415, "decode_failed"), /cannot become an avatar/],
    ["too_large", new AssetsApiError(413, "too_large"), /5 MiB/],
    ["capacity", new AvatarsApiError(429, "capacity"), /storage is full/],
    ["overloaded", new AvatarsApiError(429, "overloaded"), /being processed/],
    ["worker_timeout", new AvatarsApiError(503, "worker_timeout"), /took too long/],
  ])("%s shows the editor's copy and keeps the previous avatar", async (_code, error, copy) => {
    const c = clients();
    if (error instanceof AssetsApiError) c.upload.mockRejectedValueOnce(error);
    else c.transform.mockRejectedValueOnce(error);
    const onSaved = vi.fn();
    await openForSave(c, onSaved);
    fireEvent.click(saveButton());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(copy);
    expect(onSaved).not.toHaveBeenCalled();
    expect(saveButton().disabled).toBe(false);
  });

  const INSTALL_FIX = /Quit VIKI and run uv sync/;

  it.each([
    [
      "the upload",
      (c: Clients) =>
        c.upload.mockRejectedValueOnce(new AssetsApiError(503, "decoder_unavailable")),
    ],
    [
      "the transform",
      (c: Clients) =>
        c.transform.mockRejectedValueOnce(new AvatarsApiError(503, "decoder_unavailable")),
    ],
  ])(
    "a decoder_unavailable answer from %s names the same install fix the chat attachment path shows",
    async (_label, reject) => {
      const c = clients();
      reject(c);
      const onSaved = vi.fn();
      await openForSave(c, onSaved);
      fireEvent.click(saveButton());
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toMatch(INSTALL_FIX);
      expect(onSaved).not.toHaveBeenCalled();
    },
  );

  it("a not_found transform (the uploaded source expired) drops the held source id, so the next Save uploads again", async () => {
    const c = clients();
    c.transform.mockRejectedValueOnce(new AvatarsApiError(404, "not_found"));
    const onSaved = vi.fn();
    await openForSave(c, onSaved);

    fireEvent.click(saveButton());
    await screen.findByRole("alert");
    expect(onSaved).not.toHaveBeenCalled();
    expect(c.upload).toHaveBeenCalledTimes(1);

    fireEvent.click(saveButton());
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(c.upload).toHaveBeenCalledTimes(2);
    expect(transformedId(c, 1)).toBe(transformedId(c, 0));
  });

  it("a transform timeout then a found variant saves", async () => {
    const c = clients();
    c.transform.mockRejectedValueOnce(new AvatarsApiError(0, "timeout"));
    const onSaved = vi.fn();
    await openForSave(c, onSaved);
    fireEvent.click(saveButton());
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(c.variant).toHaveBeenCalledWith(transformedId(c, 0), 24, expect.any(AbortSignal));
    expect(onSaved).toHaveBeenCalledWith(transformedId(c, 0));
  });

  it("a transform timeout then a missing variant says it is still processing; Save retries exactly", async () => {
    const c = clients();
    c.transform.mockRejectedValueOnce(new AvatarsApiError(0, "timeout"));
    c.variant.mockRejectedValueOnce(new AvatarsApiError(404, "read_failed"));
    const onSaved = vi.fn();
    await openForSave(c, onSaved);
    fireEvent.click(saveButton());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/still processing/i);
    expect(onSaved).not.toHaveBeenCalled();

    fireEvent.click(saveButton());
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(transformedId(c, 1)).toBe(transformedId(c, 0));
    expect(c.upload).toHaveBeenCalledTimes(1);
  });
});

function writeUint32BE(bytes: Uint8Array, offset: number, value: number): void {
  bytes[offset] = (value >>> 24) & 0xff;
  bytes[offset + 1] = (value >>> 16) & 0xff;
  bytes[offset + 2] = (value >>> 8) & 0xff;
  bytes[offset + 3] = value & 0xff;
}

function writeUint24LE(bytes: Uint8Array, offset: number, value: number): void {
  bytes[offset] = value & 0xff;
  bytes[offset + 1] = (value >>> 8) & 0xff;
  bytes[offset + 2] = (value >>> 16) & 0xff;
}

/** A PNG signature and an IHDR chunk declaring `width` by `height`; the chunk length field and
 * CRC are not read by `headerPixels`, so they are left zero. */
function pngHeaderBytes(width: number, height: number) {
  const bytes = new Uint8Array(24);
  bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], 0);
  bytes.set([0x49, 0x48, 0x44, 0x52], 12); // "IHDR"
  writeUint32BE(bytes, 16, width);
  writeUint32BE(bytes, 20, height);
  return bytes;
}

/** SOI, then each given segment (a marker byte and its raw bytes, already including any length
 * field), concatenated. A 0xff pad byte inside a segment's own bytes stands for itself, not a
 * marker; only bytes *between* segments given here can hold pre-marker padding. */
function jpegBytes(segments: readonly (readonly number[])[]) {
  const bytes = [0xff, 0xd8];
  for (const segment of segments) bytes.push(...segment);
  return new Uint8Array(bytes);
}

/** A marker segment of `length` (its length field included), zero-filled: COM (0xfe) or APPn. */
function paddingSegment(marker: number, length: number): number[] {
  return [0xff, marker, (length >>> 8) & 0xff, length & 0xff, ...new Array(length - 2).fill(0)];
}

function le16(value: number): number[] {
  return [value & 0xff, (value >>> 8) & 0xff];
}

function le32(value: number): number[] {
  return [...le16(value & 0xffff), ...le16(value >>> 16)];
}

/** "GIF89a" and a logical screen of `width` by `height`, then a trailer. */
function gifBytes(width: number, height: number) {
  return new Uint8Array([
    0x47,
    0x49,
    0x46,
    0x38,
    0x39,
    0x61,
    ...le16(width),
    ...le16(height),
    0,
    0,
    0,
    0x3b,
  ]);
}

/** A BMP file header and BITMAPINFOHEADER declaring an 8-bit RLE8 image, then an end-of-bitmap
 * code: about 60 bytes that decode to `width * height` pixels. */
function rle8BmpBytes(width: number, height: number) {
  const pixels = [0x00, 0x01];
  const info = [...le32(40), ...le32(width), ...le32(height), ...le16(1), ...le16(8), ...le32(1)];
  const header = [...info, ...le32(pixels.length), ...new Array(16).fill(0)];
  const offset = 14 + header.length;
  return new Uint8Array([
    0x42,
    0x4d,
    ...le32(offset + pixels.length),
    0,
    0,
    0,
    0,
    ...le32(offset),
    ...header,
    ...pixels,
  ]);
}

function app0Segment(): number[] {
  return [0xff, 0xe0, 0x00, 0x04, 0x00, 0x00]; // length 4: the length field plus 2 filler bytes
}

function dhtSegment(): number[] {
  return [0xff, 0xc4, 0x00, 0x04, 0x00, 0x00]; // a DHT (C4): in the C0-CF range, not a frame header
}

/** A segment carrying the same precision/height/width bytes a real SOFn frame header would, but
 * tagged with `marker` (JPG 0xc8 or DAC 0xcc): in the C0-CF range yet never a frame. Its declared
 * length (8) matches its own bytes, so a correct skip lands exactly on the next marker; reading it
 * as a frame instead would return `height * width`, not the segment that follows. */
function fakeFrameHeaderSegment(marker: number, height: number, width: number): number[] {
  return [
    0xff,
    marker,
    0x00,
    0x08,
    0x08,
    (height >>> 8) & 0xff,
    height & 0xff,
    (width >>> 8) & 0xff,
    width & 0xff,
    0x00,
  ];
}

/** An SOFn segment (`marker`: 0xc0 baseline, 0xc2 progressive) declaring `height` by `width`,
 * with one trailing byte so a reader past the width field still has bytes to see. */
function sofSegment(marker: number, height: number, width: number): number[] {
  return [
    0xff,
    marker,
    0x00,
    0x11,
    0x08,
    (height >>> 8) & 0xff,
    height & 0xff,
    (width >>> 8) & 0xff,
    width & 0xff,
    0x00,
  ];
}

/** SOI, then a stuffed zero (`FF 00`), whose next two bytes are read as a length that skips
 * straight to a fabricated SOF0 of 16x16; a true SOF0 of 23000x23000 sits behind an APP15
 * padding segment. A walk that reads `00` as a marker uses those two bytes as a length and
 * measures the fabricated 16x16 frame; headerPixels refuses it and returns null instead. */
function ff00StuffedZeroBombBytes() {
  return jpegBytes([
    [0xff, 0x00, 0x00, 0x02],
    sofSegment(0xc0, 16, 16),
    paddingSegment(0xef, 0xffff),
    sofSegment(0xc0, 23000, 23000),
  ]);
}

/** SOI, then `FF FF 00` (a fill byte, then a stuffed zero) and `00 02`, which a walk that takes the
 * zero for more fill reads as a length that skips to a fabricated SOF0 of 16x16; a true SOF0 of
 * 23000x23000 sits behind an APP15 padding segment. headerPixels refuses it and returns null. */
function ffFillStuffedZeroBombBytes() {
  return jpegBytes([
    [0xff, 0xff, 0x00, 0x00, 0x02],
    sofSegment(0xc0, 16, 16),
    paddingSegment(0xef, 0xffff),
    sofSegment(0xc0, 23000, 23000),
  ]);
}

/** RIFF/WEBP with one chunk (`fourCC`, 4 bytes) whose payload is `payload`; the chunk size field
 * is not read by `headerPixels`, so it is left zero. */
function webpBytes(fourCC: string, payload: readonly number[]) {
  const bytes = [
    0x52,
    0x49,
    0x46,
    0x46, // "RIFF"
    0x00,
    0x00,
    0x00,
    0x00, // file size (unread)
    0x57,
    0x45,
    0x42,
    0x50, // "WEBP"
    ...Array.from(fourCC, (c) => c.charCodeAt(0)),
    0x00,
    0x00,
    0x00,
    0x00, // chunk size (unread)
    ...payload,
  ];
  return new Uint8Array(bytes);
}

function vp8xPayload(width: number, height: number): number[] {
  const bytes = new Uint8Array(10);
  bytes[0] = 0x10; // flags (arbitrary; not read)
  writeUint24LE(bytes, 4, width - 1);
  writeUint24LE(bytes, 7, height - 1);
  return Array.from(bytes);
}

function vp8lPayload(width: number, height: number): number[] {
  const bytes = new Uint8Array(5);
  bytes[0] = 0x2f; // the VP8L signature byte
  const fields = ((width - 1) & 0x3fff) | (((height - 1) & 0x3fff) << 14);
  bytes[1] = fields & 0xff;
  bytes[2] = (fields >>> 8) & 0xff;
  bytes[3] = (fields >>> 16) & 0xff;
  bytes[4] = (fields >>> 24) & 0xff;
  return Array.from(bytes);
}

function vp8Payload(width: number, height: number): number[] {
  const bytes = new Uint8Array(10);
  bytes.set([0x9d, 0x01, 0x2a], 3); // the VP8 start code, after a 3-byte frame tag
  bytes[6] = width & 0xff;
  bytes[7] = (width >>> 8) & 0x3f;
  bytes[8] = height & 0xff;
  bytes[9] = (height >>> 8) & 0x3f;
  return Array.from(bytes);
}

describe("headerPixels", () => {
  it("reads a PNG IHDR's width and height", () => {
    expect(headerPixels(pngHeaderBytes(23000, 23000))).toBe(529_000_000);
  });

  it("null on a PNG cut before IHDR ends", () => {
    expect(headerPixels(pngHeaderBytes(23000, 23000).slice(0, 20))).toBeNull();
  });

  it("null on a GIF header", () => {
    expect(headerPixels(new Uint8Array([0x47, 0x49, 0x46, 0x38, 0x39, 0x61]))).toBeNull();
  });

  it("reads a baseline JPEG's SOF0, skipping APP0 and DHT", () => {
    const bytes = jpegBytes([app0Segment(), dhtSegment(), sofSegment(0xc0, 5000, 4000)]);
    expect(headerPixels(bytes)).toBe(20_000_000);
  });

  it("reads a progressive JPEG's SOF2", () => {
    const bytes = jpegBytes([app0Segment(), sofSegment(0xc2, 5000, 4000)]);
    expect(headerPixels(bytes)).toBe(20_000_000);
  });

  it("skips an 0xff fill byte before a marker", () => {
    // A padding 0xff directly after SOI, then SOF0's own marker byte.
    const bytes = new Uint8Array([
      0xff,
      0xd8,
      0xff,
      0xff,
      0xc0,
      ...sofSegment(0xc0, 5000, 4000).slice(2),
    ]);
    expect(headerPixels(bytes)).toBe(20_000_000);
  });

  it("null when the SOF's declared bytes lie past the header given", () => {
    const bytes = jpegBytes([sofSegment(0xc0, 5000, 4000)]).slice(0, 8);
    expect(headerPixels(bytes)).toBeNull();
  });

  it("null on FF 00 hiding a true SOF0 behind a fabricated small one", () => {
    expect(headerPixels(ff00StuffedZeroBombBytes())).toBeNull();
  });

  it("null on a stuffed zero after an FF fill (FF FF 00) hiding a true SOF0", () => {
    expect(headerPixels(ffFillStuffedZeroBombBytes())).toBeNull();
  });

  it("reads a VP8X canvas", () => {
    expect(headerPixels(webpBytes("VP8X", vp8xPayload(4000, 5000)))).toBe(20_000_000);
  });

  it("reads a VP8L canvas", () => {
    expect(headerPixels(webpBytes("VP8L", vp8lPayload(300, 400)))).toBe(120_000);
  });

  it("reads a VP8 (lossy) canvas from its start code", () => {
    expect(headerPixels(webpBytes("VP8 ", vp8Payload(320, 240)))).toBe(76_800);
  });

  it("the VP8 scale bits are masked off, keeping only the 14-bit dimension", () => {
    const payload = vp8Payload(320, 240);
    payload[7] = (payload[7] ?? 0) | 0xc0; // the horizontal scale bits, above the 14-bit width
    payload[9] = (payload[9] ?? 0) | 0xc0; // the vertical scale bits, above the 14-bit height
    expect(headerPixels(webpBytes("VP8 ", payload))).toBe(76_800);
  });

  it("null when the VP8L signature byte is not 0x2f", () => {
    const payload = vp8lPayload(400, 300);
    payload[0] = 0x00;
    expect(headerPixels(webpBytes("VP8L", payload))).toBeNull();
  });

  it("null when the VP8 start code does not match 9d 01 2a", () => {
    const payload = vp8Payload(320, 240);
    payload[4] = 0x00;
    expect(headerPixels(webpBytes("VP8 ", payload))).toBeNull();
  });

  it.each([
    ["JPG (0xc8)", 0xc8],
    ["DAC (0xcc)", 0xcc],
  ])("skips a %s marker rather than reading its bytes as a frame header", (_label, marker) => {
    const bytes = jpegBytes([
      fakeFrameHeaderSegment(marker, 9000, 9000),
      sofSegment(0xc0, 5000, 4000),
    ]);
    expect(headerPixels(bytes)).toBe(20_000_000);
  });

  it.each([
    ["an embedded SOI (0xd8)", 0xd8],
    ["a TEM (0x01)", 0x01],
    ["an RSTn (0xd0)", 0xd0],
  ])("skips %s, which carries no length field, rather than reading one", (_label, marker) => {
    const bytes = jpegBytes([[0xff, marker], sofSegment(0xc0, 5000, 4000)]);
    expect(headerPixels(bytes)).toBe(20_000_000);
  });

  it("a width of 2^31 or more measures as a positive number over the bound, not negative", () => {
    const measured = headerPixels(pngHeaderBytes(0x80000000, 1));
    expect(measured).toBe(0x80000000);
    expect(measured).toBeGreaterThan(20_000_000);
  });
});
