/**
 * Moment One 记账 MCP App（ui://moment-one/bookkeeping）
 *
 * - 通过 app-bridge 调用 MCP 工具（bookkeeping_summary / bookkeeping_list）
 * - 渲染 structuredContent；文本 content 由 Host 降级（本 App 不依赖）
 * - 不直接连库、不存 Token —— 所有读写走 Host Bridge
 */
import { App } from "@modelcontextprotocol/ext-apps";

const app = new App({ name: "Moment One 记账", version: "1.0.0" });

// ---------- DOM ----------
const root = document.getElementById("root")!;

interface SummaryResult {
  period?: string;
  year?: number;
  month?: number;
  income: number;
  expense: number;
  balance: number;
  count: number;
  byCategory?: { category: string; amount: number }[];
  from?: string;
  to?: string;
}

interface ListItem {
  id: string;
  title: string;
  amount: number;
  flow: "expense" | "income";
  category?: string;
  merchant?: string;
  occurredAt: string;
}

interface ListResult {
  total: number;
  items: ListItem[];
  nextCursor?: string | null;
  hasMore?: boolean;
}

const fmt = (n: number) => `¥${Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;

function renderSummary(s: SummaryResult | undefined): void {
  const cards = [
    { label: "支出", value: fmt(s?.expense ?? 0), tone: "expense" },
    { label: "收入", value: fmt(s?.income ?? 0), tone: "income" },
    { label: "结余", value: fmt(s?.balance ?? 0), tone: "balance" },
  ];
  const period = s?.period ? `${s.period}${s.year ? ` ${s.year}` : ""}${s.month ? `-${s.month}` : ""}` : "";
  const byCategory = (s?.byCategory ?? [])
    .slice(0, 8)
    .map(
      (c) => `<li class="cat-row"><span class="cat-name">${esc(c.category)}</span><span>${fmt(c.amount)}</span></li>`,
    )
    .join("");

  document.getElementById("summary")!.innerHTML = `
    <div class="period">${esc(period)}</div>
    <div class="cards">
      ${cards.map((c) => `<div class="card ${c.tone}"><div class="card-label">${c.label}</div><div class="card-value">${c.value}</div></div>`).join("")}
    </div>
    <div class="section-title">支出分类小计</div>
    ${byCategory ? `<ul class="cats">${byCategory}</ul>` : `<p class="empty">暂无支出分类</p>`}`;
}

function renderList(list: ListResult | undefined): void {
  const items = (list?.items ?? []);
  const rows = items
    .map((m) => {
      const amount = fmt(m.amount);
      const sign = m.flow === "income" ? "+" : "-";
      return `<li class="row">
        <div class="row-main">
          <div class="row-title">${esc(m.title)}</div>
          <div class="row-sub">${esc(m.category ?? "未分类")}${m.merchant ? ` · ${esc(m.merchant)}` : ""} · ${esc(formatDate(m.occurredAt))}</div>
        </div>
        <div class="row-amount ${m.flow}">${sign}${amount}</div>
      </li>`;
    })
    .join("");
  document.getElementById("list")!.innerHTML = `
    <div class="section-title">最近记录（${list?.total ?? items.length}）</div>
    ${rows ? `<ul class="rows">${rows}</ul>` : `<p class="empty">暂无记账记录</p>`}
    ${list?.hasMore ? `<p class="more">还有更多，请在 Host 中继续分页</p>` : ""}`;
}

function renderError(msg: string): void {
  document.getElementById("summary")!.innerHTML = `<p class="error">加载失败：${esc(msg)}</p>`;
}

function esc(s: string | undefined | null): string {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function contentText(content: unknown[] | undefined): string {
  return (content ?? [])
    .map((c) => {
      const block = c as { type?: string; text?: string };
      return block.type === "text" && typeof block.text === "string" ? block.text : "";
    })
    .filter(Boolean)
    .join("\n");
}

// ---------- 数据加载 ----------
let loading = false;

async function refresh(): Promise<void> {
  if (loading) return;
  loading = true;
  setBusy(true);
  try {
    const [sum, list] = await Promise.all([
      app.callServerTool({ name: "bookkeeping_summary", arguments: { period: "month" } }),
      app.callServerTool({ name: "bookkeeping_list", arguments: { limit: 20 } }),
    ]);
    if (sum.isError) {
      renderError(contentText(sum.content) || "summary 失败");
    } else {
      renderSummary(sum.structuredContent as SummaryResult | undefined);
    }
    if (list.isError) {
      renderError(contentText(list.content) || "list 失败");
    } else {
      renderList(list.structuredContent as ListResult | undefined);
    }
  } catch (e) {
    renderError(e instanceof Error ? e.message : String(e));
  } finally {
    loading = false;
    setBusy(false);
  }
}

function setBusy(busy: boolean): void {
  const btn = document.getElementById("refresh") as HTMLButtonElement | null;
  if (btn) btn.disabled = busy;
}

// Host 调用工具后把结果推给 App（初次渲染用），与 refresh() 幂等
app.ontoolresult = (params) => {
  if (params.isError) return;
  const sc = params.structuredContent as Record<string, unknown> | undefined;
  if (!sc || typeof sc !== "object") return;
  if ("income" in sc && "expense" in sc) {
    renderSummary(sc as unknown as SummaryResult);
  } else if ("items" in sc) {
    renderList(sc as unknown as ListResult);
  }
};

// ---------- 初始渲染 ----------
root.innerHTML = `
  <style>
    :root { color-scheme: light dark; }
    * { box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, sans-serif; margin: 0; background: transparent; }
    .app { padding: 16px; max-width: 560px; margin: 0 auto; }
    .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
    .header h1 { font-size: 16px; margin: 0; }
    .header button { font-size: 12px; padding: 4px 10px; border-radius: 8px; border: 1px solid color-mix(in srgb, currentColor 25%, transparent); background: transparent; cursor: pointer; }
    .header button:disabled { opacity: .5; cursor: default; }
    .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 12px; }
    .card { border-radius: 10px; padding: 10px; background: color-mix(in srgb, currentColor 6%, transparent); }
    .card-label { font-size: 11px; opacity: .7; }
    .card-value { font-size: 15px; font-weight: 600; margin-top: 2px; }
    .card.expense .card-value { color: #f97316; }
    .card.income .card-value { color: #10b981; }
    .section-title { font-size: 12px; font-weight: 600; margin: 10px 0 6px; }
    .period { font-size: 11px; opacity: .6; margin-bottom: 8px; }
    .cats, .rows { list-style: none; margin: 0; padding: 0; }
    .cat-row, .row { display: flex; justify-content: space-between; align-items: center; padding: 8px 4px; border-bottom: 1px solid color-mix(in srgb, currentColor 8%, transparent); }
    .cat-name { font-size: 13px; }
    .row-main { min-width: 0; }
    .row-title { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .row-sub { font-size: 11px; opacity: .6; margin-top: 2px; }
    .row-amount { font-size: 13px; font-weight: 600; }
    .row-amount.expense { color: #f97316; }
    .row-amount.income { color: #10b981; }
    .empty { font-size: 13px; opacity: .6; padding: 16px 0; text-align: center; }
    .more { font-size: 11px; opacity: .5; text-align: center; }
    .error { color: #f43f5e; font-size: 13px; }
  </style>
  <div class="app">
    <div class="header">
      <h1>📒 Moment One 记账</h1>
      <button id="refresh" type="button">刷新</button>
    </div>
    <div id="summary"><p class="empty">加载中…</p></div>
    <div id="list"></div>
  </div>
`;

document.getElementById("refresh")!.addEventListener("click", () => void refresh());

// 连接 Host（app-bridge PostMessage）。连接完成后主动拉一次数据。
app
  .connect()
  .then(() => refresh())
  .catch((e) => renderError(e instanceof Error ? e.message : String(e)));
