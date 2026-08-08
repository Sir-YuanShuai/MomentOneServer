import { App } from "@modelcontextprotocol/ext-apps";

const app = new App({ name: "Moment One 习惯卡片", version: "1.1.0" });
const root = document.getElementById("root")!;
type Day = { date: string; done: boolean };
type Goal = { id: string; name: string; frequency?: string | null; timesPerWeek?: number | null; unit?: string | null; todayDone?: boolean; currentStreak?: number; completedDays?: number; days?: Day[] };
type Result = { view?: string; goals?: Goal[]; goal?: Goal; checkin?: unknown; total?: number; from?: string; to?: string; created?: boolean; replayed?: boolean };

const esc = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
function dots(days: Day[] | undefined): string {
  return (days ?? []).slice(-7).map((day) => `<span class="dot ${day.done ? "done" : ""}" title="${esc(day.date)}"></span>`).join("");
}
function goalRow(goal: Goal): string {
  const target = goal.frequency === "weekly" ? `每周 ${goal.timesPerWeek ?? "-"} 次` : "每日";
  return `<li><div class="goal-line"><b>${esc(goal.name)}</b><span>${esc(target)}</span></div>${goal.days?.length ? `<div class="progress">${dots(goal.days)}<small>${goal.currentStreak ?? 0} 天连续</small></div>` : `<small>${esc(goal.unit ?? "持续记录")}</small>`}</li>`;
}
function render(data: Result | undefined): void {
  if (!data) return;
  if (data.goal && (data.created || data.checkin)) {
    const title = data.checkin ? "打卡已记录" : "习惯已创建";
    const note = data.replayed ? "已返回此前相同请求的结果" : data.checkin ? "今天的完成状态已更新" : "可从下一次行动开始记录";
    root.innerHTML = card(title, `<section class="success"><b>${esc(data.goal.name)}</b><span>${esc(data.goal.frequency === "weekly" ? `每周 ${data.goal.timesPerWeek ?? "-"} 次` : "每日")}</span></section>`, note);
    return;
  }
  const goals = (data.goals ?? []).slice(0, 3);
  root.innerHTML = card("习惯进度", goals.length ? `<ul>${goals.map(goalRow).join("")}</ul>` : '<div class="empty">还没有习惯目标</div>', `共 ${data.total ?? data.goals?.length ?? 0} 个目标${data.from && data.to ? ` · ${data.from} 至 ${data.to}` : ""}`);
}
function card(title: string, body: string, footer: string): string { return `<style>${styles}</style><article class="card"><header><span class="mark">H</span><h1>${esc(title)}</h1></header>${body}<footer>${esc(footer)}</footer></article>`; }
const styles = `
:root{color-scheme:light dark;--ink:var(--color-text-primary,light-dark(#18231c,#ecf4ee));--soft:var(--color-text-secondary,light-dark(#68736b,#9eaaa1));--line:var(--color-border,light-dark(#d6ddd7,#3b4740));--accent:var(--color-primary,#4f7b5a);--surface:var(--color-background,light-dark(#fbfcf9,#151d18))}*{box-sizing:border-box}body{margin:0;background:transparent;color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.card{width:min(448px,100%);max-height:352px;overflow:hidden;border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:12px}.card header{display:flex;align-items:center;gap:8px;padding-bottom:9px;border-bottom:1px solid var(--line)}.mark{display:grid;place-items:center;width:26px;height:26px;border:1px solid var(--accent);border-radius:7px;color:var(--accent);font:700 11px ui-monospace,monospace}.card h1{margin:0;font-size:15px}.card ul{list-style:none;margin:0;padding:0}.card li{padding:10px 0;border-bottom:1px solid color-mix(in srgb,var(--line) 65%,transparent)}.card li:last-child{border-bottom:0}.goal-line{display:flex;justify-content:space-between;gap:8px}.goal-line b{font-size:13px}.goal-line span,.card small{font-size:9px;color:var(--soft)}.progress{display:flex;align-items:center;gap:5px;margin-top:7px}.progress small{margin-left:auto}.dot{width:13px;height:13px;border:1px solid var(--line);border-radius:50%}.dot.done{background:var(--accent);border-color:var(--accent);box-shadow:inset 0 0 0 3px var(--surface)}.card footer{padding-top:8px;border-top:1px solid var(--line);font-size:9px;color:var(--soft)}.empty{padding:24px 8px;text-align:center;font-size:11px;color:var(--soft)}.success{display:flex;align-items:baseline;justify-content:space-between;padding:22px 4px}.success b{font-size:18px}.success span{font-size:10px;color:var(--soft)}
`;
root.innerHTML = card("习惯", '<div class="empty">等待工具结果</div>', "结果将显示在这里");
app.ontoolresult = (params) => { if (!params.isError) render(params.structuredContent as Result | undefined); };
app.connect().catch(() => undefined);
