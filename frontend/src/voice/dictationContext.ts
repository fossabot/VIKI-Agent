import { createContext } from "react";
import type { DictationApi } from "./dictationTypes";

/** Provided by main.tsx; without it the Composer keeps its inert mic. */
export const DictationContext = createContext<DictationApi | null>(null);
