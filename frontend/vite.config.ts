// Builds three small pages (login, chat, ops) into the Python package, where
// the FastAPI app serves them at /login, /chat and /ops.
//
//   npm run dev     # http://localhost:5173, API calls proxied to `sn-agent web` on :8080
//   npm run build   # writes src/sn_agent/web/ui/
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

const BACKEND = process.env.SN_AGENT_URL || "http://127.0.0.1:8080";

// In dev, serve /chat and /ops like the backend does (GET /login is below).
function pageRoutes(): Plugin {
  return {
    name: "page-routes",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const path = (req.url || "").split("?")[0];
        if (req.method === "GET" && (path === "/chat" || path === "/ops" || path === "/")) {
          req.url = (path === "/" ? "/chat" : path) + ".html" + (req.url || "").slice(path.length);
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), pageRoutes()],
  build: {
    outDir: resolve(import.meta.dirname, "../src/sn_agent/web/ui"),
    emptyOutDir: true,
    // livekit-client alone is about 500 kB; it is one page, loaded once.
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      input: {
        login: resolve(import.meta.dirname, "login.html"),
        chat: resolve(import.meta.dirname, "chat.html"),
        ops: resolve(import.meta.dirname, "ops.html"),
      },
    },
  },
  server: {
    proxy: {
      "/api": BACKEND,
      "/logout": BACKEND,
      // GET shows the React page; POST (the form) goes to the backend.
      "/login": { target: BACKEND, bypass: (req) => (req.method === "GET" ? "/login.html" : undefined) },
    },
  },
});
