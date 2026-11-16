import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The gateway serves the built client itself; in dev mode requests are proxied to it.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8080", ws: true }, "/demo": "http://127.0.0.1:8080" } },
});
