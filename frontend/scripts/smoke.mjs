#!/usr/bin/env node
/**
 * 前端数据路径冒烟：经 Vite 代理走浏览器同源链路（管理面 + SDK 数据面）。
 *
 * 前置：make admin + make runtime + pnpm dev 均已启动。
 * 用法：node scripts/smoke.mjs [proxyBase]    # 默认 http://localhost:5173
 *
 * 覆盖：建租户 → 建用户（租户内注册表）→ 签发租户级/用户级 token →
 * /me 合成身份（tenant-{tid}[-{user}]）→ SDK 建线程 → runs.stream 流式对话
 * （真实 LLM）→ 模型列表 + context.model_name 指定模型运行 + 未知模型 400 →
 * 交付物（写 outputs → present_files → state.artifacts → Bearer 认证下载）→
 * state 持久化 → 三重隔离检查（跨租户 / 同租户跨用户 /
 * 用户 vs 租户级）→ 清理。
 */

import { Client } from "@langchain/langgraph-sdk/client";

const PROXY = process.argv[2] ?? "http://localhost:5173";
const tid = `fe-smoke-${Math.random().toString(36).slice(2, 8)}`;
const tidB = `${tid}-b`;

function fail(step, msg) {
  console.error(`✗ ${step}: ${msg}`);
  process.exit(1);
}

