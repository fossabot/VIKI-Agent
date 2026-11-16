import { useEffect, useRef, useState } from "react";
import { type Capture, DESTINATIONS, type Destination } from "../../clients/vaults/capturesApi";
import { destinationLabel, type TriageChoice } from "./triage";

export interface CaptureCardProps {
  capture: Capture;
  choice: TriageChoice | null;
  projects: readonly string[];
  onChoose: (choice: TriageChoice | null) => void;
  onOpenNote?: () => void;
}

function unreadText(unread: string): string {
  if (unread === "too_large") return "Too large to preview here; VIKI can still read it.";
  if (unread === "budget") return "Not previewed this time (read limit).";
  return "Could not be read here.";
}

export function CaptureCard({ capture, choice, projects, onChoose, onOpenNote }: CaptureCardProps) {
  const [changing, setChanging] = useState(false);
  const [to, setTo] = useState<Destination["to"]>(DESTINATIONS[0]);
  const [project, setProject] = useState("");
  const undoRef = useRef<HTMLButtonElement>(null);
  const destinationRef = useRef<HTMLSelectElement>(null);
  const changeRef = useRef<HTMLButtonElement>(null);
  const pendingFocus = useRef(false);

  // Every action below removes the control the user just activated; move focus to whatever
  // replaced it, the way QuestionCard moves focus into its swapped input (Cards.tsx). pendingFocus
  // skips the first render, and only this card's own actions set it, so an unrelated choice update
  // (e.g. the dialog's "Accept all") never steals focus from elsewhere.
  useEffect(() => {
    if (!pendingFocus.current) return;
    pendingFocus.current = false;
    if (choice !== null) undoRef.current?.focus();
    else if (changing) destinationRef.current?.focus();
    else changeRef.current?.focus();
  }, [choice, changing]);

  function choose(destination: Destination, via: TriageChoice["via"]) {
    pendingFocus.current = true;
    onChoose({
      ref: capture.ref,
      revision: capture.revision,
      modified: capture.modified,
      destination,
      via,
    });
    setChanging(false);
  }
  function acceptSuggestion() {
    if (capture.suggestion === null) return;
    choose({ to: capture.suggestion.to, project: capture.suggestion.project }, "suggestion");
  }

  return (
    <li className="capture-card">
      <p className="capture-card-head">
        <code className="mono">{capture.ref}</code>
        <span>{capture.captured ?? `Modified ${capture.modified}`}</span>
      </p>
      <p className="capture-card-preview">
        {capture.unread !== null
          ? unreadText(capture.unread)
          : capture.cut
            ? `${capture.preview}…`
            : capture.preview}
      </p>
      {capture.suggestion !== null ? (
        <p className="capture-card-suggestion">
          <span>{`VIKI suggests: ${destinationLabel(capture.suggestion)}`}</span>{" "}
          <span className="dim">{capture.suggestion.why}</span>
        </p>
      ) : (
        <p className="capture-card-suggestion">No suggestion yet.</p>
      )}
      {choice !== null ? (
        <div className="capture-card-choice">
          <span className="chip">
            {choice.via === "suggestion" ? "Accepted" : "Chosen"}:{" "}
            {destinationLabel(choice.destination)}
          </span>
          <button
            type="button"
            ref={undoRef}
            className="btn alt sm"
            onClick={() => {
              pendingFocus.current = true;
              onChoose(null);
            }}
          >
            Undo
          </button>
        </div>
      ) : changing ? (
        <div className="capture-card-change">
          <label>
            Destination
            <select
              ref={destinationRef}
              value={to}
              onChange={(event) => setTo(event.target.value as Destination["to"])}
            >
              {DESTINATIONS.map((destination) =>
                destination === "task" ? (
                  <option key={destination} value={destination} disabled={projects.length === 0}>
                    {projects.length === 0
                      ? "No projects found in 10-projects/."
                      : "Next action in a project (10-projects/)"}
                  </option>
                ) : (
                  <option key={destination} value={destination}>
                    {destinationLabel({ to: destination, project: null })}
                  </option>
                ),
              )}
            </select>
          </label>
          {to === "task" ? (
            <label>
              Project
              <select value={project} onChange={(event) => setProject(event.target.value)}>
                <option value="">Choose a project</option>
                {projects.map((proj) => (
                  <option key={proj} value={proj}>
                    {proj}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <button
            type="button"
            className="btn alt sm"
            disabled={to === "task" && project === ""}
            onClick={() => choose({ to, project: to === "task" ? project : null }, "owner")}
          >
            Use
          </button>
          <button
            type="button"
            className="btn txt sm"
            onClick={() => {
              pendingFocus.current = true;
              setChanging(false);
            }}
          >
            Cancel
          </button>
        </div>
      ) : (
        <div className="capture-card-actions">
          <button
            type="button"
            className="btn alt sm"
            disabled={capture.suggestion === null}
            onClick={acceptSuggestion}
          >
            Accept
          </button>
          <button
            type="button"
            ref={changeRef}
            className="btn alt sm"
            onClick={() => {
              pendingFocus.current = true;
              setChanging(true);
            }}
          >
            Change
          </button>
          <button
            type="button"
            className="btn alt sm"
            onClick={() => choose({ to: "discard", project: null }, "owner")}
          >
            Discard
          </button>
        </div>
      )}
      {onOpenNote ? (
        <button type="button" className="btn alt sm" onClick={onOpenNote}>
          Open note
        </button>
      ) : null}
    </li>
  );
}
