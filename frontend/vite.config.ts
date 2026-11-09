import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev proxy keeps everything same-origin: no CORS, and the tenant JWT never
// has to be readable by anything outside this origin. Point the two ports at
// wherever `make admin` / `make runtime` are listening.
const runtimePort = process.env.DF_RUNTIME_PORT || "8001";
const adminPort = process.env.DF_ADMIN_PORT || "8010";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // agent-runtime (deerflow gateway, tenant-patched)
      "/api": { target: `http://127.0.0.1:${runtimePort}`, changeOrigin: true },
      // tenant-admin (management plane)
      "/admin": { target: `http://127.0.0.1:${adminPort}`, changeOrigin: true },
      "/healthz": { target: `http://127.0.0.1:${adminPort}`, changeOrigin: true },
    },
  },
});
