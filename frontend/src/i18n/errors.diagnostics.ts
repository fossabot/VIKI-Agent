/**
 * The diagnostics module's two error codes, which `ProvidersPage`'s check buttons can receive.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "error.unsupported": "This check cannot run right now.",
    "error.check_in_progress": "Another check is running.",
  },
});
