import type { NextConfig } from "next";

// Static export only: the UI is bundled into the Tauri shell as frontendDist
// (apps/web/out). No server features, no image optimisation, no rewrites.
const nextConfig: NextConfig = {
  output: "export",
  // Emit `meetings/view/index.html` rather than `meetings/view.html` so the
  // Tauri asset resolver and any static server find every route.
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
  // Always define the mock flag so it is inlined at build time and the mock
  // sidecar (lib/mock.ts) is dead-code-eliminated unless explicitly "1".
  env: {
    NEXT_PUBLIC_SEKRETAER_MOCK: process.env.NEXT_PUBLIC_SEKRETAER_MOCK === "1" ? "1" : "0",
  },
  poweredByHeader: false,
};

export default nextConfig;
