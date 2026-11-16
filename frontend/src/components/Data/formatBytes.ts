/**
 * A byte count from `src/viki_agent/app/data_api.py`'s filesystem walk (`LocationsPanel`), formatted
 * for a table cell. Binary units (1024): every count here comes from `os.scandir`/`st_size`, not
 * a network transfer, so the OS's own unit ladder is the one the user already reads on disk.
 */
const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${UNITS[unit]}`;
}
