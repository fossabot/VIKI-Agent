export interface CaptureInboxRowProps {
  count: number;
  onOpen: () => void;
}

export function CaptureInboxRow({ count, onOpen }: CaptureInboxRowProps) {
  return (
    <section aria-label="Inbox captures">
      <p>{count === 1 ? "1 file waits in 00-inbox/." : `${count} files wait in 00-inbox/.`}</p>
      <button type="button" className="btn alt sm" onClick={onOpen}>
        Triage inbox
      </button>
    </section>
  );
}
