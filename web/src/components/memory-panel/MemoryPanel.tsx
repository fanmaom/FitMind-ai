"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ChartNoAxesCombined,
  ClipboardPlus,
  ListChecks,
  PanelRightClose,
  PanelRightOpen,
  UserRound,
} from "lucide-react";
import { api } from "@/services/api";
import { ActionPanel, type ActionItem } from "./ActionPanel";
import { DataPanel } from "./DataPanel";
import { ProfilePanel } from "./ProfilePanel";
import { TrainingPanel } from "./TrainingPanel";

type Tab = "profile" | "actions" | "data" | "training";

export type Workout = {
  id: string;
  date: string;
  exercise: string;
  sets: Array<{ weight: number; reps: number; rpe?: number | null }>;
};

export type BodyMetric = {
  id: string;
  date: string;
  weight_kg: number;
  body_fat_pct?: number | null;
};

export function MemoryPanel({ refreshKey }: { refreshKey: number }) {
  const [open, setOpen] = useState(true);
  const [tab, setTab] = useState<Tab>("profile");
  const [profile, setProfile] = useState<Record<string, unknown>>({});
  const [actions, setActions] = useState<ActionItem[]>([]);
  const [metrics, setMetrics] = useState<BodyMetric[]>([]);
  const [workouts, setWorkouts] = useState<Workout[]>([]);
  const [usage, setUsage] = useState<Record<string, number>>({});

  const load = useCallback(async () => {
    const [nextProfile, nextActions, nextMetrics, nextWorkouts, nextUsage] =
      await Promise.all([
        api<Record<string, unknown>>("/profile"),
        api<ActionItem[]>("/action-items"),
        api<BodyMetric[]>("/logs/body-metrics"),
        api<Workout[]>("/logs/workouts"),
        api<Record<string, number>>("/usage/summary"),
      ]);
    setProfile(nextProfile);
    setActions(nextActions);
    setMetrics(nextMetrics);
    setWorkouts(nextWorkouts);
    setUsage(nextUsage);
  }, []);

  useEffect(() => {
    load().catch(() => undefined);
  }, [load, refreshKey]);

  async function refreshActions() {
    setActions(await api<ActionItem[]>("/action-items"));
  }

  async function refreshMetrics() {
    setMetrics(await api<BodyMetric[]>("/logs/body-metrics"));
  }

  async function refreshWorkouts() {
    setWorkouts(await api<Workout[]>("/logs/workouts"));
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="border-l bg-white px-3 text-slate-500"
        title="打开数据面板"
      >
        <PanelRightOpen />
      </button>
    );
  }

  const tabs = [
    ["profile", UserRound, "档案"],
    ["actions", ListChecks, "待办"],
    ["data", ChartNoAxesCombined, "数据"],
    ["training", ClipboardPlus, "训练"],
  ] as const;

  return (
    <aside className="flex w-[400px] shrink-0 flex-col border-l border-slate-200 bg-white">
      <header className="flex items-center justify-between p-4">
        <div>
          <b>你的数据</b>
          <p className="text-xs text-slate-500">可编辑、可追溯、可删除</p>
        </div>
        <button onClick={() => setOpen(false)} className="text-slate-400">
          <PanelRightClose />
        </button>
      </header>

      <div className="grid grid-cols-4 border-y text-xs">
        {tabs.map(([key, Icon, text]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex items-center justify-center gap-1 py-3 ${
              tab === key
                ? "border-b-2 border-blue-600 text-blue-600"
                : "text-slate-500"
            }`}
          >
            <Icon size={14} />
            {text}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {tab === "profile" && (
          <ProfilePanel
            profile={profile}
            onSaved={async (nextProfile) => {
              setProfile(nextProfile);
              await refreshMetrics();
            }}
          />
        )}

        {tab === "actions" && (
          <ActionPanel items={actions} onChanged={refreshActions} />
        )}

        {tab === "data" && (
          <DataPanel usage={usage} metrics={metrics} workouts={workouts} />
        )}

        {tab === "training" && (
          <TrainingPanel workouts={workouts} onCreated={refreshWorkouts} />
        )}
      </div>
    </aside>
  );
}
