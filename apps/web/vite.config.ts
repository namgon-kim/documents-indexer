import { defineConfig } from "vite";

const pipeline = { target: "http://127.0.0.1:8000", changeOrigin: false };

export default defineConfig({
  server: { proxy: { "/v1": pipeline, "/health": pipeline } },
});
