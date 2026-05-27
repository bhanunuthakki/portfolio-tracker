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
    // Honor PORT env var so tooling that assigns a port (preview MCP,
    // CI runners) can override the default 5173. Default to 5173 for
    // the day-to-day `npm run dev` flow.
    port: Number(process.env.PORT) || 5173,
    // strictPort=false lets Vite fall back to the next free port if our
    // configured one is taken; combined with the PORT override above
    // we get clean behavior in both the assigned-port (preview MCP)
    // case and the user-running-something-else case.
    strictPort: false,
    // Bind on both IPv4 and IPv6 so `localhost` works regardless of how the
    // browser resolves it. Without this Vite defaults to IPv6-only on some
    // Windows setups, leading to ERR_CONNECTION_REFUSED in browsers that
    // try 127.0.0.1 first.
    host: true,
    proxy: {
      "/api": {
        // Proxy to 127.0.0.1 explicitly so we never depend on IPv6/IPv4
        // resolution when forwarding to FastAPI. Honor BACKEND_TARGET so
        // parallel worktrees / preview MCP can point at a custom uvicorn
        // when 8000 is occupied by another worktree's API.
        target: process.env.BACKEND_TARGET || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
