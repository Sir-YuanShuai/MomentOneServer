import { App } from "@modelcontextprotocol/ext-apps";

const app = new App({ name: "Moment One 记账卡片", version: "1.1.0" });
const root = document.getElementById("root")!;
type Item = { title: string; amount: number; flow: "expense" | "income"; category?: string; merchant?: string; occurredAt?: string };
type Result = { income?: number; expense?: number; balance?: number; count?: number; byCategory?: { category: string; amount: number }[]; items?: Item[]; total?: number; amount?: number; flow?: "expense" | "income"; title?: string; category?: string; merchant?: string; occurredAt?: string; replayed?: boolean };
const esc = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
const money = (value: number | undefined) => `¥${Number(value ?? 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
function render(data: Result | undefined): void {
  if (!data) return;
  if (typeof data.amount === "number" && data.flow) {
    const sign = data.flow === "income" ? "+" : "−";
    root.innerHTML = card("已记一笔", `<section class="created"><span>${esc(data.category ?? data.merchant ?? "记账")}</span><b>${sign}${money(data.amount)}</b></section>`, data.replayed ? "已返回此前相同请求的结果" : "账目已同步");
    return;
  }
  if (typeof data.income === "number" || typeof data.expense === "number") {
    const categories = (data.byCategory ?? []).slice(0, 3).map((item) => `<li><span>${esc(item.category)}</span><b>${money(item.amount)}</b></li>`).join("");
    root.innerHTML = card("收支概览", `<div class="metrics"><div><span>支出</span><b>${money(data.expense)}</b></div><div><span>收入</span><b>${money(data.income)}</b></div><div><span>结余</span><b>${money(data.balance)}</b></div></div>${categories ? `<ul class="categories">${categories}</ul>` : ""}`, `共 ${data.count ?? 0} 笔`);
    return;
  }
  const items = (data.items ?? []).slice(0, 3);
  root.innerHTML = card("账目明细", items.length ? `<ul class="rows">${items.map((item) => `<li><div><b>${esc(item.title)}</b><small>${esc(item.category ?? "未分类")}${item.merchant ? ` · ${esc(item.merchant)}` : ""}</small></div><strong class="${item.flow}">${item.flow === "income" ? "+" : "−"}${money(item.amount)}</strong></li>`).join("")}</ul>` : '<div class="empty">没有账目记录</div>', `共 ${data.total ?? items.length} 笔${(data.total ?? 0) > 3 ? "，仅展示前 3 笔" : ""}`);
}
function card(title: string, body: string, footer: string): string { return `<style>${styles}</style><article class="card"><header><span class="mark">¥</span><h1>${esc(title)}</h1></header>${body}<footer>${esc(footer)}</footer></article>`; }
const styles = `
:root{color-scheme:light dark;--ink:var(--color-text-primary,light-dark(#18231c,#ecf4ee));--soft:var(--color-text-secondary,light-dark(#68736b,#9eaaa1));--line:var(--color-border,light-dark(#d6ddd7,#3b4740));--accent:var(--color-primary,#4f7b5a);--surface:var(--color-background,light-dark(#fbfcf9,#151d18))}*{box-sizing:border-box}body{margin:0;background:transparent;color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.card{width:min(448px,100%);max-height:352px;overflow:hidden;border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:12px}.card header{display:flex;align-items:center;gap:8px;padding-bottom:9px;border-bottom:1px solid var(--line)}.mark{display:grid;place-items:center;width:26px;height:26px;border:1px solid var(--accent);border-radius:7px;color:var(--accent);font-weight:700}.card h1{margin:0;font-size:15px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;padding:10px 0}.metrics div{padding:8px;border:1px solid color-mix(in srgb,var(--line) 70%,transparent);border-radius:8px}.metrics span{display:block;font-size:9px;color:var(--soft)}.metrics b{display:block;margin-top:3px;font-size:12px}.card ul{list-style:none;margin:0;padding:0}.categories li,.rows li{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid color-mix(in srgb,var(--line) 60%,transparent);font-size:10px}.rows li>div{min-width:0}.rows b{display:block;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rows small{display:block;margin-top:2px;color:var(--soft)}.rows strong{font-size:12px;white-space:nowrap}.income{color:var(--accent)}.card footer{padding-top:8px;border-top:1px solid var(--line);font-size:9px;color:var(--soft)}.created{display:flex;align-items:baseline;justify-content:space-between;padding:22px 4px}.created span{font-size:12px;color:var(--soft)}.created b{font-size:20px}.empty{padding:24px 8px;text-align:center;font-size:11px;color:var(--soft)}
`;
root.innerHTML = card("记账", '<div class="empty">等待工具结果</div>', "结果将显示在这里");
app.ontoolresult = (params) => { if (!params.isError) render(params.structuredContent as Result | undefined); };
app.connect().catch(() => undefined);
