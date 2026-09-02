import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "FitMind AI", description: "有记忆的 AI 健身助理" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // suppressHydrationWarning 只挡 <html> 这一个元素自身的属性差异（作用范围一层深，
  // 不会掩盖应用内部真正的水合 bug）。
  //
  // 需要它是因为浏览器扩展会在 HTML 送达后、React 水合前往 <html> 上注入属性
  // （实测见过 data-web-terminal-link-bridge）。服务端吐出的是干净的
  // <html lang="zh-CN">，属性是客户端加的，代码里无从消除——只能让 React 接受
  // DOM 现状。否则 React 会判定水合失败并从最近的边界整棵重新客户端渲染，
  // 那才是真的有代价。
  return <html lang="zh-CN" suppressHydrationWarning><body>{children}</body></html>;
}
