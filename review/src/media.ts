// 审核层·已登记本地音频的单段 Range 响应；路径由 Python artifact 白名单解析。
import { createReadStream, realpathSync, statSync } from "node:fs";
import { resolve, sep } from "node:path";
import type { FastifyReply, FastifyRequest } from "fastify";

import { PythonCommandError } from "./python.ts";

export interface MediaInfo {
  path: string;
  size: number;
  sha256: string;
  content_type: string;
  workspace?: string;
}

interface ByteRange {
  start: number;
  end: number;
}

export function parseByteRange(header: string | undefined, size: number): ByteRange {
  if (size <= 0) throw new Error("invalid_range");
  if (!header) return { start: 0, end: size - 1 };
  const match = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!match || (!match[1] && !match[2])) throw new Error("invalid_range");
  let start: number;
  let end: number;
  if (!match[1]) {
    const suffix = Number(match[2]);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) throw new Error("invalid_range");
    start = Math.max(0, size - suffix);
    end = size - 1;
  } else {
    start = Number(match[1]);
    end = match[2] ? Number(match[2]) : size - 1;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)) throw new Error("invalid_range");
    end = Math.min(end, size - 1);
  }
  if (start < 0 || start >= size || end < start) throw new Error("invalid_range");
  return { start, end };
}

export async function sendLocalAudio(
  request: FastifyRequest,
  reply: FastifyReply,
  info: MediaInfo,
): Promise<void> {
  let path: string;
  let size: number;
  try {
    path = realpathSync(info.path);
    const workspace = realpathSync(info.workspace ?? resolve(info.path, ".."));
    if (path !== workspace && !path.startsWith(workspace + sep)) throw new Error("outside_workspace");
    const stat = statSync(path);
    if (!stat.isFile() || stat.size !== info.size) throw new Error("media_changed");
    size = stat.size;
  } catch {
    throw new PythonCommandError("artifact_mismatch", "音频文件不存在、越界或已变化");
  }

  let range: ByteRange;
  try {
    range = parseByteRange(request.headers.range, size);
  } catch {
    reply.header("Content-Range", `bytes */${size}`);
    await reply.code(416).send({
      error: { code: "invalid_range", message: "非法音频 Range", details: {} },
    });
    return;
  }
  const partial = Boolean(request.headers.range);
  reply
    .code(partial ? 206 : 200)
    .type(info.content_type || "audio/mpeg")
    .header("Accept-Ranges", "bytes")
    .header("Cache-Control", "private, no-cache")
    .header("Content-Length", range.end - range.start + 1);
  if (partial) reply.header("Content-Range", `bytes ${range.start}-${range.end}/${size}`);
  // HEAD 也交给 Fastify 发送同一流：框架会抑制响应体，同时保留实体 Content-Length。
  await reply.send(createReadStream(path, { start: range.start, end: range.end }));
}
