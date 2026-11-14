import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false, target: "es2020" },
  server: {
    port: 5179,
    proxy: { "/api": "http://127.0.0.1:8099", "/login": "http://127.0.0.1:8099", "/logout": "http://127.0.0.1:8099" },
    fs: { allow: [".."] },
  },
});
