import { afterEach, expect, test, vi } from "vitest";
import { createLimitsApi, LimitsApiError } from "./limitsApi";

const validReading = {
  observation: {
    observed_at: "2026-10-09T12:00:00.000Z",
    status: "allowed" as const,
    limiting: "five_hour" as const,
    utilization: 5.5,
    resets_at: 1728480000,
    windows: {
      five_hour: {
        utilization: 5.5,
        resets_at: 1728480000,
      },
      seven_day: {
        utilization: 3.2,
        resets_at: 1728912000,
      },
    },
  },
};

const nullObservation = {
  observation: null,
};

afterEach(() => {
  vi.useRealTimers();
});

test("get() returns null for a null observation", async () => {
  const request = async () => Response.json(nullObservation);
  const api = createLimitsApi("secret", request);
  const result = await api.get();
  expect(result).toBeNull();
});

test("get() parses a full reading and sends the token only in the header", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe("/api/v2/limits");
    expect(init?.headers).toMatchObject({ authorization: "Bearer secret" });
    return Response.json(validReading);
  };
  const api = createLimitsApi("secret", request);
  const result = await api.get();
  expect(result).toEqual(validReading.observation);
});

test("an allowed_warning status is accepted", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        status: "allowed_warning",
      },
    });
  const api = createLimitsApi("secret", request);
  const result = await api.get();
  expect(result?.status).toBe("allowed_warning");
});

test("a rejected status is accepted", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        status: "rejected",
      },
    });
  const api = createLimitsApi("secret", request);
  const result = await api.get();
  expect(result?.status).toBe("rejected");
});

test("an unknown status is refused", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        status: "unknown",
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("a null status is refused (C3 drops a reading whose status is off)", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        status: null,
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("all limiting window types are accepted", async () => {
  const windowTypes = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"];
  for (const windowType of windowTypes) {
    const request = async () =>
      Response.json({
        observation: {
          ...validReading.observation,
          limiting: windowType,
        },
      });
    const api = createLimitsApi("secret", request);
    const result = await api.get();
    expect(result?.limiting).toBe(windowType);
  }
});

test("an unknown limiting window is refused", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        limiting: "unknown_window",
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("utilization can be null", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        utilization: null,
      },
    });
  const api = createLimitsApi("secret", request);
  const result = await api.get();
  expect(result?.utilization).toBeNull();
});

test("utilization must be in range 0..10", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        utilization: 15,
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("utilization cannot be a boolean", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        utilization: true,
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("resets_at can be null", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        resets_at: null,
      },
    });
  const api = createLimitsApi("secret", request);
  const result = await api.get();
  expect(result?.resets_at).toBeNull();
});

test("resets_at must be a safe integer > 0", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        resets_at: 0,
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("resets_at cannot be negative", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        resets_at: -1,
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("resets_at cannot be a float", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        resets_at: 1728480000.5,
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("a window with both fields null is refused", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        windows: {
          five_hour: {
            utilization: null,
            resets_at: null,
          },
          seven_day: {
            utilization: 3.2,
            resets_at: 1728912000,
          },
        },
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("a window can be null", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        windows: {
          five_hour: null,
          seven_day: {
            utilization: 3.2,
            resets_at: 1728912000,
          },
        },
      },
    });
  const api = createLimitsApi("secret", request);
  const result = await api.get();
  expect(result?.windows.five_hour).toBeNull();
});

test("a window missing a key is refused", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        windows: {
          five_hour: {
            utilization: 5.5,
          },
          seven_day: {
            utilization: 3.2,
            resets_at: 1728912000,
          },
        },
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("observed_at must be a string", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        observed_at: 12345,
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("observed_at must be <= 256 characters", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        observed_at: "a".repeat(257),
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("a missing observation key is refused", async () => {
  const request = async () => Response.json({});
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("an extra top-level key is refused", async () => {
  const request = async () => Response.json({ ...validReading, extra: 1 });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("a null observation alongside an extra key is refused, not resolved as null", async () => {
  const request = async () => Response.json({ ...nullObservation, extra: 1 });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("a body that is not an object is refused, not resolved as null", async () => {
  const bodies: unknown[] = [null, [], 42, "hello", true];
  for (const body of bodies) {
    const request = async () => Response.json(body);
    const api = createLimitsApi("secret", request);
    const error = await api.get().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(LimitsApiError);
    expect((error as LimitsApiError).code).toBe("invalid_response");
  }
});

test("a missing observation field is refused", async () => {
  const request = async () =>
    Response.json({
      observation: {
        ...validReading.observation,
        status: undefined,
      },
    });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("invalid_response");
});

test("a 401 error uses the server code", async () => {
  const request = async () =>
    Response.json({ error: { code: "unauthorized" } }, { status: 401, statusText: "Unauthorized" });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).status).toBe(401);
  expect((error as LimitsApiError).code).toBe("unauthorized");
});

test("a 403 error uses the server code", async () => {
  const request = async () =>
    Response.json(
      { error: { code: "forbidden_origin" } },
      { status: 403, statusText: "Forbidden" },
    );
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).status).toBe(403);
  expect((error as LimitsApiError).code).toBe("forbidden_origin");
});

test("a 500 error with no code defaults to unavailable", async () => {
  const request = async () =>
    Response.json({}, { status: 500, statusText: "Internal Server Error" });
  const api = createLimitsApi("secret", request);
  const error = await api.get().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).status).toBe(500);
  expect((error as LimitsApiError).code).toBe("unavailable");
});

test("a slow response times out after 15 seconds", async () => {
  vi.useFakeTimers();
  const request = (_url: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
    });
  const api = createLimitsApi("secret", request);
  const pending = api.get().catch((thrown: unknown) => thrown);
  await vi.advanceTimersByTimeAsync(15_000);
  const error = await pending;
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("timeout");
});

test("a pre-aborted signal is immediately cancelled", async () => {
  const controller = new AbortController();
  controller.abort();
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createLimitsApi("secret", request);
  const error = await api.get(controller.signal).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(LimitsApiError);
  expect((error as LimitsApiError).code).toBe("cancelled");
});
