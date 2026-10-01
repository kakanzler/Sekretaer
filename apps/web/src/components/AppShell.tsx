"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { ConnectionGate } from "./ConnectionGate";
import { useConnection } from "./ConnectionProvider";

const NAV = [
  { href: "/", label: "会議一覧" },
  { href: "/meetings/new/", label: "新規会議" },
  { href: "/settings/", label: "設定" },
  { href: "/settings/privacy/", label: "プライバシー" },
];

function isCurrent(pathname: string, href: string): boolean {
  const norm = (p: string) => (p.endsWith("/") ? p : `${p}/`);
  if (href === "/") return pathname === "/" || norm(pathname).startsWith("/meetings/view/");
  return norm(pathname) === norm(href);
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const { mode, disconnectDev, client } = useConnection();
  return (
    <>
      <a href="#main" className="skip-link">
        本文へ移動
      </a>
      <header className="app-header">
        <span className="brand">Sekretär</span>
        <nav aria-label="メイン">
          <ul>
            {NAV.map((n) => (
              <li key={n.href}>
                <Link href={n.href} aria-current={isCurrent(pathname, n.href) ? "page" : undefined}>
                  {n.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        {mode === "dev" && client && (
          <button type="button" className="btn ghost small" onClick={disconnectDev}>
            開発接続を解除
          </button>
        )}
      </header>
      <main id="main" tabIndex={-1}>
        <ConnectionGate>{children}</ConnectionGate>
      </main>
    </>
  );
}
