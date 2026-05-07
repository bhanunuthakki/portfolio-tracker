import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    // Bind on both IPv4 and IPv6 so `localhost` works regardless of how the
    // browser resolves it. Without this Vite defaults to IPv6-only on some
    // Windows setups, leading to ERR_CONNECTION_REFUSED in browsers that
    // try 127.0.0.1 first.
    host: true,
    proxy: {
      "/api": {
        // Proxy to 127.0.0.1 explicitly so we never depend on IPv6/IPv4
        // resolution when forwarding to FastAPI.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
