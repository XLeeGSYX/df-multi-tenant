/**
 * 根组件：SessionProvider 包裹；「对话」与「管理」两个视图切换。
 *
 * 无登录页 —— 挂载即自动选择租户并签发 token（见 session.tsx）。
 * 身份变化时 Chat 以 key 重挂载，线程/消息状态整体重置。
 */

import { useState } from "react";

import AdminView from "./AdminView";
import Chat from "./Chat";
import { SessionProvider, useSession } from "./session";

function Layout() {
  const [view, setView] = useState<"chat" | "admin">("chat");
  const session = useSession();
  const identityKey = `${session.tenantId ?? "none"}:${session.user ?? ""}`;

  return view === "chat" ? (
    <Chat key={identityKey} onOpenAdmin={() => setView("admin")} />
  ) : (
    <AdminView onBack={() => setView("chat")} />
  );
}

export default function App() {
  return (
    <SessionProvider>
      <Layout />
    </SessionProvider>
  );
}
