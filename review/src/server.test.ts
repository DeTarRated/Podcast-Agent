// Fastify 审核 HTTP 与本地媒体测试；领域调用全部 fake，零真实数据库。
//
// 模拟场景：
// - health 开放，HTML/API/音频要求 Basic Auth
// - 完整 JSON Schema、Idempotency-Key、同源写保护
// - HTTP 字段到 Python CLI 的映射与 stale_revision 409
// - 本地音频完整响应、HEAD、单段 Range、后缀 Range、416 与路径越界
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { buildApp } from "./server.ts";
import { PythonCommandError, type PythonRunner } from "./python.ts";

const AUTH = { user: "owner", password: "test-password" };
const AUTHORIZATION = `Basic ${Buffer.from("owner:test-password").toString("base64")}`;

function authorized(headers: Record<string, string> = {}) {
  return { authorization: AUTHORIZATION, ...headers };
}

function runnerWith(handler: (command: string, payload: Record<string, unknown>) => unknown): PythonRunner {
  return async (command, payload) => handler(command, payload) as any;
}

test("health 开放，静态页和 API 需要鉴权", async () => {
  const app = buildApp({
    auth: AUTH,
    pythonRunner: runnerWith(() => ({ items: [], count: 0 })),
  });
  try {
    assert.equal((await app.inject({ method: "GET", url: "/api/health" })).statusCode, 200);
    const denied = await app.inject({ method: "GET", url: "/api/review/episodes" });
    assert.equal(denied.statusCode, 401);
    assert.ok(denied.headers["www-authenticate"]);
    const html = await app.inject({ method: "GET", url: "/", headers: authorized() });
    assert.equal(html.statusCode, 200);
    assert.ok(html.body.includes("播客审核箱"));
    assert.ok(html.body.includes("id=\"playback-dock\""));
    assert.ok(html.body.includes("id=\"playback-audio\""));
    const inbox = await app.inject({ method: "GET", url: "/api/review/episodes", headers: authorized() });
    assert.equal(inbox.statusCode, 200);
    assert.deepEqual(inbox.json(), { items: [], count: 0 });
  } finally {
    await app.close();
  }
});

test("写请求校验 Idempotency-Key、完整 body 与同源 Origin", async () => {
  let calls = 0;
  const app = buildApp({
    auth: AUTH,
    pythonRunner: runnerWith(() => {
      calls += 1;
      return { revision_id: 9 };
    }),
  });
  try {
    const baseBody = {
      baseRevisionId: 3,
      baseEntryNumber: 1,
      sourceArtifactId: 5,
      startMs: 1000,
      endMs: 3000,
      sourceText: "Hello",
      afterText: "人工",
    };
    const missingKey = await app.inject({
      method: "POST", url: "/api/episodes/7/overrides",
      headers: authorized({ "content-type": "application/json" }), payload: baseBody,
    });
    assert.equal(missingKey.statusCode, 400);
    const missingField = await app.inject({
      method: "POST", url: "/api/episodes/7/overrides",
      headers: authorized({ "idempotency-key": "save-1" }), payload: { baseRevisionId: 3 },
    });
    assert.equal(missingField.statusCode, 400);
    const crossSite = await app.inject({
      method: "POST", url: "/api/episodes/7/overrides",
      headers: authorized({ "idempotency-key": "save-1", origin: "https://evil.example" }),
      payload: baseBody,
    });
    assert.equal(crossSite.statusCode, 403);
    assert.equal(calls, 0);
  } finally {
    await app.close();
  }
});

