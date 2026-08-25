import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "FitMind AI", description: "有记忆的 AI 健身助理" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
