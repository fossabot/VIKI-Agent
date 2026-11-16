import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { type ReactElement, useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { type AssetsApi, AssetsApiError } from "../../clients/media/assetsApi";
import type { Attachment, UploadedAttachment } from "../../turnInput";
import type { KeepInVaultContext } from "../Attachments/KeepInVault";
import { MediaComposer } from "./MediaComposer";

/** Drains the sequential upload-then-patch microtask chain `useAttachmentPicker.addFiles` runs
 * per queued file; a plain `await Promise.resolve()` only advances one hop of it. */
async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

afterEach(cleanup);

const uploaded = (n: number): UploadedAttachment => ({
  asset_id: `123e4567-e89b-12d3-a456-42661417400${n}`,
  media_type: "image/png",
  byte_size: 2,
  width: 2,
  height: 2,
  name: `file-${n}.png`,
});
const file = (n: number) =>
  new File([new Uint8Array([n, n])], `file-${n}.png`, { type: "image/png" });

function Parent({
  api,
  send = true,
  keepInVault,
}: {
  api: AssetsApi;
  send?: boolean;
  keepInVault?: KeepInVaultContext;
}): ReactElement {
  const [attachments, setAttachments] = useState<readonly Attachment[]>([]);
  return (
    <>
      <MediaComposer
        api={api}
        keepInVault={keepInVault}
        attachments={attachments}
        onAttachments={setAttachments}
        destinationLabel="Session"
        editorKey="s-1"
        running={false}
        onStop={vi.fn()}
        onSend={async () => {
          if (send) setAttachments([]);
          return send;
        }}
      />
      <output data-testid="ids">{attachments.map((item) => item.asset_id).join(",")}</output>
    </>
  );
}

/** A controlled editor, as App wires it: `value`/`onEdit` own the text, so a document-insert
 * append is visible in the textbox. */
function ControlledParent({ api }: { api: AssetsApi }): ReactElement {
  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState<readonly Attachment[]>([]);
  return (
    <MediaComposer
      api={api}
      attachments={attachments}
      onAttachments={setAttachments}
      destinationLabel="Session"
      editorKey="s-1"
      running={false}
      onStop={vi.fn()}
      onSend={async () => true}
      value={value}
      onEdit={(text) => setValue(text)}
    />
  );
}

function deferredDocument(text = "document text", name = "notes.md") {
  let resolve!: (buffer: ArrayBuffer) => void;
  const pending = new Promise<ArrayBuffer>((done) => {
    resolve = done;
  });
  const file = new File([], name, { type: "text/markdown" });
  const arrayBuffer = vi.fn(() => pending);
  Object.defineProperty(file, "arrayBuffer", { value: arrayBuffer });
  return {
    file,
    arrayBuffer,
    resolve: () => resolve(new TextEncoder().encode(text).buffer),
  };
}

test("keeps typing during document insertion", async () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  const document = deferredDocument();
  render(<ControlledParent api={api} />);
  const textarea = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.change(textarea, { target: { value: "draft" } });
  const input = window.document.querySelector("input[type=file]") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [document.file] } });
  fireEvent.click(screen.getByRole("button", { name: "Insert into message" }));
  fireEvent.change(textarea, { target: { value: "draft with later typing" } });
  await act(async () => document.resolve());
  expect(textarea.value).toBe("draft with later typing\n\ndocument text");
});

test.each([
  { name: "another editor", remount: false, returnToOriginal: false },
  { name: "a remounted editor", remount: true, returnToOriginal: false },
  { name: "the original editor after switching away", remount: false, returnToOriginal: true },
])("does not insert into $name", async ({ remount, returnToOriginal }) => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  const document = deferredDocument();
  const onEdit = vi.fn();
  const composer = (editorKey: string) => (
    <MediaComposer
      key={remount ? editorKey : undefined}
      api={api}
      attachments={[]}
      onAttachments={vi.fn()}
      destinationLabel="Session"
      editorKey={editorKey}
      running={false}
      onStop={vi.fn()}
      onSend={async () => true}
      value={`${editorKey} draft`}
      onEdit={onEdit}
    />
  );
  const view = render(composer("s-1"));
  const input = window.document.querySelector("input[type=file]") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [document.file] } });
  fireEvent.click(screen.getByRole("button", { name: "Insert into message" }));
  view.rerender(composer("s-2"));
  if (returnToOriginal) view.rerender(composer("s-1"));
  await act(async () => document.resolve());
  expect(onEdit).not.toHaveBeenCalled();
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe(
    `${returnToOriginal ? "s-1" : "s-2"} draft`,
  );
  expect(screen.queryByRole("button", { name: "Insert into message" })).toBeNull();
});

