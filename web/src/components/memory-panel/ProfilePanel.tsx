"use client";

import { FormEvent, useEffect, useState } from "react";
import { Check, Pencil, X } from "lucide-react";
import { api } from "@/services/api";

type Profile = Record<string, unknown>;
type Draft = Record<string, string>;

const fields = [
  { key: "height_cm", label: "身高（cm）", kind: "number", step: "0.1" },
  { key: "weight_kg", label: "当前体重（kg）", kind: "number", step: "0.1" },
  { key: "age", label: "年龄", kind: "number", step: "1" },
  {
    key: "sex",
    label: "生理性别",
    kind: "select",
    options: [["male", "男"], ["female", "女"]],
  },
  {
    key: "activity",
    label: "日常活动量",
    kind: "select",
    options: [
      ["sedentary", "久坐"], ["light", "轻度"], ["moderate", "中等"],
      ["active", "活跃"], ["very_active", "非常活跃"],
    ],
  },
  { key: "target_kg", label: "目标体重（kg）", kind: "number", step: "0.1" },
  {
    key: "goal",
    label: "当前目标",
    kind: "select",
    options: [["cut", "减脂"], ["bulk", "增肌"], ["maintain", "维持"]],
  },
  { key: "phase_started_on", label: "周期开始", kind: "date" },
  { key: "training_split", label: "训练分化", kind: "text" },
  { key: "training_years", label: "训练年限", kind: "number", step: "0.5" },
  { key: "equipment", label: "可用器械", kind: "text" },
  { key: "lifts", label: "当前 1RM（JSON）", kind: "json" },
  { key: "injuries", label: "伤病（逗号分隔）", kind: "list" },
  { key: "dislikes", label: "忌口（逗号分隔）", kind: "list" },
  { key: "meal_scenarios", label: "用餐场景（JSON）", kind: "json" },
] as const;

const selectLabels: Record<string, string> = {
  male: "男", female: "女",
  sedentary: "久坐", light: "轻度", moderate: "中等",
  active: "活跃", very_active: "非常活跃",
  cut: "减脂", bulk: "增肌", maintain: "维持",
};

function toDraft(profile: Profile): Draft {
  return Object.fromEntries(fields.map((field) => {
    const value = profile[field.key];
    if (value == null) return [field.key, ""];
    if (field.kind === "list" && Array.isArray(value)) return [field.key, value.join("，")];
    if (field.kind === "json") return [field.key, JSON.stringify(value, null, 2)];
    return [field.key, String(value)];
  }));
}

export function ProfilePanel({
  profile,
  onSaved,
}: {
  profile: Profile;
  onSaved: (profile: Profile) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(() => toDraft(profile));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!editing) setDraft(toDraft(profile));
  }, [editing, profile]);

  async function save(event: FormEvent) {
    event.preventDefault();
    setError("");
    setSaving(true);
    try {
      const updates: Profile = {};
      for (const field of fields) {
        const raw = draft[field.key]?.trim() ?? "";
        if (!raw) {
          updates[field.key] = null;
        } else if (field.kind === "number") {
          const value = Number(raw);
          if (!Number.isFinite(value)) throw new Error(`${field.label}必须是数字`);
          updates[field.key] = value;
        } else if (field.kind === "list") {
          updates[field.key] = raw.split(/[，,、\n]/).map((item) => item.trim()).filter(Boolean);
        } else if (field.kind === "json") {
          try {
            updates[field.key] = JSON.parse(raw);
          } catch {
            throw new Error(`${field.label}不是有效的 JSON`);
          }
        } else {
          updates[field.key] = raw;
        }
      }
      const saved = await api<Profile>("/profile", {
        method: "PATCH",
        body: JSON.stringify(updates),
      });
      await onSaved(saved);
      setEditing(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return (
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <b className="text-sm">个人档案</b>
            <p className="text-xs text-slate-400">计划与建议会自动读取这些数据</p>
          </div>
          <button
            onClick={() => setEditing(true)}
            className="flex items-center gap-1 rounded-lg bg-blue-600 px-3 py-2 text-xs font-medium text-white"
          >
            <Pencil size={13} /> 编辑
          </button>
        </div>
        {fields.map((field) => {
          const value = profile[field.key];
          return (
            <div key={field.key} className="rounded-lg bg-slate-50 p-3">
              <div className="text-xs text-slate-500">{field.label.replace(/（.*?）/g, "")}</div>
              <div className="mt-1 break-words text-sm">
                {value == null || value === "" ? (
                  <span className="text-slate-400">未填写</span>
                ) : typeof value === "object" ? (
                  JSON.stringify(value)
                ) : (
                  selectLabels[String(value)] ?? String(value)
                )}
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <form onSubmit={save} className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <b className="text-sm">编辑个人档案</b>
          <p className="text-xs text-blue-600">修改当前体重会同步更新今日趋势</p>
        </div>
        <button
          type="button"
          onClick={() => { setEditing(false); setError(""); }}
          className="rounded p-1 text-slate-400 hover:bg-slate-100"
        >
          <X size={18} />
        </button>
      </div>

      {fields.map((field) => (
        <label key={field.key} className="block text-xs font-medium text-slate-600">
          {field.label}
          {field.kind === "select" ? (
            <select
              value={draft[field.key] ?? ""}
              onChange={(event) => setDraft((current) => ({ ...current, [field.key]: event.target.value }))}
              className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-blue-500"
            >
              <option value="">未填写</option>
              {field.options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          ) : field.kind === "json" || field.kind === "list" ? (
            <textarea
              value={draft[field.key] ?? ""}
              onChange={(event) => setDraft((current) => ({ ...current, [field.key]: event.target.value }))}
              rows={field.kind === "json" ? 3 : 2}
              className="mt-1 w-full resize-y rounded-lg border border-slate-200 px-3 py-2 font-normal text-sm outline-none focus:border-blue-500"
            />
          ) : (
            <input
              type={field.kind}
              step={"step" in field ? field.step : undefined}
              value={draft[field.key] ?? ""}
              onChange={(event) => setDraft((current) => ({ ...current, [field.key]: event.target.value }))}
              className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-normal text-sm outline-none focus:border-blue-500"
            />
          )}
        </label>
      ))}

      {error && <p className="rounded-lg bg-red-50 p-3 text-xs text-red-600">{error}</p>}
      <button
        disabled={saving}
        className="flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 py-2.5 text-sm font-medium text-white disabled:opacity-50"
      >
        <Check size={16} /> {saving ? "保存中…" : "保存档案"}
      </button>
    </form>
  );
}
