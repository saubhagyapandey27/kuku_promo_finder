// Centralized browser data boundary. All API requests join VITE_API_BASE_URL
// once; demo/input assets use Vite's configured BASE_URL for GitHub Pages.
//
// The public prototype never exposes passcode/private mode. Saved results are
// bundled and work offline; the API is only used for visitor-key New runs.

import type { Saved } from "./model";

const RAW_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) || "/api";
export const API_BASE = RAW_BASE.replace(/\/+$/, "");

/** True only when an API endpoint is explicitly configured (deploy) or local dev. */
export function apiEnabled(): boolean {
  if (import.meta.env.VITE_API_BASE_URL) return true;
  const host = typeof location !== "undefined" ? location.hostname : "";
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

async function request(path: string, init: RequestInit = {}): Promise<any> {
  const r = await fetch(API_BASE + path, { credentials: "include", ...init });
  if (!r.ok) {
    let m: unknown;
    try {
      m = (await r.json()).detail;
    } catch {
      m = "API unavailable";
    }
    throw Error(typeof m === "string" ? m : `Request failed (${r.status})`);
  }
  return r.json();
}

export const apiGet = (path: string, signal?: AbortSignal) =>
  request(path, { signal });

export const apiPost = (path: string, body?: unknown) =>
  request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const apiPostForm = (path: string, body: FormData) =>
  request(path, { method: "POST", body });

// Vite serves public/ assets under BASE_URL (e.g. /<repo>/ on GitHub Pages).
export const demoUrl = (name: string) =>
  `${import.meta.env.BASE_URL}demo/${name}`;

export async function loadRun(rid: string, signal?: AbortSignal): Promise<Saved> {
  return apiGet(`/runs/${rid}`, signal);
}

export type Caps = {
  formats: string[];
  profiles: string[];
  live_enabled: boolean;
  live_reason: string;
  visitor_enabled: boolean;
  visitor_reason: string;
  profile_credentials: Record<string, string[]>;
};

export const loadConfig = (signal?: AbortSignal): Promise<Caps> =>
  apiGet("/config", signal);
