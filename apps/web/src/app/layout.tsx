import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { AppShell } from "@/components/AppShell";
import { ConnectionProvider } from "@/components/ConnectionProvider";
import "./globals.css";

export const metadata: Metadata = {
  title: "Sekretär",
  description: "会議音声をローカルで文字起こしし、構造化ノートを更新する会議支援アプリ",
};

export const viewport: Viewport = {
  colorScheme: "light dark",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ja">
      <body>
        <ConnectionProvider>
          <AppShell>{children}</AppShell>
        </ConnectionProvider>
      </body>
    </html>
  );
}
