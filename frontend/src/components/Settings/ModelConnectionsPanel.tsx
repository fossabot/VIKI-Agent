import { useContext, useEffect, useId, useRef, useState } from "react";
import { MAGPIE_BASE_URL } from "../../clients/app/magpieApi";
import {
  type ModelApiMode,
  type ModelConnection,
  type ModelConnectionDraft,
  ModelConnectionError,
  ModelConnectionsContext,
  type ModelConnectionsListing,
} from "../../clients/app/modelConnectionsApi";
import { t } from "../../i18n/t";
import type { ShellSnapshot, ShellStore } from "../../state/shell";
import { MagpiePanel } from "./MagpiePanel";
import "./ModelConnections.css";

const EMPTY: ModelConnectionDraft = {
  name: "",
  base_url: "",
  api_mode: "chat_completions",
  model: "",
  api_key: "",
};

export function ModelConnectionsPanel({
  store,
  snapshot,
}: {
  store: ShellStore;
  snapshot: ShellSnapshot;
}) {
  const api = useContext(ModelConnectionsContext);
  const [listing, setListing] = useState<ModelConnectionsListing | null>(null);
  const [draft, setDraft] = useState(EMPTY);
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tested, setTested] = useState(false);
  const [keyEdit, setKeyEdit] = useState<{ id: string; key: string } | null>(null);
  const [keyResult, setKeyResult] = useState<{ id: string; error: string | null } | null>(null);
  const [picker, setPicker] = useState(false);
  const [magpieModels, setMagpieModels] = useState<string[] | null>(null);
  const [magpieModel, setMagpieModel] = useState("");
  const [gatewayKey, setGatewayKey] = useState("");
  const alive = useRef(true);
  const testAbort = useRef<AbortController | null>(null);
  const keyAbort = useRef<AbortController | null>(null);
  const id = useId();
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    if (api)
      void api
        .list(controller.signal)
        .then((next) => {
          if (!controller.signal.aborted) setListing(next);
        })
        .catch(() => {
          if (!controller.signal.aborted) setError("unavailable");
        });
    return () => {
      alive.current = false;
      controller.abort();
      testAbort.current?.abort();
      keyAbort.current?.abort();
    };
  }, [api]);
  if (!api) return null;
  const disabled = busy !== null || snapshot.settings?.read_only !== false;
  const defaultModel = snapshot.settings?.effective["chat.default_model"];
  const qualifiedDefault = typeof defaultModel === "string" && defaultModel.startsWith("viki-llm/");
  const currentDefault = listing?.connections.find(
    (connection) => connection.model_ref === defaultModel,
  );
  const isMagpie = (connection: ModelConnection) => connection.base_url === MAGPIE_BASE_URL;
  const savedMagpie = listing?.connections.filter(isMagpie) ?? [];
  const duplicate = savedMagpie.some((connection) => connection.model === magpieModel);
  const capacity = listing !== null && listing.connections.length >= 8;
  function change(patch: Partial<ModelConnectionDraft>) {
    setDraft((current) => ({ ...current, ...patch }));
    setTested(false);
    setModels([]);
  }
  async function run(kind: string, work: () => Promise<void>) {
    if (busy !== null) return;
    setBusy(kind);
    setError(null);
    try {
      await work();
    } catch (error) {
      if (alive.current) {
        setGatewayKey("");
        setError(error instanceof ModelConnectionError ? error.code : "unavailable");
      }
    } finally {
      if (alive.current) setBusy(null);
    }
  }
  async function reload() {
    if (!api) return;
    const next = await api.list();
    if (alive.current) setListing(next);
    await store.refreshSettings();
  }
  function field(key: "name" | "base_url" | "model" | "api_key", label: string, max: number) {
    return (
      <label className="llm-field">
        <span>{label}</span>
        <input
          className="input"
          type={key === "api_key" ? "password" : "text"}
          value={draft[key]}
          maxLength={max}
          autoComplete="off"
          spellCheck={false}
          required={key !== "api_key"}
          list={key === "model" && models.length ? `${id}-models` : undefined}
          onChange={(event) => {
            if (key === "model") {
              setDraft((value) => ({ ...value, model: event.target.value }));
              setTested(false);
            } else change({ [key]: event.target.value });
          }}
        />
      </label>
    );
  }

  function connectionRow(connection: ModelConnection) {
    if (!listing || !api) return null;
    const managed = isMagpie(connection);
    return (
      <div className="llm-connection" key={connection.id}>
        <div>
          <strong>{connection.name}</strong>
          <div className="mono">{connection.model}</div>
          {!managed && <div className="settings-row-desc">{connection.base_url}</div>}
        </div>
        <div className="settings-actions">
          <button
            type="button"
            className="btn txt sm"
            disabled={disabled}
            aria-label={`${t(managed ? "magpie.update_key" : "llm.update_key")}: ${connection.name}`}
            onClick={() => {
              setKeyEdit({ id: connection.id, key: "" });
              setKeyResult(null);
            }}
          >
            {t(managed ? "magpie.update_key" : "llm.update_key")}
          </button>
          {defaultModel === connection.model_ref ? (
            <span className="settings-pill" data-tone="ready">
              {t("llm.default")}
            </span>
          ) : (
            <button
              type="button"
              className="btn alt sm"
              disabled={disabled}
              onClick={() =>
                void run("default", async () => {
                  await store.patch({ "chat.default_model": connection.model_ref });
                  await reload();
                })
              }
            >
              {t("llm.use")}
            </button>
          )}
          <button
            type="button"
            className="btn txt sm"
            disabled={disabled}
            aria-label={`${t("llm.remove")}: ${connection.name}`}
            onClick={() =>
              void run("remove", async () => {
                const next = await api.remove(listing.revision, connection.id);
                if (alive.current) setListing(next);
                await store.refreshSettings();
              })
            }
          >
            {t("llm.remove")}
          </button>
        </div>
        {keyEdit?.id === connection.id && (
          <form
            className="llm-key-update"
            onSubmit={(event) => {
              event.preventDefault();
              if (disabled) return;
              const controller = new AbortController();
              keyAbort.current = controller;
              setBusy("key");
              setKeyResult(null);
              void api
                .updateKey(connection.id, listing.revision, keyEdit.key, controller.signal)
                .then(
                  (next) => {
                    if (!alive.current || controller.signal.aborted) return;
                    setListing(next);
                    setKeyEdit(null);
                    setKeyResult({ id: connection.id, error: null });
                  },
                  (error: unknown) => {
                    if (!alive.current || controller.signal.aborted) return;
                    setKeyEdit({ id: connection.id, key: "" });
                    setKeyResult({
                      id: connection.id,
                      error: error instanceof ModelConnectionError ? error.code : "unavailable",
                    });
                  },
                )
                .finally(() => {
                  if (alive.current && !controller.signal.aborted) setBusy(null);
                });
            }}
          >
            <label className="llm-field">
              <span>
                {t(managed ? "magpie.new_key" : "llm.new_key")}: {connection.name}
              </span>
              <input
                className="input"
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={keyEdit.key}
                maxLength={4096}
                disabled={disabled}
                onChange={(event) => setKeyEdit({ id: connection.id, key: event.target.value })}
              />
            </label>
            <p className="settings-row-desc">{t("llm.key_scope")}</p>
            <div className="settings-actions">
              <button type="submit" className="btn alt sm" disabled={disabled}>
                {t(managed ? "magpie.save_key" : "llm.save_key")}
              </button>
              <button
                type="button"
                className="btn txt sm"
                disabled={disabled}
                onClick={() => {
                  setKeyEdit(null);
                  setKeyResult(null);
                }}
              >
                {t("llm.cancel_key")}
              </button>
            </div>
          </form>
        )}
        {keyResult?.id === connection.id &&
          (keyResult.error === null ? (
            <p role="status">{t(managed ? "magpie.key_updated" : "llm.key_updated")}</p>
          ) : (
            <p role="alert">
              {keyResult.error === "stale_revision" ? t("llm.stale") : t("llm.failed")}
            </p>
          ))}
        {keyResult?.id === connection.id && keyResult.error === "stale_revision" && (
          <button
            type="button"
            className="btn txt sm"
            disabled={disabled}
            onClick={() =>
              void run("reload", async () => {
                await reload();
                if (alive.current) {
                  setKeyEdit(null);
                  setKeyResult(null);
                }
              })
            }
          >
            {t("llm.reload")}
          </button>
        )}
      </div>
    );
  }
  return (
    <section className="settings-card" aria-labelledby={`${id}-title`} aria-busy={busy !== null}>
      <div className="settings-card-head">
        <h3 className="settings-card-title" id={`${id}-title`}>
          {t("llm.title")}
        </h3>
      </div>
      <p className="settings-row-desc">{t("llm.help")}</p>
      <div className="llm-current" role="status">
        <span>{t("llm.current_default")}</span>
        <strong>
          {!snapshot.settings
            ? t("llm.loading")
            : currentDefault
              ? currentDefault.name
              : qualifiedDefault
                ? listing === null && !error
                  ? t("llm.loading")
                  : t("llm.missing_default")
                : t("llm.native_title")}
        </strong>
        {currentDefault && (
          <span className="chip dim">
            {isMagpie(currentDefault) ? "Magpie" : t("llm.direct_label")}
          </span>
        )}
        {currentDefault ? (
          <span className="mono">{currentDefault.model}</span>
        ) : (
          typeof defaultModel === "string" && <span className="mono">{defaultModel}</span>
        )}
      </div>
      <section className="llm-choice" aria-labelledby={`${id}-native`}>
        <h4 id={`${id}-native`}>{t("llm.native_title")}</h4>
        <p className="settings-row-desc">{t("llm.native_help")}</p>
        {snapshot.settings && !qualifiedDefault ? (
          <span className="settings-pill" data-tone="ready">
            {t("llm.default")}
          </span>
        ) : (
          <button
            type="button"
            className="btn alt sm"
            disabled={disabled}
            onClick={() =>
              void run("default", async () => {
                await store.patch({ "chat.default_model": null });
                await reload();
              })
            }
          >
            {t("llm.subscription")}
          </button>
        )}
      </section>
      <section className="llm-choice">
        <MagpiePanel
          connectionDisabled={disabled || listing === null || capacity}
          onConnect={() => {
            setPicker(true);
            setMagpieModels(null);
            setMagpieModel("");
            setGatewayKey("");
          }}
        />
        {picker && (
          <form
            className="llm-magpie-picker"
            onSubmit={(event) => {
              event.preventDefault();
              if (
                !listing ||
                disabled ||
                capacity ||
                !magpieModels?.includes(magpieModel) ||
                duplicate
              )
                return;
              const api_key = gatewayKey;
              setGatewayKey("");
              void run("magpie-save", async () => {
                const next = await api.save(listing.revision, {
                  name: [...`Magpie \u00b7 ${magpieModel}`].slice(0, 80).join(""),
                  base_url: MAGPIE_BASE_URL,
                  api_mode: "anthropic",
                  model: magpieModel,
                  api_key,
                });
                if (alive.current) {
                  setListing(next);
                  setPicker(false);
                  setMagpieModels(null);
                  setMagpieModel("");
                }
                await store.refreshSettings();
              });
            }}
          >
            <fieldset className="llm-fields" disabled={disabled || listing === null || capacity}>
              <legend>{t("magpie.connect")}</legend>
              <details className="llm-gateway-key">
                <summary>{t("magpie.gateway_key")}</summary>
                <label className="llm-field">
                  <span>{t("magpie.gateway_key")}</span>
                  <input
                    className="input"
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    maxLength={4096}
                    value={gatewayKey}
                    onChange={(event) => setGatewayKey(event.target.value)}
                  />
                </label>
                <p className="settings-row-desc">{t("magpie.gateway_key_help")}</p>
              </details>
              <div className="settings-actions">
                <button
                  type="button"
                  className="btn alt sm"
                  onClick={() =>
                    void run("magpie-discover", async () => {
                      const controller = new AbortController();
                      testAbort.current = controller;
                      setMagpieModels(null);
                      setMagpieModel("");
                      try {
                        const found = await api.discover(
                          {
                            name: "Magpie",
                            base_url: MAGPIE_BASE_URL,
                            api_mode: "anthropic",
                            model: "",
                            api_key: gatewayKey,
                          },
                          controller.signal,
                        );
                        if (alive.current) {
                          setMagpieModels(found);
                          setMagpieModel(found[0] ?? "");
                        }
                      } catch (failure) {
                        if (alive.current) setGatewayKey("");
                        throw failure;
                      }
                    })
                  }
                >
                  {busy === "magpie-discover" ? t("llm.discovering") : t("llm.models")}
                </button>
                <button
                  type="button"
                  className="btn txt sm"
                  onClick={() => {
                    setPicker(false);
                    setGatewayKey("");
                    setMagpieModels(null);
                    setMagpieModel("");
                  }}
                >
                  {t("llm.cancel_key")}
                </button>
              </div>
              {magpieModels !== null &&
                (magpieModels.length === 0 ? (
                  <p role="status" className="settings-row-desc">
                    {t("magpie.empty_models")}
                  </p>
                ) : (
                  <>
                    <div className="llm-field">
                      <label htmlFor={`${id}-magpie-model`}>{t("magpie.pick_model")}</label>
                      <select
                        id={`${id}-magpie-model`}
                        className="input"
                        value={magpieModel}
                        onChange={(event) => setMagpieModel(event.target.value)}
                      >
                        {magpieModels.map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))}
                      </select>
                    </div>
                    {duplicate ? (
                      <p role="status" className="settings-row-desc">
                        {t("magpie.saved_model")}
                      </p>
                    ) : null}
                    <button
                      type="submit"
                      className="btn alt sm"
                      disabled={duplicate || !magpieModel}
                    >
                      {busy === "magpie-save" ? t("llm.saving") : t("magpie.add_model")}
                    </button>
                  </>
                ))}
            </fieldset>
          </form>
        )}
        {savedMagpie.map(connectionRow)}
      </section>
      {listing === null && !error && <p role="status">{t("llm.loading")}</p>}
      {capacity && <p className="settings-row-desc">{t("llm.capacity")}</p>}
      <p className="settings-row-desc">{t("llm.scope")}</p>
      <details className="llm-advanced">
        <summary>{t("llm.advanced")}</summary>
        <p className="settings-row-desc">{t("llm.direct_help")}</p>
        <p className="settings-row-desc">{t("llm.keys_help")}</p>
        {listing?.connections.filter((connection) => !isMagpie(connection)).map(connectionRow)}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!listing || disabled) return;
            void run("save", async () => {
              const next = await api.save(listing.revision, draft);
              if (alive.current) {
                setListing(next);
                setDraft(EMPTY);
                setModels([]);
                setTested(false);
              }
              await store.refreshSettings();
            });
          }}
        >
          <fieldset
            className="llm-fields"
            disabled={disabled || listing === null || listing.connections.length >= 8}
          >
            <legend>{t("llm.add")}</legend>
            <div className="settings-actions">
              <button
                type="button"
                className="btn txt sm"
                onClick={() =>
                  change({ base_url: "http://127.0.0.1:11434/v1", api_mode: "chat_completions" })
                }
              >
                {t("llm.local")}
              </button>
              <button
                type="button"
                className="btn txt sm"
                onClick={() =>
                  change({ base_url: "https://api.openai.com/v1", api_mode: "responses" })
                }
              >
                {t("llm.cloud")}
              </button>
            </div>
            <p className="settings-row-desc">{t("llm.local_help")}</p>
            {field("name", t("llm.name"), 160)}
            {field("base_url", t("llm.url"), 2048)}
            <label className="llm-field">
              <span>{t("llm.mode")}</span>
              <select
                className="input"
                value={draft.api_mode}
                onChange={(event) => change({ api_mode: event.target.value as ModelApiMode })}
              >
                <option value="chat_completions">Chat Completions</option>
                <option value="responses">Responses API</option>
                <option value="anthropic">Anthropic Messages</option>
              </select>
            </label>
            {field("model", t("llm.model"), 1024)}
            <datalist id={`${id}-models`}>
              {models.map((model) => (
                <option key={model} value={model} />
              ))}
            </datalist>
            {field("api_key", t("llm.key"), 4096)}
            <p className="settings-row-desc">{t("llm.test_help")}</p>
            <div className="settings-actions">
              <button
                type="button"
                className="btn alt sm"
                disabled={!draft.base_url}
                onClick={() =>
                  void run("discover", async () => {
                    const controller = new AbortController();
                    testAbort.current = controller;
                    const found = await api.discover(draft, controller.signal);
                    if (alive.current) {
                      setModels(found);
                      const first = found[0];
                      if (!draft.model && first)
                        setDraft((current) => ({ ...current, model: first }));
                    }
                  })
                }
              >
                {busy === "discover" ? t("llm.discovering") : t("llm.models")}
              </button>
              <button
                type="button"
                className="btn alt sm"
                disabled={!draft.name.trim() || !draft.base_url || !draft.model}
                onClick={() =>
                  void run("test", async () => {
                    const controller = new AbortController();
                    testAbort.current = controller;
                    const found = await api.test(draft, controller.signal);
                    if (alive.current) {
                      setModels(found);
                      setTested(true);
                    }
                  })
                }
              >
                {busy === "test" ? t("llm.testing") : t("llm.test")}
              </button>
              <button type="submit" className="btn alt sm">
                {busy === "save" ? t("llm.saving") : t("llm.save")}
              </button>
            </div>
          </fieldset>
        </form>
        {tested && <p role="status">{t("llm.tested")}</p>}
      </details>
      {error && <p role="alert">{error === "stale_revision" ? t("llm.stale") : t("llm.failed")}</p>}
      {error && (
        <button
          type="button"
          className="btn txt sm"
          disabled={busy !== null}
          onClick={() => void run("reload", reload)}
        >
          {t("llm.reload")}
        </button>
      )}
    </section>
  );
}
