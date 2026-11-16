import { createContext } from "react";
import type { AssetsApi } from "../clients/media/assetsApi";

/** An authenticated client, never a token or an asset URL supplied by a message. */
export const MediaContext = createContext<AssetsApi | null>(null);
