"use client";
import { useEffect, useState } from "react";
import { AuthForm } from "@/components/auth/AuthForm";
import { ChatView } from "@/components/chat/ChatView";
import { MemoryPanel } from "@/components/memory-panel/MemoryPanel";
import { getToken } from "@/services/api";

export default function Home() {
  const [ready, setReady] = useState(false); const [authenticated,setAuthenticated]=useState(false); const [refresh,setRefresh]=useState(0);
  useEffect(()=>{setAuthenticated(Boolean(getToken()));setReady(true);},[]);
  if(!ready) return null;
  if(!authenticated) return <AuthForm onDone={()=>setAuthenticated(true)}/>;
  return <main className="flex h-screen overflow-hidden"><ChatView onChanged={()=>setRefresh(x=>x+1)}/><MemoryPanel refreshKey={refresh}/></main>;
}
