"use client";
import { LogOut, User } from "lucide-react";
import { clearToken } from "@/services/api";

/** 顶栏右侧的账号区：显示当前登录的是谁，并提供退出入口。
 *
 * 退出这件事此前没有出口——clearToken 定义了却没有任何调用方，登录后除了手动
 * 清 localStorage 没法换账号。对一个「每个人的数据只属于自己」的产品来说，
 * 能看见当前身份、能切换身份，和能注册是同一件事的两半。
 */
export function AccountMenu({ email }: { email?: string }) {
  function signOut() {
    clearToken();
    // 整页重载而不是切 React 状态：聊天、记忆面板、待办各自持有上一个账号的
    // 数据，逐个清理容易漏，漏掉的那份会串到下一个账号的界面上。重载是这里
    // 唯一能保证干净的做法。
    window.location.reload();
  }

  return (
    <div className="flex items-center gap-3">
      {email && (
        <span className="flex max-w-[220px] items-center gap-1.5 truncate text-xs text-slate-500" title={email}>
          <User size={13} className="shrink-0" />
          <span className="truncate">{email}</span>
        </span>
      )}
      <button
        onClick={signOut}
        className="flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs text-slate-600 transition hover:border-slate-300 hover:bg-slate-50"
      >
        <LogOut size={13} />退出
      </button>
    </div>
  );
}
