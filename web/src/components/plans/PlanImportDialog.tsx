"use client";
import { ChangeEvent, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, FileSpreadsheet, Loader2, Upload, X } from "lucide-react";
import { upload } from "@/services/api";

type ProfileUpdate = { field: string; label: string; value: string | number };
type PlanDay = {
  day: number; day_type: string; focus?: string;
  carb_g?: number; protein_g?: number; fat_g?: number; kcal?: number; balance_kcal?: number;
};
type Preview = {
  week_count: number; day_count: number; carb_cycle: boolean;
  weeks: { week: number; days: PlanDay[] }[];
  basics: Record<string, string | number>;
  profile_updates: ProfileUpdate[];
  warnings: string[];
};

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];
const DAY_TYPE_LABELS: Record<string, string> = {
  high: "高碳", medium: "中碳", low: "低碳", rest: "休息",
};
const DAY_TYPE_STYLES: Record<string, string> = {
  high: "bg-orange-100 text-orange-700",
  medium: "bg-blue-100 text-blue-700",
  low: "bg-slate-200 text-slate-600",
  rest: "bg-emerald-100 text-emerald-700",
};

export function PlanImportDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const [file, setFile] = useState<File>();
  const [preview, setPreview] = useState<Preview>();
  const [applyProfile, setApplyProfile] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<string>();
  const inputRef = useRef<HTMLInputElement>(null);

  async function pick(event: ChangeEvent<HTMLInputElement>) {
    const picked = event.target.files?.[0];
    if (!picked) return;
    setFile(picked);
    setPreview(undefined);
    setError("");
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", picked);
      setPreview(await upload<Preview>("/plans/import/preview", form));
    } catch (err) {
      setError(err instanceof Error ? err.message : "解析失败");
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      // 后端读的是 multipart 表单字段，布尔值要序列化成字符串。
      form.append("apply_profile", String(applyProfile));
      const result = await upload<{ created: boolean; week_count: number; day_count: number }>(
        "/plans/import", form,
      );
      setDone(
        result.created
          ? `已导入 ${result.week_count} 周、${result.day_count} 天的计划。`
          : "这份计划之前已经导入过了，没有重复添加。",
      );
      onImported();
    } catch (err) {
      setError(err instanceof Error ? err.message : "导入失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
      <div className="flex max-h-[88vh] w-full max-w-3xl flex-col rounded-2xl bg-white shadow-2xl">
        <header className="flex items-center justify-between border-b border-slate-200 px-6 py-4">
          <div className="flex items-center gap-2">
            <FileSpreadsheet className="text-blue-600" size={18} />
            <h2 className="font-bold">导入计划表</h2>
          </div>
          <button onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-100"><X size={18} /></button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {done ? (
            <div className="py-10 text-center">
              <CheckCircle2 className="mx-auto text-emerald-500" size={40} />
              <p className="mt-3 font-medium">{done}</p>
              <p className="mt-1 text-sm text-slate-500">
                现在可以问我「今天吃多少」「这周三练什么」。
              </p>
              <button onClick={onClose} className="mt-5 rounded-lg bg-blue-600 px-5 py-2 text-sm text-white hover:bg-blue-700">
                知道了
              </button>
            </div>
          ) : (
            <>
              <button
                onClick={() => inputRef.current?.click()}
                disabled={busy}
                className="flex w-full items-center justify-center gap-2 rounded-xl border-2 border-dashed border-slate-300 py-8 text-sm text-slate-500 transition hover:border-blue-400 hover:text-blue-600 disabled:opacity-50"
              >
                {busy && !preview ? <Loader2 className="animate-spin" size={16} /> : <Upload size={16} />}
                {file ? file.name : "选择 xlsx 计划表"}
              </button>
              <input ref={inputRef} type="file" accept=".xlsx,.xlsm" onChange={pick} className="hidden" />
              <p className="mt-2 text-xs text-slate-400">
                每周一个工作表，第一行是星期，左侧一列是「碳日」「训练部位」「碳水(g)」这类标签。
              </p>

              {error && <p role="alert" className="mt-4 rounded bg-red-50 p-3 text-sm text-red-600">{error}</p>}

              {preview && (
                <div className="mt-5 space-y-5">
                  <div className="flex flex-wrap gap-2 text-sm">
                    <span className="rounded-full bg-blue-50 px-3 py-1 text-blue-700">
                      {preview.week_count} 周 / {preview.day_count} 天
                    </span>
                    {preview.carb_cycle && (
                      <span className="rounded-full bg-orange-50 px-3 py-1 text-orange-700">碳循环</span>
                    )}
                  </div>

                  {preview.warnings.length > 0 && (
                    <div className="rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
                      <p className="mb-1 flex items-center gap-1 font-medium"><AlertTriangle size={13} />有几处需要你确认</p>
                      <ul className="list-inside list-disc space-y-0.5">
                        {preview.warnings.map(w => <li key={w}>{w}</li>)}
                      </ul>
                    </div>
                  )}

                  {/* 整张表摆出来让用户核对。预览是数据进库前唯一能发现解析错位
                      的机会——解析器再怎么容错也可能读错行。 */}
                  {preview.weeks.map(week => (
                    <div key={week.week}>
                      <h3 className="mb-1.5 text-sm font-medium text-slate-700">第 {week.week} 周</h3>
                      <div className="overflow-x-auto rounded-lg border border-slate-200">
                        <table className="w-full text-xs">
                          <thead className="bg-slate-50 text-slate-500">
                            <tr>
                              <th className="px-2 py-1.5 text-left font-medium">周</th>
                              <th className="px-2 py-1.5 text-left font-medium">类型</th>
                              <th className="px-2 py-1.5 text-left font-medium">训练</th>
                              <th className="px-2 py-1.5 text-right font-medium">碳水</th>
                              <th className="px-2 py-1.5 text-right font-medium">蛋白</th>
                              <th className="px-2 py-1.5 text-right font-medium">脂肪</th>
                              <th className="px-2 py-1.5 text-right font-medium">热量</th>
                            </tr>
                          </thead>
                          <tbody>
                            {week.days.map(day => (
                              <tr key={day.day} className="border-t border-slate-100">
                                <td className="px-2 py-1.5 text-slate-500">{WEEKDAYS[day.day - 1] ?? day.day}</td>
                                <td className="px-2 py-1.5">
                                  <span className={`rounded px-1.5 py-0.5 ${DAY_TYPE_STYLES[day.day_type] ?? "bg-slate-100 text-slate-600"}`}>
                                    {DAY_TYPE_LABELS[day.day_type] ?? day.day_type}
                                  </span>
                                </td>
                                <td className="max-w-[140px] truncate px-2 py-1.5" title={day.focus}>{day.focus ?? "—"}</td>
                                <td className="px-2 py-1.5 text-right tabular-nums">{day.carb_g ?? "—"}</td>
                                <td className="px-2 py-1.5 text-right tabular-nums">{day.protein_g ?? "—"}</td>
                                <td className="px-2 py-1.5 text-right tabular-nums">{day.fat_g ?? "—"}</td>
                                <td className="px-2 py-1.5 text-right tabular-nums">{day.kcal ?? "—"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  ))}

                  {/* 档案改动单列一块，默认不勾。网上流传的模板都带着原作者的身体
                      数据，直接写进去等于把别人的体重当成自己的——而档案每轮都注入
                      prompt，之后所有热量计算都会按错的体重算。 */}
                  {preview.profile_updates.length > 0 && (
                    <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-slate-200 p-3 text-sm">
                      <input
                        type="checkbox" checked={applyProfile}
                        onChange={e => setApplyProfile(e.target.checked)}
                        className="mt-0.5 h-4 w-4"
                      />
                      <span>
                        <span className="font-medium">同时更新我的档案</span>
                        <span className="mt-1 block text-xs text-slate-500">
                          {preview.profile_updates.map(u => `${u.label}：${u.value}`).join("、")}
                        </span>
                        <span className="mt-1 block text-xs text-amber-700">
                          这份表里的数字可能来自模板作者，确认是你自己的再勾选。
                        </span>
                      </span>
                    </label>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        {!done && (
          <footer className="flex justify-end gap-2 border-t border-slate-200 px-6 py-4">
            <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-slate-600 hover:bg-slate-100">取消</button>
            <button
              onClick={confirm}
              disabled={!preview || busy}
              className="flex items-center gap-2 rounded-lg bg-blue-600 px-5 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40"
            >
              {busy && preview && <Loader2 className="animate-spin" size={15} />}
              确认导入
            </button>
          </footer>
        )}
      </div>
    </div>
  );
}
