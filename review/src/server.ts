// 审核层·Fastify HTTP 入口；完整 schema、鉴权、静态审核页与 Python CLI 路由。
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import Fastify, { type FastifyInstance, type FastifyRequest } from "fastify";

import { createAuthHook, loginWithDomain, type AuthConfig } from "./auth.ts";
import { sendLocalAudio, type MediaInfo } from "./media.ts";
import { PythonCommandError, runPython, type PythonRunner } from "./python.ts";

const PUBLIC_DIR = resolve(import.meta.dirname, "../public");

const episodeParams = {
  type: "object",
  required: ["episodeId"],
  additionalProperties: false,
  properties: { episodeId: { type: "integer", minimum: 1 } },
} as const;

const writeHeaders = {
  type: "object",
  required: ["idempotency-key"],
  properties: { "idempotency-key": { type: "string", minLength: 1, maxLength: 200 } },
} as const;

const revisionProperties = { baseRevisionId: { type: "integer", minimum: 1 } } as const;
const revisionBody = {
  type: "object",
  required: ["baseRevisionId"],
  additionalProperties: false,
  properties: revisionProperties,
} as const;

const overrideBody = {
  type: "object",
  required: [
    "baseRevisionId",
    "baseEntryNumber",
    "sourceArtifactId",
    "startMs",
    "endMs",
    "sourceText",
    "afterText",
  ],
  additionalProperties: false,
  properties: {
    ...revisionProperties,
    baseEntryNumber: { type: "integer", minimum: 1 },
    sourceArtifactId: { type: "integer", minimum: 1 },
    startMs: { type: "integer", minimum: 0 },
    endMs: { type: "integer", minimum: 1 },
    sourceText: { type: "string" },
    afterText: { type: "string", maxLength: 5000 },
    supersedesOverrideId: { anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }] },
    retracted: { type: "boolean" },
  },
} as const;

const variantBody = {
  type: "object",
  required: ["baseRevisionId", "variant"],
  additionalProperties: false,
  properties: { ...revisionProperties, variant: { type: "string", enum: ["review", "split"] } },
} as const;

const rejectBody = {
  type: "object",
  required: ["baseRevisionId", "reason", "ranges"],
  additionalProperties: false,
  properties: {
    ...revisionProperties,
    reason: { type: "string", minLength: 1, maxLength: 2000 },
    ranges: {
      type: "array",
      minItems: 1,
      items: {
        type: "object",
        required: ["startMs", "endMs"],
        additionalProperties: false,
        properties: {
          startMs: { type: "integer", minimum: 0 },
          endMs: { type: "integer", minimum: 1 },
        },
      },
    },
  },
} as const;

const usageQuery = {
  type: "object",
  additionalProperties: false,
  properties: {
    episodeId: { type: "integer", minimum: 1 },
    purpose: { type: "string", enum: ["transcribe", "translate", "review", "research", "highlights", "classify"] },
    status: { type: "string", enum: ["intent", "success", "failed", "unknown"] },
    provider: { type: "string", minLength: 1, maxLength: 100 },
    limit: { type: "integer", minimum: 1, maximum: 500 },
  },
} as const;

const errorStatus: Record<string, number> = {
  validation_error: 400,
  unauthorized: 401,
  not_found: 404,
  stale_revision: 409,
  invalid_state: 409,
  override_conflict: 409,
  artifact_mismatch: 409,
  delivery_not_ready: 409,
  internal_error: 500,
};

export interface AppOptions {
  auth?: AuthConfig;
  pythonRunner?: PythonRunner;
  dbPath?: string;
  now?: string;
  domainUrl?: string;
  domainToken?: string;
  multiUser?: boolean;
  logger?: boolean;
}

function requestId(request: FastifyRequest): string {
  return String(request.headers["idempotency-key"]);
}

