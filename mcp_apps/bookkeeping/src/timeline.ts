import { App } from "@modelcontextprotocol/ext-apps";

const app = new App({ name: "Moment One 记忆卡片", version: "1.1.0" });
const root = document.getElementById("root")!;

type MomentItem = {
  id: string;
  title: string;
  description?: string | null;
  category?: string;
  type?: string;
  tags?: string[];
  occurredAt?: string;
};
type Result = {
  id?: string;
  view?: string;
  query?: string;
  date?: string;
  count?: number;
  total?: number;
  hasMore?: boolean;
  prompt?: string;
  items?: MomentItem[];
  highlights?: MomentItem[];
  moment?: MomentItem;
  title?: string;
  description?: string | null;
  category?: string;
  type?: string;
  tags?: string[];
  occurredAt?: string;
  created?: boolean;
  replayed?: boolean;
};

const categoryLabel: Record<string, string> = {
  experience: "经历",
  habit: "习惯",
  travel: "旅行",
  food: "饮食",
  growth: "成长",
  emotion: "情绪",
};

function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (char) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!,
  );
}
function formatTime(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
function row(item: MomentItem): string {
  const label = categoryLabel[item.category ?? ""] ?? item.type ?? "Moment";
  return `<li><span class="rail"></span><div class="copy"><div class="line"><b>${esc(item.title)}</b><time>${esc(formatTime(item.occurredAt))}</time></div>${item.description ? `<p>${esc(item.description)}</p>` : ""}<small>${esc(label)}${item.tags?.length ? ` · ${item.tags.slice(0, 2).map((tag) => `#${esc(tag)}`).join(" ")}` : ""}</small></div></li>`;
}
function render(data: Result | undefined): void {
  if (!data) return;
  const detail = data.moment ?? (data.id ? data as MomentItem : undefined);
  if (detail) {
    const title = data.created ? "已记录" : "Moment";
    root.innerHTML = card(title, `<section class="detail"><div class="detail-head"><span>${esc(categoryLabel[detail.category ?? ""] ?? detail.type ?? "记录")}</span><time>${esc(formatTime(detail.occurredAt))}</time></div><h2>${esc(detail.title)}</h2>${detail.description ? `<p>${esc(detail.description)}</p>` : ""}<div class="tags">${(detail.tags ?? []).slice(0, 3).map((tag) => `<span>#${esc(tag)}</span>`).join("")}</div></section>`, data.replayed ? "已返回此前相同请求的结果" : "已同步到你的记忆库");
    return;
  }

  const isReview = data.view === "daily-review" || Boolean(data.highlights);
  const items = (isReview ? data.highlights : data.items) ?? [];
  const visible = items.slice(0, 3);
  const title = isReview ? `${data.date ?? "今日"}回顾` : data.query ? `“${data.query}”的结果` : "最近的 Moment";
  const count = data.count ?? data.total ?? items.length;
  const body = visible.length ? `<ul>${visible.map(row).join("")}</ul>` : `<div class="empty">没有找到可展示的 Moment</div>`;
  const footer = isReview ? data.prompt ?? `今天共 ${count} 条记录` : `共 ${count} 条${data.hasMore ? "，仅展示前 3 条" : ""}`;
  root.innerHTML = card(title, body, footer);
}
function card(title: string, body: string, footer: string): string {
  return `<style>${styles}</style><article class="card"><header><span class="mark">M1</span><h1>${esc(title)}</h1></header>${body}<footer>${esc(footer)}</footer></article>`;
}

const styles = `
:root{color-scheme:light dark;--ink:var(--color-text-primary,light-dark(#18231c,#ecf4ee));--soft:var(--color-text-secondary,light-dark(#68736b,#9eaaa1));--line:var(--color-border,light-dark(#d6ddd7,#3b4740));--accent:var(--color-primary,#4f7b5a);--surface:var(--color-background,light-dark(#fbfcf9,#151d18))}*{box-sizing:border-box}body{margin:0;background:transparent;color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.card{width:min(448px,100%);max-height:352px;overflow:hidden;border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:12px}.card header{display:flex;align-items:center;gap:8px;padding-bottom:9px;border-bottom:1px solid var(--line)}.mark{display:grid;place-items:center;width:26px;height:26px;border:1px solid var(--accent);border-radius:7px;color:var(--accent);font:700 9px ui-monospace,monospace}.card h1{margin:0;font-size:15px;line-height:1.2}.card ul{list-style:none;margin:0;padding:0}.card li{display:grid;grid-template-columns:8px 1fr;gap:7px;padding:9px 0;border-bottom:1px solid color-mix(in srgb,var(--line) 65%,transparent)}.card li:last-child{border-bottom:0}.rail{width:3px;height:100%;min-height:31px;border-radius:4px;background:color-mix(in srgb,var(--accent) 72%,transparent)}.copy{min-width:0}.line{display:flex;align-items:baseline;justify-content:space-between;gap:8px}.line b{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.line time,.copy small{font-size:9px;color:var(--soft);white-space:nowrap}.copy p{margin:2px 0 3px;font-size:10px;line-height:1.35;color:var(--soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.card footer{padding-top:8px;border-top:1px solid var(--line);font-size:9px;color:var(--soft)}.empty{padding:24px 8px;text-align:center;font-size:11px;color:var(--soft)}.detail{padding:10px 0 6px}.detail-head{display:flex;justify-content:space-between;color:var(--soft);font-size:9px}.detail h2{margin:7px 0 4px;font-size:17px}.detail p{margin:0;max-height:55px;overflow:hidden;font-size:11px;line-height:1.5;color:var(--soft)}.tags{display:flex;gap:5px;margin-top:8px}.tags span{font-size:9px;color:var(--accent)}
`;

root.innerHTML = card("Moment One", '<div class="empty">等待工具结果</div>', "结果将显示在这里");
app.ontoolresult = (params) => { if (!params.isError) render(params.structuredContent as Result | undefined); };
app.connect().catch(() => undefined);
