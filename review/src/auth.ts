// 审核层·单用户 Basic Auth 与写请求同源保护；生产必须由 HTTPS 反向代理暴露。
import { createHash, timingSafeEqual } from "node:crypto";
import type { FastifyReply, FastifyRequest } from "fastify";

export interface AuthConfig {
  user: string;
  password: string;
}

function digest(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

function matches(actual: string, expected: string): boolean {
  return timingSafeEqual(digest(actual), digest(expected));
}

export function createAuthHook(config: AuthConfig) {
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

    if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) {
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
          await reply.code(403).send({
            error: { code: "forbidden", message: "拒绝跨站写请求", details: {} },
          });
          return;
        }
      }
      if (request.headers["sec-fetch-site"] === "cross-site") {
        await reply.code(403).send({
          error: { code: "forbidden", message: "拒绝跨站写请求", details: {} },
        });
      }
    }
  };
}
