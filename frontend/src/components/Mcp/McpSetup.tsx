import { useId, useState } from "react";
import { t } from "../../i18n/t";
import { CopyButton } from "../Conversation/Copy";
import "./McpSetup.css";

const ATLASSIAN_URL = "https://mcp.atlassian.com/v2/mcp";
const HTTP_JSON = JSON.stringify(
  { mcpServers: { "my-server": { type: "http", url: "https://example.com/mcp" } } },
  null,
  2,
);
const STDIO_JSON = JSON.stringify(
  { mcpServers: { "local-tools": { command: "example-mcp", args: ["--stdio"] } } },
  null,
  2,
);

/** Where new chats start: the active workspace's root, or null when it is not known. */
export interface McpStart {
  path: string | null;
  chatOnly: boolean;
}

const WHERE_NOTES = ["code", "added", "new_chat", "switches", "profile"] as const;

/** Setup commands are copied for the user's terminal; this view starts no process. */
export function McpSetup({ start }: { start?: McpStart }) {
  const headingId = useId();
  const whereId = useId();
  const [scope, setScope] = useState("local");
  const [custom, setCustom] = useState(false);
  const command = `claude mcp add --transport http --scope ${scope} atlassian ${ATLASSIAN_URL}`;

  return (
    <section className="mcp-setup" aria-labelledby={headingId}>
      <h3 id={headingId}>{t("mcp.setup.title")}</h3>
      <p>{t("mcp.setup.inherited")}</p>
      {start ? (
        <section aria-labelledby={whereId}>
          <h4 id={whereId}>{t("mcp.where.title")}</h4>
          {start.path !== null ? (
            <>
              <p>{t("mcp.where.start")}</p>
              <pre>
                <code>{start.path}</code>
              </pre>
              <CopyButton
                label={t("mcp.where.copy_path")}
                getText={() => start.path ?? ""}
                className="btn alt sm"
              />
            </>
          ) : (
            <p>{start.chatOnly ? t("mcp.where.chat_only") : t("mcp.where.start_unknown")}</p>
          )}
          <ul>
            {WHERE_NOTES.map((note) => (
              <li key={note}>{t(`mcp.where.${note}`)}</li>
            ))}
          </ul>
        </section>
      ) : null}
      <h4>{t("mcp.setup.atlassian")}</h4>
      <label>
        {t("mcp.setup.scope")}
        <select value={scope} onChange={(event) => setScope(event.target.value)}>
          {(["local", "user", "project"] as const).map((value) => (
            <option key={value} value={value}>
              {t(`mcp.setup.scope.${value}`)}
            </option>
          ))}
        </select>
      </label>
      <p>{t("mcp.setup.folder")}</p>
      <pre>
        <code>{command}</code>
      </pre>
      <CopyButton
        key={scope}
        label={t("mcp.setup.copy_command")}
        getText={() => command}
        className="btn alt sm"
      />
      <ol>
        <li>{t("mcp.setup.authorize")}</li>
        <li>{t("mcp.setup.restart")}</li>
      </ol>
      <p>{t("mcp.setup.refresh")}</p>
      <button
        type="button"
        className="btn txt sm"
        aria-expanded={custom}
        aria-controls={`${headingId}-json`}
        onClick={() => setCustom(!custom)}
      >
        {t("mcp.setup.json")}
      </button>
      <div id={`${headingId}-json`} hidden={!custom}>
        <p>{t("mcp.setup.json_help")}</p>
        <h4>{t("mcp.setup.http")}</h4>
        <p>{t("mcp.setup.http_help")}</p>
        <pre>
          <code>{HTTP_JSON}</code>
        </pre>
        <CopyButton
          label={t("mcp.setup.copy_http")}
          getText={() => HTTP_JSON}
          className="btn alt sm"
        />
        <h4>{t("mcp.setup.stdio")}</h4>
        <p>{t("mcp.setup.stdio_help")}</p>
        <pre>
          <code>{STDIO_JSON}</code>
        </pre>
        <CopyButton
          label={t("mcp.setup.copy_stdio")}
          getText={() => STDIO_JSON}
          className="btn alt sm"
        />
      </div>
    </section>
  );
}
