import type { ReactNode } from "react";
import { t } from "../../i18n/t";
import type { OverviewNote, VaultEntry } from "../../state/vaultApi";
import { NoteCard, type NoteCardKind } from "./NoteCard";

export interface RelatedLink {
  target: string;
  state: string;
  candidates: string[];
}

export interface RelatedBacklink {
  source: string;
  line: number;
}

export function RelatedNotes({
  links,
  backlinks,
  backlinkPhase,
  backlinkStatus,
  listing,
  overview,
  onOpen,
}: {
  links: readonly RelatedLink[];
  backlinks: readonly RelatedBacklink[];
  backlinkPhase: "loading" | "ready" | "partial" | "error";
  backlinkStatus: ReactNode;
  listing: ReadonlyMap<string, Pick<VaultEntry, "title" | "modified">>;
  overview: ReadonlyMap<string, OverviewNote>;
  onOpen: (ref: string, kind: "link" | "backlink") => void;
}) {
  const visibleLinks = links.filter(
    (link) => link.state === "resolved" || link.state === "ambiguous" || link.state === "missing",
  );
  const openCard = (ref: string, kind: NoteCardKind) => {
    if (kind === "link" || kind === "backlink") onOpen(ref, kind);
  };
  const card = (ref: string, kind: "link" | "backlink") => {
    const listed = listing.get(ref);
    const metadata = overview.get(ref);
    return (
      <NoteCard
        noteRef={ref}
        {...(listed ? { listing: listed } : {})}
        {...(metadata ? { overview: metadata } : {})}
        variant="compact"
        kind={kind}
        onOpen={openCard}
      />
    );
  };

  return (
    <section aria-label={t("vault.reader.related")} className="vault-related">
      <h3>{t("vault.reader.related")}</h3>
      {visibleLinks.length > 0 ? (
        <section aria-label="Links">
          <h4>{t("vault.reader.links")}</h4>
          <ul>
            {visibleLinks.flatMap((link) =>
              link.state === "missing" ? (
                <li key={link.target} className="vault-related-missing">
                  {`${link.target} (no note yet)`}
                </li>
              ) : (
                link.candidates.map((candidate) => (
                  <li key={`${link.target}\0${candidate}`}>{card(candidate, "link")}</li>
                ))
              ),
            )}
          </ul>
        </section>
      ) : null}
      <section aria-label="Backlinks">
        <h4>{t("vault.reader.backlinks")}</h4>
        {backlinkStatus}
        {backlinkPhase !== "loading" && backlinkPhase !== "error" && backlinks.length === 0 ? (
          <p>
            {backlinkPhase === "partial"
              ? "No backlinks found in the scanned results."
              : "No backlinks found."}
          </p>
        ) : null}
        {backlinks.length > 0 ? (
          <ul>
            {backlinks.map((link) => (
              <li key={link.source}>{card(link.source, "backlink")}</li>
            ))}
          </ul>
        ) : null}
      </section>
    </section>
  );
}