test("override HTTP 字段映射到 Python CLI", async () => {
  let captured: Record<string, unknown> | undefined;
  const app = buildApp({
    auth: AUTH,
    pythonRunner: runnerWith((command, payload) => {
      assert.equal(command, "save-override");
      captured = payload;
      return { revision_id: 10 };
    }),
  });
  try {
    const response = await app.inject({
      method: "POST",
      url: "/api/episodes/7/overrides",
      headers: authorized({ "idempotency-key": "save-7" }),
      payload: {
        baseRevisionId: 3,
        baseEntryNumber: 2,
        sourceArtifactId: 5,
        startMs: 1000,
        endMs: 3000,
        sourceText: "Hello",
        afterText: "人工译文",
      },
    });
    assert.equal(response.statusCode, 200);
    assert.deepEqual(captured, {
      episode_id: 7,
      base_revision_id: 3,
      base_entry_number: 2,
      source_artifact_id: 5,
      start_ms: 1000,
      end_ms: 3000,
      source_text: "Hello",
      after_text: "人工译文",
      supersedes_override_id: null,
      retracted: false,
      request_id: "save-7",
    });
  } finally {
    await app.close();
  }
});

test("Python stale_revision 映射为 409", async () => {
  const app = buildApp({
    auth: AUTH,
    pythonRunner: runnerWith(() => {
      throw new PythonCommandError("stale_revision", "旧版本", { current: 11 });
    }),
  });
  try {
    const response = await app.inject({
      method: "POST",
      url: "/api/episodes/7/approve",
      headers: authorized({ "idempotency-key": "approve-7" }),
      payload: { baseRevisionId: 3 },
    });
    assert.equal(response.statusCode, 409);
    assert.equal(response.json().error.code, "stale_revision");
    assert.equal(response.json().error.details.current, 11);
  } finally {
    await app.close();
  }
});

test("用量报表支持筛选并拒绝非法 limit", async () => {
  let captured: Record<string, unknown> | undefined;
  const app = buildApp({
    auth: AUTH,
    pythonRunner: async (command, payload) => {
      captured = payload;
      return {
        summary: { call_count: 1, total_amount: 0.01 },
        filters: { episodes: [], purposes: [], providers: [], statuses: [] },
        items: [], truncated: false, r2_note: "R2 另算",
      } as any;
    },
  });
  try {
    const response = await app.inject({
      method: "GET", url: "/api/usage?purpose=highlights&status=unknown&limit=100", headers: authorized(),
    });
    assert.equal(response.statusCode, 200);
    assert.deepEqual(captured, {
      episode_id: undefined,
      purpose: "highlights",
      status: "unknown",
      provider: undefined,
      limit: 100,
    });
    const invalid = await app.inject({ method: "GET", url: "/api/usage?limit=501", headers: authorized() });
    assert.equal(invalid.statusCode, 400);
  } finally {
    await app.close();
  }
});

test("节目库 API 走 delivery.cli 并返回签名资源", async () => {
  const modules: string[] = [];
  const app = buildApp({
    auth: AUTH,
    pythonRunner: async (command, _payload, options) => {
      modules.push(options?.module ?? "review.cli");
      if (command === "library") return { items: [], count: 0 } as any;
      return {
        episode: { id: 1, title: "Published" },
        cues: [], highlights: { summary: "摘要", items: [] },
        audio_url: "signed://audio", subtitle_url: "signed://subtitle",
        expires_seconds: 900,
      } as any;
    },
  });
  try {
    const list = await app.inject({ method: "GET", url: "/api/library/episodes", headers: authorized() });
    assert.equal(list.statusCode, 200);
    const detail = await app.inject({ method: "GET", url: "/api/library/episodes/1", headers: authorized() });
    assert.equal(detail.statusCode, 200);
    assert.equal(detail.json().audio_url, "signed://audio");
    assert.deepEqual(modules, ["delivery.cli", "delivery.cli"]);
  } finally {
    await app.close();
  }
});