test("duplicate activation reads and inserts a document once", async () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  const document = deferredDocument();
  render(<ControlledParent api={api} />);
  const input = window.document.querySelector("input[type=file]") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [document.file] } });
  const insert = screen.getByRole("button", { name: "Insert into message" });
  act(() => {
    fireEvent.click(insert);
    fireEvent.click(insert);
  });
  await act(async () => document.resolve());
  expect(document.arrayBuffer).toHaveBeenCalledTimes(1);
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe(
    "document text",
  );
});

test.each(["first", "second"])(
  "two documents completing together preserve both inserts when %s resolves first",
  async (firstToResolve) => {
    const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
    const first = deferredDocument("first document", "first.md");
    const second = deferredDocument("second document", "second.md");
    render(<ControlledParent api={api} />);
    const textarea = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "draft" } });
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [first.file, second.file] } });
    for (const insert of screen.getAllByRole("button", { name: "Insert into message" })) {
      fireEvent.click(insert);
    }
    const documents = firstToResolve === "first" ? [first, second] : [second, first];
    await act(async () => {
      for (const document of documents) document.resolve();
    });
    expect(textarea.value).toBe(
      firstToResolve === "first"
        ? "draft\n\nfirst document\n\nsecond document"
        : "draft\n\nsecond document\n\nfirst document",
    );
    expect(screen.queryByRole("button", { name: "Insert into message" })).toBeNull();
  },
);

test("two uploads preserve order and do not duplicate when parent rerenders", async () => {
  const resolvers: Array<(asset: UploadedAttachment) => void> = [];
  const api: AssetsApi = {
    upload: vi.fn(
      (_file, _options): Promise<UploadedAttachment> =>
        new Promise((resolve) => resolvers.push(resolve)),
    ),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files: [file(1), file(2)] } });
    await Promise.resolve();
  });
  await act(async () => resolvers.shift()?.(uploaded(1)));
  await act(async () => {
    await Promise.resolve();
    resolvers.shift()?.(uploaded(2));
  });
  expect(screen.getByTestId("ids").textContent).toBe(
    `${uploaded(1).asset_id},${uploaded(2).asset_id}`,
  );
});

test("shows the hidden-context note once an attachment is added", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async (_file, _options) => uploaded(1)),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  expect(screen.queryByText(/handled by the provider/)).toBeNull();
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files: [file(1)] } });
    await Promise.resolve();
  });
  expect(
    screen.getByText(
      "Instruction files such as CLAUDE.md, hooks and tool results are handled by the provider and are not shown here.",
    ),
  ).toBeTruthy();
});

test("removing a ready local asset stays removed and retry keeps its position", async () => {
  let attempt = 0;
  const api: AssetsApi = {
    upload: vi.fn(async (_file, _options): Promise<UploadedAttachment> => {
      attempt += 1;
      if (attempt === 1) throw new Error("failed");
      return uploaded(1);
    }),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files: [file(1)] } });
    await Promise.resolve();
  });
  expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry" })));
  expect(screen.getByTestId("ids").textContent).toBe(uploaded(1).asset_id);
  const removeButton = screen.getAllByRole("button", { name: "Remove" })[0];
  if (!removeButton) throw new Error("remove button missing");
  fireEvent.click(removeButton);
  expect(screen.getByTestId("ids").textContent).toBe("");
});

test("successful send clears the submitted local batch before a later upload", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async (_file, _options) => uploaded(1)),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files: [file(1)] } });
    await Promise.resolve();
  });
  await act(async () => Promise.resolve());
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await act(async () => Promise.resolve());
  expect(screen.getByTestId("ids").textContent).toBe("");
});

test("delayed send keeps an attachment added after submission", async () => {
  let resolveSend!: (value: boolean) => void;
  const api: AssetsApi = {
    upload: vi.fn(async (incoming: File) => uploaded(incoming.name.includes("2") ? 2 : 1)),
    read: vi.fn(async () => new Blob()),
  };
  function Delayed(): ReactElement {
    const [attachments, setAttachments] = useState<readonly Attachment[]>([]);
    return (
      <>
        <MediaComposer
          api={api}
          attachments={attachments}
          onAttachments={setAttachments}
          destinationLabel="Session"
          editorKey="s-1"
          running={false}
          onStop={vi.fn()}
          onSend={() => {
            const submitted = new Set(attachments.map((item) => item.asset_id));
            return new Promise((resolve) => {
              resolveSend = (value) => {
                if (value)
                  setAttachments((current) =>
                    current.filter((item) => !submitted.has(item.asset_id)),
                  );
                resolve(value);
              };
            });
          }}
        />
        <output data-testid="ids">{attachments.map((item) => item.asset_id).join(",")}</output>
      </>
    );
  }
  render(<Delayed />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files: [file(1)] } });
    await Promise.resolve();
  });
  await act(async () => Promise.resolve());
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await act(async () => {
    fireEvent.change(input, { target: { files: [file(2)] } });
    await Promise.resolve();
  });
  await act(async () => Promise.resolve());
  resolveSend(true);
  await act(async () => Promise.resolve());
  expect(screen.getByTestId("ids").textContent).toBe(uploaded(2).asset_id);
});

