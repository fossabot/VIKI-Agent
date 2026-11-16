/** Bound private response allocation before parsing or decoding it. */
export async function boundedBlob(response: Response, maximum: number): Promise<Blob> {
  if (!response.body) throw new Error("empty_response");
  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maximum) throw new Error("response_too_large");
      chunks.push(new Uint8Array(value));
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
  return new Blob(chunks, { type: response.headers.get("content-type")?.split(";")[0] ?? "" });
}

export async function boundedJson(response: Response, maximum: number): Promise<unknown> {
  const blob = await boundedBlob(response, maximum);
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(await blob.arrayBuffer()));
}
