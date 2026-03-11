import { defineConfig, devices } from "@playwright/test";

/**
 * HelmLog — Playwright E2E configuration
 *
 * The app server is started externally (e.g. `uv run uvicorn helmlog.web:app`)
 * and Playwright connects to it via BASE_URL.
 *
 * In CI the webServer block below starts the app automatically.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  outputDir: "./test-results",

  /* Fail the build on CI if you accidentally left test.only in the source */
  forbidOnly: !!process.env.CI,

  /* Retry on CI only */
  retries: process.env.CI ? 2 : 0,

  /* Parallel workers */
  workers: process.env.CI ? 1 : undefined,

  /* Reporters */
  reporter: process.env.CI
    ? [["html", { open: "never" }], ["github"]]
    : [["html", { open: "on-failure" }]],

  use: {
    baseURL: process.env.BASE_URL || "http://localhost:8000",

    /* Capture screenshot on failure */
    screenshot: "only-on-failure",

    /* Retain trace on failure for debugging */
    trace: "retain-on-failure",

    /* Record video on failure */
    video: "retain-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  /* Start the HelmLog dev server before running tests */
  webServer: {
    command: "uv run python tests/e2e/serve.py",
    url: "http://localhost:8000",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
