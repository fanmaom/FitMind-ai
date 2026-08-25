"use client";

import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { BodyMetric, Workout } from "./MemoryPanel";

export function DataPanel({
  usage,
  metrics,
  workouts,
}: {
  usage: Record<string, number>;
  metrics: BodyMetric[];
  workouts: Workout[];
}) {
  return (
    <div className="space-y-5">
      <section className="grid grid-cols-3 gap-2 text-center text-xs">
        <div className="rounded bg-slate-50 p-2">
          <b className="block text-lg">{usage.total_input ?? 0}</b>输入 token
        </div>
        <div className="rounded bg-slate-50 p-2">
          <b className="block text-lg">{Math.round((usage.cache_hit_rate ?? 0) * 100)}%</b>缓存命中
        </div>
        <div className="rounded bg-slate-50 p-2">
          <b className="block text-lg">{Math.round((usage.degradation_rate ?? 0) * 100)}%</b>降级率
        </div>
      </section>

      <section>
        <div className="flex items-end justify-between">
          <b className="text-sm">体重趋势</b>
          <span className="text-xs text-slate-400">档案体重保存后自动同步</span>
        </div>
        <div className="mt-2 h-44 rounded bg-slate-50 p-2">
          {metrics.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={metrics}>
                <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                <YAxis domain={["dataMin - 2", "dataMax + 2"]} width={32} />
                <Tooltip />
                <Line type="monotone" dataKey="weight_kg" stroke="#2563eb" strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <Empty text="还没有体重记录" />
          )}
        </div>
      </section>

      <section>
        <b className="text-sm">最近训练</b>
        <div className="mt-2 space-y-2">
          {workouts.slice(0, 8).map((workout) => (
            <div key={workout.id} className="rounded bg-slate-50 p-3 text-sm">
              <b>{workout.exercise}</b>
              <span className="float-right text-xs text-slate-400">{workout.date}</span>
              <p className="mt-1 text-xs text-slate-500">
                {workout.sets.length} 组 · 总容量 {Math.round(workout.sets.reduce(
                  (sum, set) => sum + Number(set.weight) * Number(set.reps), 0,
                ))} kg
              </p>
            </div>
          ))}
          {!workouts.length && <Empty text="还没有训练记录" />}
        </div>
      </section>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="flex h-full min-h-24 items-center justify-center text-sm text-slate-400">{text}</div>;
}
