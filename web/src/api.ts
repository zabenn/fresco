// A typed client for the API; the types in schema.d.ts are generated from its OpenAPI
// schema with `pnpm openapi`.
import createClient from "openapi-fetch";
import type { Readable } from "openapi-typescript-helpers";
import type { components, paths } from "./schema";

export const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
export const api = createClient<paths>({ baseUrl: API_URL });
// As openapi-fetch returns it: Readable<> turns the x and y tuples into number[].
export type ExtractionResult = Readable<components["schemas"]["ExtractionResult"]>;

// FastAPI errors carry a `detail` string, or a list of validation errors.
export function errorMessage(error: unknown): string {
  const detail = (error as { detail?: unknown })?.detail;
  return typeof detail === "string" ? detail : JSON.stringify(detail ?? error);
}
