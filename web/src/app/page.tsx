"use client";
import { useEffect, useState } from "react";
import { AuthForm } from "@/components/auth/AuthForm";
import { ChatView } from "@/components/chat/ChatView";
import { MemoryPanel } from "@/components/memory-panel/MemoryPanel";
import { api, clearToken, getToken } from "@/services/api";

export default function Home() {
  const [ready, setReady] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    // 光看 localStorage 里有没有 token 是不够的——JWT 会过期，而过期的 token
    // 长得跟有效的一模一样。只判存在的话页面会进到主界面，然后每个请求都 401，
    // 用户看到的是一个功能全坏但没告诉他为什么的界面，且没有回到登录页的路。
    //
    // 拿 /me 探一次：能过就是有效，401 就清掉并回登录页。
    if (!getToken()) { setReady(true); return; }
    api("/me")
      .then(() => setAuthenticated(true))
      .catch(() => clearToken())
      .finally(() => setReady(true));
  }, []);

  if (!ready) return null;
  if (!authenticated) return <AuthForm onDone={() => setAuthenticated(true)} />;
  return <main className="flex h-screen overflow-hidden"><ChatView onChanged={() => setRefresh(x => x + 1)} /><MemoryPanel refreshKey={refresh} /></main>;
}
