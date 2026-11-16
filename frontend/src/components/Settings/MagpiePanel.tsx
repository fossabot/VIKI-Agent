import { useContext, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { MagpieStatus, MagpieView } from "../../clients/app/magpieApi";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import { t, tf } from "../../i18n/t";
import { readMagpieView, watchMagpieView } from "./magpieView";
import "./MagpiePanel.css";

// Shown as text to copy by hand: the app never runs it (no in-app download of Magpie).
const PREPARE_COMMAND = "uv run python scripts/prepare_magpie.py";

interface EmbeddedView {
  view: MagpieView;
  ui: string | null;
  controller: AbortController;
  stop: () => void;
  pending: MagpieView | null;
  syncing: boolean;
  ready: boolean;
}

export function MagpiePanel({
  onConnect,
  connectionDisabled,
}: {
  onConnect: (baseUrl: string) => void;
  connectionDisabled: boolean;
}) {
  const api = useContext(ModelConnectionsContext)?.magpie;
  const [status, setStatus] = useState<MagpieStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [stage, setStage] = useState<{ id: string; target: HTMLElement } | null>(null);
  const request = useRef<AbortController | null>(null);
  const embedded = useRef<EmbeddedView | null>(null);
  const id = useId();

  useLayoutEffect(() => {
    if (!stage) return;
    const { target } = stage;
    const scroll = { top: target.scrollTop, left: target.scrollLeft };
    target.classList.add("magpie-stage-host");
    target.scrollTop = 0;
    target.scrollLeft = 0;
    return () => {
      target.classList.remove("magpie-stage-host");
      target.scrollTop = scroll.top;
      target.scrollLeft = scroll.left;
    };
  }, [stage]);

  function closeView(owner: EmbeddedView) {
    if (embedded.current !== owner) return;
    embedded.current = null;
    setStage((current) => (current?.id === owner.view.id ? null : current));
    owner.pending = null;
    owner.stop();
    owner.controller.abort();
    void api?.close(owner.ui, owner.view.id).catch(() => {});
  }

  async function updateView(owner: EmbeddedView, view: MagpieView) {
    if (!api || embedded.current !== owner) return;
    owner.view = view;
    owner.pending = view;
    if (owner.syncing) return;
    owner.syncing = true;
    try {
      while (owner.pending && embedded.current === owner) {
        const next = owner.pending;
        owner.pending = null;
        await api.view(
          owner.ui,
          { ...next, visible: owner.ready && next.visible },
          owner.controller.signal,
        );
      }
    } catch {
      if (!owner.controller.signal.aborted && embedded.current === owner) {
        setFailed(true);
        setBusy(false);
        closeView(owner);
      }
    } finally {
      owner.syncing = false;
    }
  }

  // The API identity owns these requests; cleanup closes only this component's view generation.
  // biome-ignore lint/correctness/useExhaustiveDependencies: request/view refs carry the current generation.
  useEffect(() => {
    const controller = new AbortController();
    const closed = (event: Event) => {
      const owner = embedded.current;
      if (owner && (event as CustomEvent<unknown>).detail === owner.view.id) {
        closeView(owner);
        setBusy(false);
      }
    };
    window.addEventListener("viki-magpie-closed", closed);
    if (api)
      void api.status(controller.signal).then(
        (next) => {
          if (!controller.signal.aborted) setStatus(next);
        },
        () => {
          if (!controller.signal.aborted) setFailed(true);
        },
      );
    return () => {
      controller.abort();
      request.current?.abort();
      const owner = embedded.current;
      if (owner) closeView(owner);
      window.removeEventListener("viki-magpie-closed", closed);
    };
  }, [api]);
  if (!api) return null;
  async function open() {
    if (!api || busy) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setFailed(false);
    let owner: EmbeddedView | null = null;
    try {
      const ui = new URLSearchParams(window.location.search).get("ui");
      if (status?.desktop) {
        const previous = embedded.current;
        if (previous) closeView(previous);
        const viewId = [...crypto.getRandomValues(new Uint8Array(16))]
          .map((byte) => byte.toString(16).padStart(2, "0"))
          .join("");
        const view = readMagpieView(viewId);
        const target = document.querySelector<HTMLElement>('main.app-view[data-region="view"]');
        if (!view || !target) throw new Error("Magpie view is unavailable.");
        owner = {
          view,
          ui,
          controller,
          stop: () => {},
          pending: null,
          syncing: false,
          ready: false,
        };
        embedded.current = owner;
        setStage({ id: view.id, target });
        const current = owner;
        owner.stop = watchMagpieView(view, (next) => void updateView(current, next));
      }
      const next = owner
        ? await api.open(ui, controller.signal, { ...owner.view, visible: false })
        : await api.open(ui, controller.signal);
      if (!controller.signal.aborted && (!owner || embedded.current === owner)) {
        if (owner) {
          owner.ready = true;
          void updateView(
            owner,
            readMagpieView(owner.view.id) ?? { ...owner.view, visible: false },
          );
        }
        setStatus(next);
      }
    } catch {
      if (!controller.signal.aborted) {
        setFailed(true);
        setBusy(false);
        if (owner) closeView(owner);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <>
      {stage &&
        createPortal(
          <div className="magpie-stage" role="status" aria-busy={busy}>
            <h2>{t("magpie.title")}</h2>
            <p>{busy ? t("magpie.opening") : t("magpie.help")}</p>
          </div>,
          stage.target,
        )}
      <section className="settings-details" aria-labelledby={`${id}-title`} aria-busy={busy}>
        <h4 id={`${id}-title`}>{t("magpie.title")}</h4>
        <p className="settings-row-desc">{t("magpie.help")}</p>
        <p className="settings-row-desc">{t("magpie.authority")}</p>
        <p className="settings-row-desc">{t("magpie.storage")}</p>
        {status && (
          <p className="settings-row-desc">
            {tf("magpie.version", { version: status.version })}
            {" · "}
            {status.desktop ? t("magpie.desktop") : t("magpie.browser")}
          </p>
        )}
        {status?.available === false && (
          <>
            <p role="status" className="settings-row-desc">
              {t("magpie.missing")}
            </p>
            <p className="settings-row-desc">{t("magpie.missing_source")}</p>
            <p className="settings-row-desc">
              <code className="mono">{PREPARE_COMMAND}</code>
            </p>
          </>
        )}
        <div className="settings-actions">
          <button
            type="button"
            className="btn alt sm"
            disabled={busy || !status?.available}
            onClick={() => void open()}
          >
            {busy ? t("magpie.opening") : t("magpie.open")}
          </button>
          <button
            type="button"
            className="btn txt sm"
            disabled={connectionDisabled || busy || !status?.available || !status.base_url}
            onClick={() => {
              if (status?.base_url) onConnect(status.base_url);
            }}
          >
            {t("magpie.connect")}
          </button>
        </div>
        <p className="settings-row-desc">{t("magpie.connect_help")}</p>
        {failed && <p role="alert">{t("magpie.failed")}</p>}
      </section>
    </>
  );
}
