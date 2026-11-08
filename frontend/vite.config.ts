import { defineConfig, loadEnv } from "vite";
import { readFileSync } from "node:fs";

// GitHub Pages project sites are served from /<repo>/. Set VITE_BASE in the
// workflow (or a local .env). Same-origin localhost dev uses "/" and the proxy.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const base = env.VITE_BASE || "/";
  return {
    base,
    server: {
      ...(process.env.PF_DEV_TLS_CERT && process.env.PF_DEV_TLS_KEY
        ? {
            https: {
              cert: readFileSync(process.env.PF_DEV_TLS_CERT),
              key: readFileSync(process.env.PF_DEV_TLS_KEY),
            },
          }
        : {}),
      proxy: {
        "/api": {
          target: process.env.PF_DEV_API_ORIGIN || "http://127.0.0.1:8000",
          changeOrigin: false,
        },
      },
    },
  };
});