export function buildApp(options: AppOptions = {}): FastifyInstance {
  const auth = {
    user: options.auth?.user ?? process.env.PODCAST_REVIEW_USER ?? "",
    password: options.auth?.password ?? process.env.PODCAST_REVIEW_PASSWORD ?? "",
    domainUrl: options.domainUrl ?? options.auth?.domainUrl ?? process.env.PODCAST_DOMAIN_URL,
    domainToken: options.domainToken ?? options.auth?.domainToken ?? process.env.PODCAST_DOMAIN_TOKEN,
    multiUser: options.multiUser ?? options.auth?.multiUser ?? process.env.PODCAST_MULTIUSER === "1",
  };
  if (process.env.PODCAST_MULTIUSER !== "1" && (!auth.user || !auth.password)) {
    throw new Error("缺少 PODCAST_REVIEW_USER / PODCAST_REVIEW_PASSWORD");
  }
  const app = Fastify({ logger: options.logger ?? false });
  const runner = options.pythonRunner ?? runPython;
  const scopedPayload = (request: FastifyRequest | undefined, payload: Record<string, unknown>) => {
    const user = request?.user;
    return user
      ? { ...payload, _user_id: user.id, _user_role: user.role }
      : payload;
  };
  const call = <T = unknown>(command: string, payload: Record<string, unknown>, request?: FastifyRequest): Promise<T> =>
    runner(command, scopedPayload(request, payload), {
      dbPath: options.dbPath, now: options.now,
      domainUrl: options.domainUrl, domainToken: options.domainToken,
    });
  const deliveryCall = <T = unknown>(command: string, payload: Record<string, unknown>, request?: FastifyRequest): Promise<T> =>
    runner(command, scopedPayload(request, payload), {
      dbPath: options.dbPath, now: options.now, module: "delivery.cli",
      domainUrl: options.domainUrl, domainToken: options.domainToken,
    });

  app.addHook("onRequest", createAuthHook(auth));
  app.post("/login", (request, reply) => loginWithDomain(auth, request, reply));
  app.setErrorHandler((error, _request, reply) => {
    if (error instanceof PythonCommandError) {
      const status = errorStatus[error.code] ?? 500;
      return reply.code(status).send({
        error: { code: error.code, message: error.message, details: error.details },
      });
    }
    const localError = error as { statusCode?: number; validation?: unknown };
    const status =
      typeof localError.statusCode === "number" && localError.statusCode >= 400
        ? localError.statusCode
        : 500;
    return reply.code(status).send({
      error: {
        code: status === 400 ? "validation_error" : "internal_error",
        message: status === 400 ? "请求参数校验失败" : "审核服务内部错误",
        details: status === 400 ? localError.validation ?? {} : {},
      },
    });
  });
  app.setNotFoundHandler((_request, reply) =>
    reply.code(404).send({ error: { code: "not_found", message: "资源不存在", details: {} } }),
  );

  app.get("/api/health", async () => ({ status: "ok" }));
  app.get(
    "/api/usage",
    { schema: { querystring: usageQuery } },
    async (request) => {
      const query = request.query as Record<string, string | number | undefined>;
      return call("usage-report", {
        episode_id: query.episodeId,
        purpose: query.purpose,
        status: query.status,
        provider: query.provider,
        limit: query.limit,
      }, request);
    },
  );
  app.get("/api/review/episodes", async (request) => call("inbox", {}, request));
  app.get(
    "/api/episodes/:episodeId/review",
    { schema: { params: episodeParams } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      return call("detail", { episode_id: episodeId }, request);
    },
  );

  app.post(
    "/api/episodes/:episodeId/overrides",
    { schema: { params: episodeParams, headers: writeHeaders, body: overrideBody } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      const body = request.body as Record<string, any>;
      return call("save-override", {
        episode_id: episodeId,
        base_revision_id: body.baseRevisionId,
        base_entry_number: body.baseEntryNumber,
        source_artifact_id: body.sourceArtifactId,
        start_ms: body.startMs,
        end_ms: body.endMs,
        source_text: body.sourceText,
        after_text: body.afterText,
        supersedes_override_id: body.supersedesOverrideId ?? null,
        retracted: body.retracted ?? false,
        request_id: requestId(request),
      }, request);
    },
  );

  app.post(
    "/api/episodes/:episodeId/variant",
    { schema: { params: episodeParams, headers: writeHeaders, body: variantBody } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      const body = request.body as { baseRevisionId: number; variant: string };
      return call("select-variant", {
        episode_id: episodeId,
        base_revision_id: body.baseRevisionId,
        variant: body.variant,
      }, request);
    },
  );

  app.post(
    "/api/episodes/:episodeId/approve",
    { schema: { params: episodeParams, headers: writeHeaders, body: revisionBody } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      const { baseRevisionId } = request.body as { baseRevisionId: number };
      return call("approve", {
        episode_id: episodeId,
        base_revision_id: baseRevisionId,
        method: "manual",
        request_id: requestId(request),
      }, request);
    },
  );

  app.post(
    "/api/episodes/:episodeId/hold",
    { schema: { params: episodeParams, headers: writeHeaders, body: revisionBody } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      const { baseRevisionId } = request.body as { baseRevisionId: number };
      return call("hold", { episode_id: episodeId, base_revision_id: baseRevisionId }, request);
    },
  );

  app.post(
    "/api/episodes/:episodeId/resume",
    { schema: { params: episodeParams, headers: writeHeaders, body: revisionBody } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      const { baseRevisionId } = request.body as { baseRevisionId: number };
      return call("resume", { episode_id: episodeId, base_revision_id: baseRevisionId }, request);
    },
  );

  app.post(
    "/api/episodes/:episodeId/reject",
    { schema: { params: episodeParams, headers: writeHeaders, body: rejectBody } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      const body = request.body as {
        baseRevisionId: number;
        reason: string;
        ranges: { startMs: number; endMs: number }[];
      };
      return call("reject", {
        episode_id: episodeId,
        base_revision_id: body.baseRevisionId,
        reason: body.reason,
        ranges: body.ranges.map((range) => ({ start_ms: range.startMs, end_ms: range.endMs })),
        request_id: requestId(request),
      }, request);
    },
  );

  app.post(
    "/api/episodes/:episodeId/revisions",
    { schema: { params: episodeParams, headers: writeHeaders, body: revisionBody } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      const { baseRevisionId } = request.body as { baseRevisionId: number };
      return call("revise-published", {
        episode_id: episodeId,
        base_revision_id: baseRevisionId,
      }, request);
    },
  );

  app.get("/api/library/episodes", async (request) => deliveryCall("library", {}, request));
  app.get(
    "/api/library/episodes/:episodeId",
    { schema: { params: episodeParams } },
    async (request) => {
      const { episodeId } = request.params as { episodeId: number };
      return deliveryCall("detail", { episode_id: episodeId }, request);
    },
  );

  app.get(
    "/api/episodes/:episodeId/audio",
    { schema: { params: episodeParams } },
    async (request, reply) => {
      const { episodeId } = request.params as { episodeId: number };
      const info = await call<MediaInfo>("media-info", { episode_id: episodeId }, request);
      await sendLocalAudio(request, reply, info);
    },
  );

  const staticFiles: Record<string, [string, string]> = {
    "/": ["index.html", "text/html; charset=utf-8"],
    "/index.html": ["index.html", "text/html; charset=utf-8"],
    "/app.js": ["app.js", "text/javascript; charset=utf-8"],
    "/player.js": ["player.js", "text/javascript; charset=utf-8"],
    "/styles.css": ["styles.css", "text/css; charset=utf-8"],
    "/login.html": ["login.html", "text/html; charset=utf-8"],
  };
  app.get("/login", async (_request, reply) => {
    reply.type("text/html; charset=utf-8").header("Cache-Control", "no-cache");
    return reply.send(readFileSync(resolve(PUBLIC_DIR, "login.html")));
  });
  for (const [url, [file, contentType]] of Object.entries(staticFiles)) {
    app.get(url, async (_request, reply) => {
      reply.type(contentType).header("Cache-Control", "no-cache");
      return reply.send(readFileSync(resolve(PUBLIC_DIR, file)));
    });
  }

  return app;
}

async function main(): Promise<void> {
  const app = buildApp({ logger: true });
  const port = Number(process.env.PODCAST_REVIEW_PORT ?? 8787);
  const host = process.env.PODCAST_REVIEW_HOST ?? "127.0.0.1";
  await app.listen({ port, host });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error("审核服务启动失败", error instanceof Error ? error.message : "unknown");
    process.exitCode = 1;
  });
}
