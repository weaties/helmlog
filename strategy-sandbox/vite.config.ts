import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Fully offline, no network. Static single-page app.
export default defineConfig({
  plugins: [react()],
  base: "./",
});
