import "./globals.css";
import Link from "next/link";
import type { ReactNode } from "react";
import Notifications from "@/components/Notifications";

export const metadata = { title: "RentAgent", description: "AI rental broker working on your behalf" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <Link href="/" className="logo">🏠 RentAgent</Link>
          <span className="muted small">Your AI rental broker — every commitment needs your approval</span>
          <span className="spacer" />
          <Notifications />
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
