import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the API so the browser talks to one loopback origin.
export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, proxy: { "/v1": "http://127.0.0.1:8765" } },
  build: { outDir: "dist", sourcemap: false },
});