test("多用户登录、会话与退出流程", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: any, init: any) => {
    const url = String(input);
    calls.push(url);
    if (url.endsWith("/auth/session")) {
      return new Response(JSON.stringify({
        status: "success", data: { id: 42, email: "viewer@example.com", role: "viewer" },
      }), { status: 200 });
    }
    if (url.endsWith("/auth/login")) {
      return new Response(JSON.stringify({
        status: "success",
        data: { token: "token-123", user: { id: 42, email: "viewer@example.com", role: "viewer" } },
      }), { status: 200 });
    }
    return new Response(JSON.stringify({ status: "success", data: { revoked: true } }), { status: 200 });
  }) as typeof fetch;
  const app = buildApp({
    auth: AUTH,
    domainUrl: "http://domain",
    domainToken: "internal",
    multiUser: true,
    pythonRunner: runnerWith(() => ({ items: [], count: 0 })),
  });
  try {
    const loginPage = await app.inject({ method: "GET", url: "/login.html" });
    assert.equal(loginPage.statusCode, 200);
    const denied = await app.inject({ method: "GET", url: "/api/review/episodes" });
    assert.equal(denied.statusCode, 401);
    const loggedIn = await app.inject({
      method: "POST", url: "/login",
      payload: { email: "viewer@example.com", password: "viewer-password" },
    });
    assert.equal(loggedIn.statusCode, 200);
    assert.ok(loggedIn.headers["set-cookie"]?.toString().includes("HttpOnly"));
    const inbox = await app.inject({
      method: "GET", url: "/api/review/episodes",
      headers: { cookie: "podcast_session=token-123" },
    });
    assert.equal(inbox.statusCode, 200);
    const logout = await app.inject({
      method: "POST", url: "/logout",
      headers: { cookie: "podcast_session=token-123" },
    });
    assert.equal(logout.statusCode, 200);
    assert.ok(logout.headers["set-cookie"]?.toString().includes("Max-Age=0"));
    assert.ok(calls.some((url) => url.endsWith("/auth/login")));
    assert.ok(calls.some((url) => url.endsWith("/auth/session")));
  } finally {
    await app.close();
    globalThis.fetch = originalFetch;
  }
});

test("本地音频支持 HEAD、Range、后缀 Range 与非法 Range", async () => {
  const workspace = mkdtempSync(join(tmpdir(), "review-media-"));
  const audio = join(workspace, "audio.mp3");
  writeFileSync(audio, Buffer.from("0123456789"));
  const app = buildApp({
    auth: AUTH,
    pythonRunner: runnerWith((command) => {
      assert.equal(command, "media-info");
      return { path: audio, workspace, size: 10, sha256: "a".repeat(64), content_type: "audio/mpeg" };
    }),
  });
  try {
    const partial = await app.inject({
      method: "GET", url: "/api/episodes/1/audio",
      headers: authorized({ range: "bytes=2-5" }),
    });
    assert.equal(partial.statusCode, 206);
    assert.equal(partial.body, "2345");
    assert.equal(partial.headers["content-range"], "bytes 2-5/10");
    const suffix = await app.inject({
      method: "GET", url: "/api/episodes/1/audio",
      headers: authorized({ range: "bytes=-3" }),
    });
    assert.equal(suffix.statusCode, 206);
    assert.equal(suffix.body, "789");
    const head = await app.inject({ method: "HEAD", url: "/api/episodes/1/audio", headers: authorized() });
    assert.equal(head.statusCode, 200);
    assert.equal(head.headers["content-length"], "10");
    assert.equal(head.body, "");
    const invalid = await app.inject({
      method: "GET", url: "/api/episodes/1/audio",
      headers: authorized({ range: "bytes=99-" }),
    });
    assert.equal(invalid.statusCode, 416);
    const multiple = await app.inject({
      method: "GET", url: "/api/episodes/1/audio",
      headers: authorized({ range: "bytes=0-1,3-4" }),
    });
    assert.equal(multiple.statusCode, 416);
  } finally {
    await app.close();
  }
});

test("媒体 realpath 越界被拒绝", async () => {
  const root = mkdtempSync(join(tmpdir(), "review-outside-"));
  mkdirSync(join(root, "workspace"));
  const outside = join(root, "outside.mp3");
  writeFileSync(outside, "audio");
  const app = buildApp({
    auth: AUTH,
    pythonRunner: runnerWith(() => ({
      path: outside,
      workspace: join(root, "workspace"),
      size: 5,
      sha256: "a".repeat(64),
      content_type: "audio/mpeg",
    })),
  });
  try {
    const response = await app.inject({
      method: "GET", url: "/api/episodes/1/audio", headers: authorized(),
    });
    assert.equal(response.statusCode, 409);
    assert.equal(response.json().error.code, "artifact_mismatch");
  } finally {
    await app.close();
  }
});