async function adminJson(path, init) {
  const res = await fetch(PROXY + path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}: ${(await res.text()).slice(0, 200)}`);
  return res.json();
}

function extractText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((b) => (typeof b === "string" ? b : b?.type === "text" ? (b.text ?? "") : ""))
      .join("");
  }
  return "";
}

function clientFor(token, tenant) {
  return new Client({
    apiUrl: `${PROXY}/api`,
    defaultHeaders: { Authorization: `Bearer ${token}`, "x-tenant-id": tenant },
  });
}

async function meOf(token, tenant) {
  const res = await fetch(`${PROXY}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}`, "x-tenant-id": tenant },
  });
  if (!res.ok) throw new Error(`/me -> ${res.status}`);
  return res.json();
}

// ── 1. 租户 + 用户（管理面）───────────────────────────────────────────────
await adminJson("/admin/tenants", {
  method: "POST",
  body: JSON.stringify({ id: tid, name: "FE Smoke A" }),
}).catch((e) => fail("建租户 A", e.message));
console.log(`✓ 租户创建: ${tid}`);

for (const name of ["alice", "bob"]) {
  await adminJson(`/admin/tenants/${tid}/users`, {
    method: "POST",
    body: JSON.stringify({ username: name, display_name: name }),
  }).catch((e) => fail(`建用户 ${name}`, e.message));
}
console.log("✓ 用户创建: alice, bob");

await adminJson("/admin/tenants", {
  method: "POST",
  body: JSON.stringify({ id: tidB, name: "FE Smoke B" }),
}).catch((e) => fail("建租户 B", e.message));
console.log(`✓ 租户创建: ${tidB}`);

// 未注册用户不可签发
const mallory = await adminJson(`/admin/tenants/${tid}/token`, {
  method: "POST",
  body: JSON.stringify({ user: "mallory" }),
}).then(
  () => fail("未注册用户", "mallory 不应能拿到 token"),
  (e) => e.message,
);
console.log(`✓ 未注册用户被拒: ${mallory.slice(0, 60)}`);

// ── 2. 三种身份的 token + /me 合成身份 ────────────────────────────────────
async function tokenFor(tenant, user) {
  const { access_token } = await adminJson(`/admin/tenants/${tenant}/token`, {
    method: "POST",
    body: JSON.stringify(user ? { user } : {}),
  }).catch((e) => fail(`签发 token ${tenant}${user ? "/" + user : ""}`, e.message));
  return access_token;
}

const tTenant = await tokenFor(tid, null);
const tAlice = await tokenFor(tid, "alice");
const tBob = await tokenFor(tid, "bob");
const tB = await tokenFor(tidB, null);

const meTenant = await meOf(tTenant, tid).catch((e) => fail("/me 租户级", e.message));
if (meTenant.id !== `tenant-${tid}`) fail("/me 租户级", `身份不符: ${meTenant.id}`);
console.log(`✓ /me 租户级 = ${meTenant.id}`);

const meAlice = await meOf(tAlice, tid).catch((e) => fail("/me alice", e.message));
if (meAlice.id !== `tenant-${tid}-alice` || meAlice.email !== `alice@${tid}.tenant.local`) {
  fail("/me alice", `身份不符: ${JSON.stringify(meAlice).slice(0, 200)}`);
}
console.log(`✓ /me 用户级 = ${meAlice.id} (${meAlice.email})`);

// ── 3. SDK 数据面：alice 建线程 + 真实流式对话 ────────────────────────────
const alice = clientFor(tAlice, tid);
const thread = await alice.threads.create().catch((e) => fail("建线程", e.message));
console.log(`✓ 线程创建（alice）: ${thread.thread_id}`);

let eventCount = 0;
let assistantText = "";
try {
  const stream = alice.runs.stream(thread.thread_id, "lead_agent", {
    input: {
      messages: [
        { type: "human", content: [{ type: "text", text: "用一句话说明你是什么系统。" }] },
      ],
    },
    config: { recursion_limit: 1000 },
    streamMode: ["messages-tuple"],
  });
  for await (const ev of stream) {
    eventCount += 1;
    if (ev.event === "messages") {
      const [chunk] = ev.data;
      // 流式增量的 type 是 "AIMessageChunk"（state 里的完整消息才是 "ai"）
      if (chunk?.type === "ai" || chunk?.type === "AIMessageChunk") {
        assistantText += extractText(chunk.content);
      }
    } else if (ev.event === "error") {
      fail("runs.stream", JSON.stringify(ev.data).slice(0, 300));
    }
  }
} catch (err) {
  fail("runs.stream", err?.message ?? String(err));
}
const flat = assistantText.replace(/\s+/g, " ").trim();
if (!flat) fail("runs.stream", "未收到任何 AI 文本增量");
console.log(`✓ 流式完成: ${eventCount} 个事件, AI 回复: ${flat.slice(0, 100)}…`);

// ── 4. 模型选择（GET /api/models + context.model_name）────────────────────
const modelsRes = await fetch(`${PROXY}/api/models`, {
  headers: { Authorization: `Bearer ${tAlice}`, "x-tenant-id": tid },
});
if (!modelsRes.ok) fail("模型列表", `/api/models -> ${modelsRes.status}`);
const { models: modelList } = await modelsRes.json();
if (!Array.isArray(modelList) || modelList.length === 0 || !modelList[0]?.name) {
  fail("模型列表", `返回为空或缺少 name: ${JSON.stringify(modelList).slice(0, 120)}`);
}
console.log(`✓ 模型列表: ${modelList.map((m) => m.name).join(", ")}`);

// 显式指定模型走全链路：context.model_name → 网关允许列表校验 → agent 解析
const mThread = await alice.threads.create().catch((e) => fail("建线程(模型)", e.message));
let mText = "";
try {
  const stream = alice.runs.stream(mThread.thread_id, "lead_agent", {
    input: {
      messages: [{ type: "human", content: [{ type: "text", text: "回复一个字：好" }] }],
    },
    config: { recursion_limit: 1000 },
    context: { model_name: modelList[0].name },
    streamMode: ["messages-tuple"],
  });
  for await (const ev of stream) {
    if (ev.event === "messages") {
      const [chunk] = ev.data;
      if (chunk?.type === "ai" || chunk?.type === "AIMessageChunk") mText += extractText(chunk.content);
    } else if (ev.event === "error") {
      fail("模型指定运行", JSON.stringify(ev.data).slice(0, 300));
    }
  }
} catch (err) {
  fail("模型指定运行", err?.message ?? String(err));
}
if (!mText.trim()) fail("模型指定运行", "未收到 AI 文本增量");
console.log(`✓ 显式模型运行 (${modelList[0].name}): ${mText.replace(/\s+/g, " ").trim().slice(0, 60)}`);

// 未知模型名 → 400（config.yaml models: 允许列表拒绝，发生在建 run 之前）
const badModel = await fetch(`${PROXY}/api/threads/${mThread.thread_id}/runs/stream`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    Authorization: `Bearer ${tAlice}`,
    "x-tenant-id": tid,
  },
  body: JSON.stringify({
    input: { messages: [{ type: "human", content: "hi" }] },
    context: { model_name: "not-a-real-model" },
    stream_mode: ["messages-tuple"],
  }),
}).then(
  (r) => r.status,
  () => -1,
);
if (badModel !== 400) fail("未知模型拒绝", `期望 400, 实际 ${badModel}`);
console.log("✓ 未知模型名被拒 (400)");

// ── 5. 交付物（写 outputs → present_files → state.artifacts → 认证下载）───
// 复刻控制台交付物栏的完整链路：普通用户让 agent 产出并交付一个文件，
// 断言 state.values.artifacts 出现虚拟路径，再带 Bearer 头下载成功。
const artThread = await alice.threads.create().catch((e) => fail("建线程(交付物)", e.message));
try {
  const stream = alice.runs.stream(artThread.thread_id, "lead_agent", {
    input: {
      messages: [
        {
          type: "human",
          content: [
            {
              type: "text",
              text: "请用 write_file 工具创建 /mnt/user-data/outputs/fe-smoke-deliverable.txt，内容只有一行 ok，然后调用 present_files 工具交付这个文件。除此之外不要做任何别的事情。",
            },
          ],
        },
      ],
    },
    config: { recursion_limit: 1000 },
    streamMode: ["messages-tuple"],
  });
  for await (const ev of stream) {
    if (ev.event === "error") fail("交付物运行", JSON.stringify(ev.data).slice(0, 300));
  }
} catch (err) {
  fail("交付物运行", err?.message ?? String(err));
}

const artState = await alice.threads
  .getState(artThread.thread_id)
  .catch((e) => fail("交付物 state", e.message));
const artifactPaths = artState?.values?.artifacts ?? [];
const deliverable = artifactPaths.find((p) => String(p).endsWith("/fe-smoke-deliverable.txt"));
if (!deliverable) {
  fail("交付物 state", `artifacts 不含交付文件: ${JSON.stringify(artifactPaths)}`);
}
console.log(`✓ 交付物进入 state.artifacts: ${deliverable}`);

// 下载走与 UI 相同的编码（逐段 encodeURIComponent、保留 /）与认证头
const encodedDeliverable = deliverable.split("/").map(encodeURIComponent).join("/");
const dlRes = await fetch(
  `${PROXY}/api/threads/${artThread.thread_id}/artifacts${encodedDeliverable}?download=true`,
  { headers: { Authorization: `Bearer ${tAlice}`, "x-tenant-id": tid } },
);
if (!dlRes.ok) fail("交付物下载", `HTTP ${dlRes.status}`);
const dlText = await dlRes.text();
if (!dlText.includes("ok")) fail("交付物下载", `内容不符: ${JSON.stringify(dlText.slice(0, 80))}`);
console.log(`✓ 交付物认证下载成功 (${dlRes.headers.get("content-type")}, ${dlText.length} 字节)`);

// ── 6. 权威 state 持久化 ──────────────────────────────────────────────────
const state = await alice.threads.getState(thread.thread_id).catch((e) => fail("getState", e.message));
const msgs = state?.values?.messages ?? [];
const roles = msgs.map((m) => m.type).join(",");
if (!msgs.some((m) => m.type === "human") || !msgs.some((m) => m.type === "ai")) {
  fail("state 持久化", `消息角色不完整: ${roles}`);
}
console.log(`✓ state 持久化: ${msgs.length} 条消息 (${roles})`);

// ── 7. 三重隔离（owner 轴）───────────────────────────────────────────────
const seenBob = await clientFor(tBob, tid).threads.search({ limit: 50 }).catch((e) => fail("bob 线程列表", e.message));
if (seenBob.some((t) => t.thread_id === thread.thread_id)) {
  fail("同租户跨用户隔离", "bob 看到了 alice 的线程");
}
console.log(`✓ 同租户跨用户隔离: bob（${seenBob.length} 条）看不到 alice 的线程`);

const seenTenant = await clientFor(tTenant, tid).threads.search({ limit: 50 }).catch((e) => fail("租户级线程列表", e.message));
if (seenTenant.some((t) => t.thread_id === thread.thread_id)) {
  fail("用户 vs 租户级隔离", "租户级身份看到了 alice 的线程");
}
console.log(`✓ 用户 vs 租户级隔离: 租户级（${seenTenant.length} 条）看不到 alice 的线程`);

const seenB = await clientFor(tB, tidB).threads.search({ limit: 50 }).catch((e) => fail("B 租户线程列表", e.message));
if (seenB.some((t) => t.thread_id === thread.thread_id)) {
  fail("跨租户隔离", "B 租户看到了 A 的线程");
}
console.log(`✓ 跨租户隔离: B 租户（${seenB.length} 条）不含 A 的线程`);

// ── 8. 清理 ──────────────────────────────────────────────────────────────
for (const t of [tid, tidB]) {
  await adminJson(`/admin/tenants/${t}`, { method: "DELETE" }).catch((e) => fail(`清理 ${t}`, e.message));
}
console.log("✓ 清理完成");

console.log("\nALL CHECKS PASSED");