test("an expired or removed restored attachment shows unavailable, disables send, and is never sent", async () => {
  const expiredRef: Attachment = {
    asset_id: "123e4567-e89b-12d3-a456-426614174010",
    media_type: "image/png",
    byte_size: 2,
    width: 2,
    height: 2,
    name: "expired.png",
    modified_at: null,
    source: "history",
  };
  const removedRef: Attachment = {
    asset_id: "123e4567-e89b-12d3-a456-426614174011",
    media_type: "image/png",
    byte_size: 2,
    width: 2,
    height: 2,
    name: "removed.png",
    modified_at: null,
    source: "history",
  };
  const onSend = vi.fn(async () => true);
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn(async (assetId: string) => {
      if (assetId === expiredRef.asset_id) throw new AssetsApiError(410, "expired");
      throw new AssetsApiError(404, "read_failed");
    }),
  };
  function RestoredParent(): ReactElement {
    const [attachments, setAttachments] = useState<readonly Attachment[]>([expiredRef, removedRef]);
    return (
      <>
        <MediaComposer
          api={api}
          attachments={attachments}
          onAttachments={setAttachments}
          destinationLabel="Session"
          editorKey="s-1"
          running={false}
          onStop={vi.fn()}
          onSend={onSend}
        />
        <output data-testid="ids">{attachments.map((item) => item.asset_id).join(",")}</output>
      </>
    );
  }
  render(<RestoredParent />);
  await act(async () => flush());
  expect(screen.getAllByText("Unavailable")).toHaveLength(2);
  const sendButton = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
  expect(sendButton.disabled).toBe(true);
  fireEvent.click(sendButton);
  expect(onSend).not.toHaveBeenCalled();

  for (const removeButton of screen.getAllByRole("button", { name: "Remove" })) {
    fireEvent.click(removeButton);
  }
  expect(screen.getByTestId("ids").textContent).toBe("");
  expect(sendButton.disabled).toBe(false);
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "hello" },
  });
  fireEvent.click(sendButton);
  expect(onSend).toHaveBeenCalledTimes(1);
});

test("two drops before a re-render refuse the second batch one line per file", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async () => uploaded(1)),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const picker = document.querySelector(".attachment-picker") as HTMLElement;
  await act(async () => {
    fireEvent.drop(picker, {
      dataTransfer: { files: [file(1), file(2), file(3), file(4), file(5), file(6)] },
    });
    fireEvent.drop(picker, {
      dataTransfer: { files: [file(7), file(8), file(9), file(10), file(11)] },
    });
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(6);
  const alert = screen.getByRole("alert");
  expect(within(alert).getAllByRole("listitem")).toHaveLength(5);
  for (const n of [7, 8, 9, 10, 11]) {
    expect(alert.textContent).toContain(`file-${n}.png`);
  }
});

test("a drop on the picker does not also reach the composer's own drop handler", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async () => uploaded(1)),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const picker = document.querySelector(".attachment-picker") as HTMLElement;
  await act(async () => {
    fireEvent.drop(picker, { dataTransfer: { files: [file(1)] } });
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(1);
  expect(screen.getAllByText("file-1.png")).toHaveLength(1);
});

test("a mixed batch keeps its images and refuses a PDF by name", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async () => uploaded(1)),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const picker = document.querySelector(".attachment-picker") as HTMLElement;
  const pdf = new File([new Uint8Array(4)], "doc.pdf", { type: "application/pdf" });
  await act(async () => {
    fireEvent.drop(picker, { dataTransfer: { files: [file(1), pdf] } });
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("alert").textContent).toContain("doc.pdf");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a .md file inserts its text into the editor and makes no chip", async () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  render(<ControlledParent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  const md = new File([new TextEncoder().encode("hello world")], "notes.md", {
    type: "text/markdown",
  });
  await act(async () => {
    fireEvent.change(input, { target: { files: [md] } });
    await flush();
  });
  expect(api.upload).not.toHaveBeenCalled();
  expect(screen.getByText('"notes.md" can be inserted into the message as text.')).toBeTruthy();
  expect(screen.queryByText("notes.md")).toBeNull(); // no chip: only the offer names the file
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Insert into message" }));
    await flush();
  });
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe(
    "hello world",
  );
  expect(screen.queryByText('"notes.md" can be inserted into the message as text.')).toBeNull();
});

