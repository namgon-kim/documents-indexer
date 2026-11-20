import { defineConfig } from "vite";

const pipeline = { target: "http://127.0.0.1:8000", changeOrigin: false };

export default defineConfig({
  // Relative asset paths let a proxy mount the built app under any path prefix.
  base: "./",
  server: { proxy: { "/v1": pipeline, "/health": pipeline } },
});
