"use client";

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import type { Card } from "@/types/chat";

const shell = "mt-3 overflow-hidden rounded-lg border border-slate-200 bg-white p-4 shadow-sm transition-all duration-200 hover:shadow-md";
const n = (value: unknown) => typeof value === "number" ? value : Number(value ?? 0);

function WorkoutCard({ p }: { p: any }) {
  return <div className={shell}><h3 className="font-semibold">训练已记录 · {p.exercise}</h3><p className="mt-1 text-sm text-slate-500">{p.date} · 总容量 {n(p.total_volume_kg).toFixed(0)} kg</p><div className="mt-3 flex flex-wrap gap-2">{(p.sets ?? []).map((s: any, i: number) => <span key={i} className="rounded bg-slate-100 px-2 py-1 text-sm">{s.weight}kg × {s.reps}</span>)}</div></div>;
}
function MetricCard({ p }: { p: any }) {
  return <div className={shell}><h3 className="font-semibold">身体数据已记录</h3><div className="mt-3 flex gap-8"><div><b className="text-2xl text-blue-600">{p.weight_kg}</b><span className="text-sm"> kg</span></div>{p.body_fat_pct && <div><b className="text-2xl">{p.body_fat_pct}</b><span className="text-sm">% 体脂</span></div>}</div></div>;
}
function MacrosCard({ p }: { p: any }) {
  const data = [{ name: "蛋白", value: n(p.protein_g), color: "#2563eb" }, { name: "碳水", value: n(p.carb_g), color: "#60a5fa" }, { name: "脂肪", value: n(p.fat_g), color: "#f59e0b" }];
  return <div className={shell}><h3 className="font-semibold">每日营养目标 · {n(p.kcal).toFixed(0)} kcal</h3><div className="flex h-44 items-center"><ResponsiveContainer width="45%" height="100%"><PieChart><Pie data={data} dataKey="value" innerRadius={38} outerRadius={62}>{data.map(x => <Cell key={x.name} fill={x.color} />)}</Pie><Tooltip /></PieChart></ResponsiveContainer><div className="space-y-2 text-sm">{data.map(x => <div key={x.name}><span className="mr-2 inline-block h-2 w-2 rounded-full" style={{ background: x.color }} />{x.name} {x.value}g</div>)}</div></div></div>;
}
function ProjectionCard({ p }: { p: any }) {
  return <div className={shell}><h3 className="font-semibold">目标进度推算</h3><div className="mt-3 grid grid-cols-2 gap-3"><Stat label="预计时间" value={`${p.days ?? "—"} 天`} /><Stat label="周变化" value={`${p.weekly_change_kg ?? "—"} kg`} /></div>{p.note && <p className="mt-3 rounded bg-amber-50 p-2 text-sm text-amber-800">{p.note}</p>}</div>;
}
function StrengthPlanCard({ p }: { p: any }) {
  const weeks = p.weeks ?? [];
  const names = Object.keys(weeks[0]?.lifts ?? {});
  return <div className={shell}><h3 className="font-semibold">增力周期 · {p.scheme}</h3><div className="mt-3 overflow-x-auto"><table className="min-w-full text-sm"><thead><tr><th className="p-2 text-left">周</th>{names.map(x => <th className="p-2" key={x}>{x}</th>)}</tr></thead><tbody>{weeks.map((w: any) => <tr key={w.week} className={w.is_deload ? "bg-blue-50" : "border-t"}><td className="p-2">{w.week}{w.is_deload ? " 卸载" : ""}</td>{names.map(x => <td className="p-2 text-center" key={x}>{w.lifts[x].weight_kg}kg<br/><span className="text-xs text-slate-400">{w.lifts[x].sets}×{w.lifts[x].reps}</span></td>)}</tr>)}</tbody></table></div></div>;
}
function CutPlanCard({ p }: { p: any }) {
  const target = p.daily_target ?? {};
  return <div className={shell}><h3 className="font-semibold">减脂周期 · {p.weeks?.length ?? 0} 周</h3><p className="mt-1 text-sm text-slate-500">每日 {target.kcal} kcal · P {target.protein_g}g / C {target.carb_g}g / F {target.fat_g}g</p>{p.carb_cycle && <div className="mt-3 flex gap-2 text-xs"><Badge c="bg-blue-100 text-blue-700">高碳</Badge><Badge c="bg-slate-100">中碳</Badge><Badge c="bg-amber-100 text-amber-700">低碳</Badge></div>}</div>;
}
function MealPlanCard({ p }: { p: any }) {
  return <div className={shell}><h3 className="font-semibold">场景配餐 · {p.scenario}</h3><div className="mt-3 space-y-3">{(p.meals ?? []).map((meal: any) => <div className="rounded bg-slate-50 p-3" key={meal.meal}><b>第 {meal.meal} 餐 · {meal.target?.kcal} kcal</b><div className="mt-1 text-sm">{(meal.items ?? []).map((x: any) => `${x.food_name} ${x.grams}g`).join(" + ")}</div><p className="mt-1 text-xs text-slate-500">{meal.preparation_note}</p></div>)}</div></div>;
}
function ConflictCard({ p }: { p: any }) {
  return <div className={`${shell} border-amber-200`}><h3 className="font-semibold text-amber-800">目标存在冲突</h3><p className="mt-2 text-sm">{p.reason}</p><div className="mt-3 grid gap-2">{(p.options ?? []).map((o: any) => <button key={o.key} onClick={() => window.dispatchEvent(new CustomEvent("fitness-option", { detail: o.title }))} className="rounded border border-slate-200 p-3 text-left hover:border-blue-400"><b>{o.title}</b><p className="mt-1 text-xs text-slate-500">{o.summary}</p></button>)}</div></div>;
}
function Stat({ label, value }: { label: string; value: string }) { return <div className="rounded bg-slate-50 p-3"><div className="text-xs text-slate-500">{label}</div><b className="text-xl">{value}</b></div>; }
function Badge({ c, children }: { c: string; children: React.ReactNode }) { return <span className={`rounded px-2 py-1 ${c}`}>{children}</span>; }

export function CardRenderer({ card }: { card: Card }) {
  const props = { p: card.payload };
  const map: Record<string, React.ComponentType<{ p: any }>> = { workout_logged: WorkoutCard, metric_logged: MetricCard, macros: MacrosCard, projection: ProjectionCard, strength_plan: StrengthPlanCard, cut_plan: CutPlanCard, meal_plan: MealPlanCard, conflict: ConflictCard };
  const Component = map[card.type];
  if (!Component) return <details className={shell}><summary>未知卡片：{card.type}</summary><pre className="mt-2 overflow-auto text-xs">{JSON.stringify(card.payload, null, 2)}</pre></details>;
  return <Component {...props} />;
}