test("a non-UTF-8 .txt is offered, then refused only once the insert is attempted", async () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  render(<ControlledParent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  const bad = new File([new Uint8Array([0xff, 0xff])], "bad.txt", { type: "text/plain" });
  await act(async () => {
    fireEvent.change(input, { target: { files: [bad] } });
    await flush();
  });
  expect(screen.getByRole("button", { name: "Insert into message" })).toBeTruthy();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Insert into message" }));
    await flush();
  });
  expect(screen.queryByRole("button", { name: "Insert into message" })).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("bad.txt");
});

test("a 101 KiB .md file is refused outright, never offered as a document", async () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  render(<Parent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  const big = new File([new Uint8Array(101 * 1024)], "big.md", { type: "text/markdown" });
  await act(async () => {
    fireEvent.change(input, { target: { files: [big] } });
    await flush();
  });
  expect(screen.queryByRole("button", { name: "Insert into message" })).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("big.md");
});

test("a count overflow refuses only the extra image, by name", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async () => uploaded(1)),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  const files = Array.from({ length: 11 }, (_, index) => file(index + 1));
  await act(async () => {
    fireEvent.change(input, { target: { files } });
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(10);
  expect(screen.getByRole("alert").textContent).toContain("file-11.png");
});

test("the size-refusal copy uses the capabilities' effective per-image limit", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async () => uploaded(1)),
    read: vi.fn(async () => new Blob()),
    capabilities: vi.fn(async () => ({
      maxAttachments: 10,
      maxFileBytes: 5 * 1024 * 1024,
      maxTurnBytes: 50 * 1024 * 1024,
      maxDimension: 8000,
    })),
  };
  render(<Parent api={api} />);
  await act(async () => flush());
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  const big = new File([new Uint8Array(6 * 1024 * 1024)], "big.png", { type: "image/png" });
  await act(async () => {
    fireEvent.change(input, { target: { files: [big] } });
    await flush();
  });
  expect(screen.getByRole("alert").textContent).toContain("5 MiB");
});

test("blockedReason names an in-flight upload as the reason a blocked Enter did nothing", async () => {
  const api: AssetsApi = {
    upload: vi.fn(() => new Promise<UploadedAttachment>(() => {})),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files: [file(1)] } });
    await flush();
  });
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  expect(screen.getByText("Wait for the attachments to finish uploading.")).toBeTruthy();
});

test("a batch is not refused for six attachments the parent already dropped from persisted", async () => {
  let nextId = 0;
  const api: AssetsApi = {
    upload: vi.fn(async (): Promise<UploadedAttachment> => {
      nextId += 1;
      return {
        asset_id: `asset-${nextId}`,
        media_type: "image/png",
        byte_size: 2,
        width: 2,
        height: 2,
        name: `file-${nextId}.png`,
      };
    }),
    read: vi.fn(async () => new Blob()),
  };
  function ClearParent(): ReactElement {
    const [attachments, setAttachments] = useState<readonly Attachment[]>([]);
    return (
      <>
        <MediaComposer
          api={api}
          attachments={attachments}
          onAttachments={setAttachments}
          destinationLabel="Session"
          editorKey="s-1"
          running={false}
          onStop={vi.fn()}
          onSend={async () => true}
        />
        <button type="button" onClick={() => setAttachments([])}>
          clear
        </button>
      </>
    );
  }
  render(<ClearParent />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, {
      target: { files: [file(1), file(2), file(3), file(4), file(5), file(6)] },
    });
    await flush();
  });
  expect(api.upload).toHaveBeenCalledTimes(6);
  fireEvent.click(screen.getByRole("button", { name: "clear" }));
  await act(async () => {
    fireEvent.change(input, {
      target: { files: [file(7), file(8), file(9), file(10), file(11)] },
    });
    await flush();
  });
  // The six delivered rows are invisible in the strip once `persisted` drops them (the user
  // sees none of them), so a limit of 10 must not count them against the five new files.
  expect(api.upload).toHaveBeenCalledTimes(11);
  expect(screen.queryByRole("alert")).toBeNull();
});

test("inserting a document adds a blank line when the value doesn't end with a newline", async () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  render(<ControlledParent api={api} />);
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "a" } });
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  const md = new File([new TextEncoder().encode("hello")], "notes.md", { type: "text/markdown" });
  await act(async () => {
    fireEvent.change(input, { target: { files: [md] } });
    await flush();
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Insert into message" }));
    await flush();
  });
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe(
    "a\n\nhello",
  );
});

