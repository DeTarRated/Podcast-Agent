// 审核层·单用户 Basic Auth（兼容）与多用户数据库会话鉴权；写请求同源保护。
import { createHash, timingSafeEqual } from "node:crypto";
import type { FastifyReply, FastifyRequest } from "fastify";

export interface AuthConfig {
  user: string;
  password: string;
  domainUrl?: string;
  domainToken?: string;
  multiUser?: boolean;
}

export interface AuthenticatedUser {
  id: number;
  email: string;
  role: string;
}

declare module "fastify" {
  interface FastifyRequest {
    user?: AuthenticatedUser;
  }
}

function digest(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

function matches(actual: string, expected: string): boolean {
  return timingSafeEqual(digest(actual), digest(expected));
}

function writeGuard(request: FastifyRequest, reply: FastifyReply): boolean {
  if (!["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) return true;
  const origin = request.headers.origin;
  const host = request.headers.host;
  if (origin) {
    let originHost = "";
    try {
      originHost = new URL(origin).host;
    } catch {
      originHost = "";
    }
    if (!host || originHost !== host) {
      void reply.code(403).send({ error: { code: "forbidden", message: "拒绝跨站写请求", details: {} } });
      return false;
    }
  }
  if (request.headers["sec-fetch-site"] === "cross-site") {
    void reply.code(403).send({ error: { code: "forbidden", message: "拒绝跨站写请求", details: {} } });
    return false;
  }
  return true;
}

function basicAuthHook(config: AuthConfig) {
  return async function authHook(request: FastifyRequest, reply: FastifyReply): Promise<void> {
    if (request.raw.url === "/api/health") return;
    const header = request.headers.authorization;
    let authenticated = false;
    if (header?.startsWith("Basic ")) {
      try {
        const decoded = Buffer.from(header.slice(6), "base64").toString("utf8");
        const separator = decoded.indexOf(":");
        if (separator >= 0) {
          authenticated =
            matches(decoded.slice(0, separator), config.user) &&
            matches(decoded.slice(separator + 1), config.password);
        }
      } catch {
        authenticated = false;
      }
    }
    if (!authenticated) {
      reply.header("WWW-Authenticate", 'Basic realm="podcast-agent", charset="UTF-8"');
      await reply.code(401).send({
        error: { code: "unauthorized", message: "需要审核访问凭据", details: {} },
      });
      return;
    }
    if (!writeGuard(request, reply)) return;
  };
}

function sessionCookie(token: string, secure: boolean, maxAge: number): string {
  return `podcast_session=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure ? "; Secure" : ""}`;
}

function sessionToken(request: FastifyRequest): string | undefined {
  const header = request.headers.cookie;
  if (!header) return undefined;
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === "podcast_session") return decodeURIComponent(rest.join("="));
  }
  return undefined;
}

export async function loginWithDomain(config: AuthConfig, request: FastifyRequest, reply: FastifyReply): Promise<void> {
  const base = (config.domainUrl ?? process.env.PODCAST_DOMAIN_URL)?.replace(/\/$/, "");
  if (!base) throw new Error("多用户模式需要 PODCAST_DOMAIN_URL");
  const body = request.body as { email?: string; password?: string };
  if (!body?.email || !body.password) {
    await reply.code(400).send({ error: { code: "validation_error", message: "邮箱和密码必填", details: {} } });
    return;
  }
  try {
    const response = await fetch(`${base}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(15_000),
    });
    const payload = await response.json() as {
      status?: string;
      data?: { token: string; user: AuthenticatedUser };
      error?: { message?: string };
    };
    if (payload.status !== "success" || !payload.data) {
      await reply.code(401).send({ error: { code: "unauthorized", message: payload.error?.message ?? "邮箱或密码错误", details: {} } });
      return;
    }
    request.user = payload.data.user;
    reply.header("Set-Cookie", sessionCookie(payload.data.token, process.env.PODCAST_AUTH_SECURE_COOKIE === "1", 7 * 24 * 3600));
    await reply.send(payload.data);
  } catch {
    await reply.code(503).send({ error: { code: "auth_unavailable", message: "登录服务暂不可用", details: {} } });
  }
}

function multiUserHook(config: AuthConfig) {
  const base = (config.domainUrl ?? process.env.PODCAST_DOMAIN_URL)?.replace(/\/$/, "");
  if (!base) throw new Error("多用户模式需要 PODCAST_DOMAIN_URL");

  async function sessionUser(token: string | undefined): Promise<AuthenticatedUser | null> {
    if (!token) return null;
    try {
      const response = await fetch(`${base}/auth/session`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ token }),
        signal: AbortSignal.timeout(10_000),
      });
      const payload = await response.json() as { status?: string; data?: AuthenticatedUser };
      return payload.status === "success" && payload.data ? payload.data : null;
    } catch {
      return null;
    }
  }

  return async function authHook(request: FastifyRequest, reply: FastifyReply): Promise<void> {
    const url = request.raw.url ?? "";
    if (url === "/api/health" || url === "/login" || url === "/login.html" ||
        url === "/styles.css" || url === "/app.js" || url === "/player.js") {
      if (url === "/login" && request.method === "POST") {
        if (!writeGuard(request, reply)) return;
      }
      return;
    }

    const user = await sessionUser(sessionToken(request));
    if (user) {
      request.user = user;
      if (url === "/logout" && request.method === "POST") {
        if (!writeGuard(request, reply)) return;
        try {
          await fetch(`${base}/auth/logout`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: sessionToken(request) }),
          });
        } finally {
          reply.header("Set-Cookie", sessionCookie("", process.env.PODCAST_AUTH_SECURE_COOKIE === "1", 0));
        }
        await reply.send({ status: "success" });
        return;
      }
      if (!writeGuard(request, reply)) return;
      return;
    }

    if (url.startsWith("/api/")) {
      await reply.code(401).send({ error: { code: "unauthorized", message: "请先登录", details: {} } });
    } else {
      await reply.redirect("/login.html");
    }
  };
}

export function createAuthHook(config: AuthConfig) {
  const enabled = config.multiUser ?? process.env.PODCAST_MULTIUSER === "1";
  return enabled ? multiUserHook(config) : basicAuthHook(config);
}
