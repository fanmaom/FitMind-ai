"use client";
import { FormEvent, useState } from "react";
import { Activity, KeyRound, Loader2, UserPlus } from "lucide-react";
import { API_BASE, parseApiError, setToken } from "@/services/api";

// 与后端 RegisterRequest 的约束保持一致（schemas/auth.py）。
// 写在界面上而不是等提交后报错：密码要求是注册时最容易踩、也最容易提前说清的
// 一件事，让用户填完才被拒是没必要的往返。
const MIN_PASSWORD_LENGTH = 8;

type Mode = "login" | "register";

export function AuthForm({ onDone }: { onDone: () => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const registering = mode === "register";

  function switchTo(next: Mode) {
    setMode(next);
    // 切换表单时清掉上一个模式的报错。留着的话，「邮箱已注册」会跟着用户跳到
    // 登录页继续显示，看起来像是登录本身失败了。
    setError("");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError("");
    const form = new FormData(event.currentTarget);
    const password = String(form.get("password") ?? "");

    // 浏览器的 minLength 只在原生校验里生效，粘贴、密码管理器自动填充或
    // 禁用校验都能绕过。这里再拦一次，把提示留在本地而不是换成一次 422。
    if (registering && password.length < MIN_PASSWORD_LENGTH) {
      setError(`密码至少 ${MIN_PASSWORD_LENGTH} 位`);
      setLoading(false);
      return;
    }

    try {
      const response = await fetch(`${API_BASE}/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: form.get("email"), password }),
      });
      // 只读一次 body：Response 的正文是一次性流，读两遍会抛
      // "body stream already read"。成功和失败分支共用这一份数据。
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(parseApiError(body, registering ? "注册失败" : "登录失败"));
      }
      setToken(body.access_token);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : registering ? "注册失败" : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-950 via-slate-900 to-blue-950 p-6">
      <div className="w-full max-w-md rounded-2xl border border-white/10 bg-white p-8 shadow-2xl">
        <div className="mb-6 flex items-center gap-3">
          <div className="rounded-xl bg-blue-600 p-3 text-white"><Activity /></div>
          <div>
            <h1 className="text-2xl font-bold">FitMind AI</h1>
            <p className="text-sm text-slate-500">有记忆的私人健身助理</p>
          </div>
        </div>

        {/* 两个模式做成并排分段控件，而不是藏在表单底部的一行文字链接。
            新用户第一眼要能看见「创建账号」是个可选项——否则会以为这是内部
            系统、必须有人给他开号。 */}
        <div className="mb-6 grid grid-cols-2 gap-1 rounded-xl bg-slate-100 p-1">
          {([["login", "登录", KeyRound], ["register", "创建账号", UserPlus]] as const).map(
            ([value, label, Icon]) => (
              <button
                key={value}
                type="button"
                onClick={() => switchTo(value)}
                aria-pressed={mode === value}
                className={`flex items-center justify-center gap-1.5 rounded-lg py-2 text-sm font-medium transition ${
                  mode === value ? "bg-white text-blue-700 shadow-sm" : "text-slate-500 hover:text-slate-700"
                }`}
              >
                <Icon size={15} />{label}
              </button>
            ),
          )}
        </div>

        <form onSubmit={submit} className="space-y-4">
          <label className="block text-sm">
            邮箱
            <input
              name="email" type="email" required autoComplete="email"
              placeholder="you@example.com"
              className="mt-1 w-full rounded-lg border px-3 py-2.5 outline-none focus:border-blue-500"
            />
          </label>
          <label className="block text-sm">
            密码
            <input
              name="password" type="password" required
              minLength={registering ? MIN_PASSWORD_LENGTH : undefined}
              // 注册与登录用不同的 autoComplete：浏览器据此决定是"生成新密码"
              // 还是"填充已存的那个"。都写 current-password 会让密码管理器在
              // 注册时填进旧密码。
              autoComplete={registering ? "new-password" : "current-password"}
              className="mt-1 w-full rounded-lg border px-3 py-2.5 outline-none focus:border-blue-500"
            />
            {registering && (
              <span className="mt-1 block text-xs text-slate-500">
                至少 {MIN_PASSWORD_LENGTH} 位
              </span>
            )}
          </label>

          {error && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-600">{error}</p>}

          <button
            disabled={loading}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 py-3 font-medium text-white transition hover:bg-blue-700 disabled:opacity-50"
          >
            {loading && <Loader2 className="animate-spin" size={16} />}
            {loading ? "请稍候…" : registering ? "创建我的账号" : "登录"}
          </button>
        </form>

        <p className="mt-5 text-center text-xs leading-relaxed text-slate-500">
          {registering
            ? "账号独立：训练记录、身体档案与长期记忆只属于你，其他人看不到。"
            : "第一次来？点上方「创建账号」，几秒就能开始。"}
        </p>
      </div>
    </main>
  );
}