test("blockedReason shows the failed-chip reason on a blocked Enter", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async () => {
      throw new Error("boom");
    }),
    read: vi.fn(async () => new Blob()),
  };
  render(<Parent api={api} />);
  const input = document.querySelector("input[type=file]") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files: [file(1)] } });
    await flush();
  });
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  expect(screen.getByText("Remove or retry the attachments that failed.")).toBeTruthy();
});

test("blockedReason shows the outside-limits reason on a blocked Enter", async () => {
  const ref1: Attachment = {
    asset_id: "123e4567-e89b-12d3-a456-426614174020",
    media_type: "image/png",
    byte_size: 2,
    width: 2,
    height: 2,
    name: "one.png",
    modified_at: null,
    source: "history",
  };
  const ref2: Attachment = {
    asset_id: "123e4567-e89b-12d3-a456-426614174021",
    media_type: "image/png",
    byte_size: 2,
    width: 2,
    height: 2,
    name: "two.png",
    modified_at: null,
    source: "history",
  };
  const api: AssetsApi = {
    upload: vi.fn(),
    read: vi.fn(async () => new Blob()),
    capabilities: vi.fn(async () => ({
      maxAttachments: 1,
      maxFileBytes: 20 * 1024 * 1024,
      maxTurnBytes: 50 * 1024 * 1024,
      maxDimension: 8000,
    })),
  };
  function LimitsParent(): ReactElement {
    const [attachments, setAttachments] = useState<readonly Attachment[]>([ref1, ref2]);
    return (
      <MediaComposer
        api={api}
        attachments={attachments}
        onAttachments={setAttachments}
        destinationLabel="Session"
        editorKey="s-1"
        running={false}
        onStop={vi.fn()}
        onSend={async () => true}
      />
    );
  }
  render(<LimitsParent />);
  await act(async () => flush());
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  expect(screen.getByText("The attachments exceed the destination's limits.")).toBeTruthy();
});

test("the keep-in-vault context reaches a ready attachment, and its absence leaves no keep control", async () => {
  const api: AssetsApi = {
    upload: vi.fn(async (_file, _options) => uploaded(1)),
    read: vi.fn(async () => new Blob()),
  };
  const keepInVault: KeepInVaultContext = {
    api: { keepAttachment: vi.fn() },
    workspaceId: "w-1",
    policyRevision: 1,
    rootPhysicalId: "12:34",
  };
  const attach = async () => {
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { files: [file(1)] } });
      await Promise.resolve();
    });
  };
  render(<Parent api={api} />);
  await attach();
  expect(screen.queryByRole("button", { name: "Save to vault" })).toBeNull();
  cleanup();
  render(<Parent api={api} keepInVault={keepInVault} />);
  await attach();
  expect(screen.getByRole("button", { name: "Save to vault" })).toBeTruthy();
});

test("document insertion keeps a Japanese filename and text, without interpreting markup", async () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  const text = "日本語 <img src=x onerror=alert(1)>";
  const doc = deferredDocument(text, "資料.md");
  const { container } = render(<ControlledParent api={api} />);
  const input = container.querySelector("input[type=file]") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [doc.file] } });
  expect(screen.getByText('"資料.md" can be inserted into the message as text.')).toBeTruthy();
  expect(doc.arrayBuffer).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Insert into message" }));
  await act(async () => doc.resolve());
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe(
    text,
  );
  expect(container.querySelector("img")).toBeNull();
  expect(doc.arrayBuffer).toHaveBeenCalledTimes(1);
  expect(api.upload).not.toHaveBeenCalled();
  expect(api.read).not.toHaveBeenCalled();
});

test("a refusal keeps a Japanese filename literal and Dismiss does not upload it", () => {
  const api: AssetsApi = { upload: vi.fn(), read: vi.fn(async () => new Blob()) };
  const { container } = render(<Parent api={api} />);
  const name = "<img src=x onerror=alert(1)>資料.pdf";
  const pdf = new File([new Uint8Array(4)], name, { type: "application/pdf" });
  const input = container.querySelector("input[type=file]") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [pdf] } });
  expect(screen.getByRole("alert").textContent).toContain(
    `"${name}" can't be attached (images must be PNG, JPEG or WebP). Copy the part you need and paste it into the message.`,
  );
  expect(container.querySelector("img")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(api.upload).not.toHaveBeenCalled();
  expect(api.read).not.toHaveBeenCalled();
});
