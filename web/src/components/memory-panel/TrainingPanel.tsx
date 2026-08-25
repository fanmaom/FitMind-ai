"use client";

import { FormEvent, useMemo, useState } from "react";
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  CirclePlus,
  Dumbbell,
  Minus,
  Plus,
  Save,
  X,
} from "lucide-react";
import { api } from "@/services/api";
import type { Workout } from "./MemoryPanel";

type SetDraft = { weight: string; reps: string; rpe: string };
type View = "history" | "create";

const commonExercises = ["卧推", "深蹲", "硬拉", "肩推", "划船", "引体向上", "高位下拉", "腿举", "二头弯举", "三头下压"];
const weekdays = ["一", "二", "三", "四", "五", "六", "日"];

function formatLocalDate(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function today() {
  return formatLocalDate(new Date());
}

function workoutVolume(workout: Workout) {
  return workout.sets.reduce(
    (sum, set) => sum + Number(set.weight) * Number(set.reps),
    0,
  );
}

function displayVolume(value: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value);
}

export function TrainingPanel({
  workouts,
  onCreated,
}: {
  workouts: Workout[];
  onCreated: () => Promise<void>;
}) {
  const now = new Date();
  const [view, setView] = useState<View>("history");
  const [visibleMonth, setVisibleMonth] = useState(() => new Date(now.getFullYear(), now.getMonth(), 1));
  const [date, setDate] = useState(today);
  const [exercise, setExercise] = useState("");
  const [sets, setSets] = useState<SetDraft[]>([{ weight: "", reps: "10", rpe: "" }]);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const workoutsByDate = useMemo(() => {
    const grouped = new Map<string, Workout[]>();
    for (const workout of workouts) {
      grouped.set(workout.date, [...(grouped.get(workout.date) ?? []), workout]);
    }
    return grouped;
  }, [workouts]);

  function updateSet(index: number, key: keyof SetDraft, value: string) {
    setSets((current) => current.map((set, itemIndex) =>
      itemIndex === index ? { ...set, [key]: value } : set,
    ));
  }

  function addSet() {
    const previous = sets.at(-1) ?? { weight: "", reps: "10", rpe: "" };
    setSets((current) => [...current, { ...previous }]);
  }

  function openCreate(day = date) {
    setDate(day);
    setNotice("");
    setError("");
    setView("create");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!exercise.trim()) return setError("请填写训练动作");

    let normalized: Array<{ weight: number; reps: number; rpe?: number }>;
    try {
      normalized = sets.map((set, index) => {
        const weight = Number(set.weight || 0);
        const reps = Number(set.reps);
        const rpe = set.rpe ? Number(set.rpe) : null;
        if (!Number.isFinite(weight) || weight < 0) throw new Error(`第 ${index + 1} 组重量无效`);
        if (!Number.isInteger(reps) || reps < 1 || reps > 100) throw new Error(`第 ${index + 1} 组次数无效`);
        if (rpe != null && (!Number.isFinite(rpe) || rpe < 1 || rpe > 10)) throw new Error(`第 ${index + 1} 组 RPE 应为 1–10`);
        return { weight, reps, ...(rpe == null ? {} : { rpe }) };
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "训练数据无效");
      return;
    }

    setSaving(true);
    try {
      const result = await api<{ deduplicated: boolean }>("/logs/workouts", {
        method: "POST",
        body: JSON.stringify({ date, exercise: exercise.trim(), sets: normalized }),
      });
      await onCreated();
      const selected = new Date(`${date}T00:00:00`);
      setVisibleMonth(new Date(selected.getFullYear(), selected.getMonth(), 1));
      setNotice(result.deduplicated ? "这条训练已经存在，没有重复添加。" : "训练已保存，月历和数据页已同步更新。" );
      setExercise("");
      setSets([{ weight: "", reps: "10", rpe: "" }]);
      setView("history");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  if (view === "create") {
    return (
      <div className="space-y-5">
        <div className="flex items-start justify-between">
          <div>
            <b className="text-sm">添加训练</b>
            <p className="mt-1 text-xs text-slate-400">按动作记录每组重量、次数与可选 RPE</p>
          </div>
          <button type="button" aria-label="返回训练月历" onClick={() => setView("history")} className="rounded p-1 text-slate-400 hover:bg-slate-100">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={submit} className="space-y-4">
          <label className="block text-xs font-medium text-slate-600">
            日期
            <input type="date" value={date} onChange={(event) => setDate(event.target.value)} required className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-blue-500" />
          </label>
          <label className="block text-xs font-medium text-slate-600">
            动作
            <input aria-label="训练动作" list="exercise-options" value={exercise} onChange={(event) => setExercise(event.target.value)} placeholder="例如：卧推" className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-blue-500" />
            <datalist id="exercise-options">{commonExercises.map((name) => <option key={name} value={name} />)}</datalist>
          </label>

          <div className="space-y-2">
            <div className="grid grid-cols-[28px_1fr_1fr_1fr_28px] gap-2 px-1 text-center text-[11px] text-slate-400">
              <span>组</span><span>kg</span><span>次数</span><span>RPE</span><span />
            </div>
            {sets.map((set, index) => (
              <div key={index} className="grid grid-cols-[28px_1fr_1fr_1fr_28px] items-center gap-2">
                <span className="text-center text-xs text-slate-500">{index + 1}</span>
                <input aria-label={`第 ${index + 1} 组重量`} type="number" min="0" max="1000" step="0.5" value={set.weight} onChange={(event) => updateSet(index, "weight", event.target.value)} placeholder="0" className="min-w-0 rounded-lg border px-2 py-2 text-center text-sm" />
                <input aria-label={`第 ${index + 1} 组次数`} type="number" min="1" max="100" step="1" value={set.reps} onChange={(event) => updateSet(index, "reps", event.target.value)} className="min-w-0 rounded-lg border px-2 py-2 text-center text-sm" />
                <input aria-label={`第 ${index + 1} 组 RPE`} type="number" min="1" max="10" step="0.5" value={set.rpe} onChange={(event) => updateSet(index, "rpe", event.target.value)} placeholder="可选" className="min-w-0 rounded-lg border px-2 py-2 text-center text-sm" />
                <button type="button" aria-label={`删除第 ${index + 1} 组`} disabled={sets.length === 1} onClick={() => setSets((current) => current.filter((_, itemIndex) => itemIndex !== index))} className="text-slate-400 disabled:opacity-20"><Minus size={16} /></button>
              </div>
            ))}
            <button type="button" onClick={addSet} className="flex w-full items-center justify-center gap-1 rounded-lg border border-dashed border-slate-300 py-2 text-xs text-slate-500 hover:border-blue-400 hover:text-blue-600">
              <CirclePlus size={14} /> 添加一组
            </button>
          </div>

          {error && <p className="rounded-lg bg-red-50 p-3 text-xs text-red-600">{error}</p>}
          <button disabled={saving} className="flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 py-2.5 text-sm font-medium text-white disabled:opacity-50">
            <Save size={16} /> {saving ? "保存中…" : "保存训练"}
          </button>
        </form>
      </div>
    );
  }

  const year = visibleMonth.getFullYear();
  const month = visibleMonth.getMonth();
  const leadingBlanks = (new Date(year, month, 1).getDay() + 6) % 7;
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const selectedWorkouts = workoutsByDate.get(date) ?? [];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <b className="flex items-center gap-2 text-sm"><CalendarDays size={16} />训练记录</b>
          <p className="mt-1 text-xs text-slate-400">按月查看容量，点击日期查看详情</p>
        </div>
        <button type="button" onClick={() => openCreate()} className="flex items-center gap-1 rounded-lg bg-blue-600 px-3 py-2 text-xs font-medium text-white">
          <Plus size={14} /> 添加训练
        </button>
      </div>

      {notice && <p className="rounded-lg bg-green-50 p-3 text-xs text-green-700">{notice}</p>}

      <section className="rounded-xl border border-slate-200 p-3">
        <div className="mb-3 flex items-center justify-between">
          <button type="button" aria-label="上个月" onClick={() => setVisibleMonth(new Date(year, month - 1, 1))} className="rounded p-1.5 text-slate-500 hover:bg-slate-100"><ChevronLeft size={18} /></button>
          <b className="text-sm">{year} 年 {month + 1} 月</b>
          <button type="button" aria-label="下个月" onClick={() => setVisibleMonth(new Date(year, month + 1, 1))} className="rounded p-1.5 text-slate-500 hover:bg-slate-100"><ChevronRight size={18} /></button>
        </div>
        <div className="grid grid-cols-7 text-center text-[11px] font-medium text-slate-400">
          {weekdays.map((weekday) => <span key={weekday} className="pb-2">{weekday}</span>)}
        </div>
        <div className="grid grid-cols-7 gap-1">
          {Array.from({ length: leadingBlanks }).map((_, index) => <span key={`blank-${index}`} />)}
          {Array.from({ length: daysInMonth }).map((_, index) => {
            const day = index + 1;
            const dayKey = formatLocalDate(new Date(year, month, day));
            const dayWorkouts = workoutsByDate.get(dayKey) ?? [];
            const volume = dayWorkouts.reduce((sum, workout) => sum + workoutVolume(workout), 0);
            const exercises = [...new Set(dayWorkouts.map((workout) => workout.exercise))];
            const active = dayKey === date;
            const isToday = dayKey === today();
            return (
              <button
                type="button"
                key={dayKey}
                aria-label={`${dayKey}${dayWorkouts.length ? `，${dayWorkouts.length} 条训练` : ""}`}
                onClick={() => setDate(dayKey)}
                className={`min-h-16 overflow-hidden rounded-lg border p-1 text-left transition ${active ? "border-blue-500 bg-blue-50" : "border-transparent hover:bg-slate-50"}`}
              >
                <span className={`mx-auto flex h-5 w-5 items-center justify-center rounded-full text-[11px] ${isToday ? "bg-blue-600 text-white" : "text-slate-600"}`}>{day}</span>
                {volume > 0 && <span className="mt-1 block truncate rounded bg-emerald-100 px-1 text-center text-[9px] font-semibold text-emerald-700">{displayVolume(volume)}</span>}
                {exercises.slice(0, 1).map((name) => <span key={name} className="mt-0.5 block truncate text-center text-[9px] text-slate-500">{name}</span>)}
              </button>
            );
          })}
        </div>
      </section>

      <section>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2"><Dumbbell size={15} /><b className="text-sm">{date} 的训练</b></div>
          <button type="button" onClick={() => openCreate(date)} className="text-xs font-medium text-blue-600">在这天添加</button>
        </div>
        <div className="mt-2 space-y-2">
          {selectedWorkouts.map((workout) => (
            <div key={workout.id} className="rounded-lg bg-slate-50 p-3 text-sm">
              <div className="flex items-center justify-between"><b>{workout.exercise}</b><span className="text-xs font-medium text-emerald-600">{displayVolume(workoutVolume(workout))} kg</span></div>
              <p className="mt-1 text-xs text-slate-500">{workout.sets.map((set) => `${set.weight}kg×${set.reps}${set.rpe ? ` @${set.rpe}` : ""}`).join(" · ")}</p>
            </div>
          ))}
          {!selectedWorkouts.length && <div className="rounded-lg bg-slate-50 py-6 text-center text-xs text-slate-400">当天还没有训练记录</div>}
        </div>
      </section>
    </div>
  );
}
