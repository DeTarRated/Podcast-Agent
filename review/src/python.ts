// 审核层·Python 窄 CLI 适配；Node 不拼 SQL，也不实现另一套审核状态机。
import { spawn } from "node:child_process";
import { resolve } from "node:path";

const REPO_ROOT = resolve(import.meta.dirname, "../..");
const MAX_OUTPUT_BYTES = 20 * 1024 * 1024;

export class PythonCommandError extends Error {
  readonly code: string;
  readonly details: unknown;

  constructor(code: string, message: string, details: unknown = {}) {
    super(message);
    this.name = "PythonCommandError";
    this.code = code;
    this.details = details;
  }
}

export interface PythonRunOptions {
  dbPath?: string;
  now?: string;
  timeoutMs?: number;
  module?: "review.cli" | "delivery.cli";
  domainUrl?: string;
  domainToken?: string;
  fetchImpl?: typeof fetch;
  spawnImpl?: typeof spawn;
}

export type PythonRunner = <T = unknown>(
  command: string,
  payload: Record<string, unknown>,
  options?: PythonRunOptions,
) => Promise<T>;

async function runDomain<T>(
  command: string,
  payload: Record<string, unknown>,
  options: PythonRunOptions,
): Promise<T> {
  const module = options.module ?? "review.cli";
  const area = module === "delivery.cli" ? "delivery" : "review";
  const base = (options.domainUrl ?? process.env.PODCAST_DOMAIN_URL)?.replace(/\/$/, "");
  if (!base) throw new PythonCommandError("internal_error", "未配置领域服务地址");
  const token = options.domainToken ?? process.env.PODCAST_DOMAIN_TOKEN ?? "";
  const fetchImpl = options.fetchImpl ?? fetch;
  let response: Response;
  try {
    response = await fetchImpl(`${base}/rpc/${area}/${command}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-Domain-Token": token,
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(options.timeoutMs ?? 60_000),
    });
  } catch {
    throw new PythonCommandError("internal_error", "领域服务不可用或超时");
  }
  let envelope: any;
  try {
    envelope = await response.json();
  } catch {
    throw new PythonCommandError("internal_error", "领域服务未返回有效 JSON");
  }
  if (envelope.status === "success") return envelope.data as T;
  if (envelope.status === "failed" && envelope.error) {
    throw new PythonCommandError(
      envelope.error.code ?? "internal_error",
      envelope.error.message ?? "领域操作失败",
      envelope.error.details ?? {},
    );
  }
  throw new PythonCommandError("internal_error", `领域服务 HTTP ${response.status}`);
}

export async function runPython<T = unknown>(
  command: string,
  payload: Record<string, unknown>,
  options: PythonRunOptions = {},
): Promise<T> {
  if (options.domainUrl ?? process.env.PODCAST_DOMAIN_URL) {
    return runDomain<T>(command, payload, options);
  }
  const args = ["run", "python", "-m", options.module ?? "review.cli", command];
  if (options.dbPath) args.push("--db", options.dbPath);
  if (options.now) args.push("--now", options.now);
  const spawnImpl = options.spawnImpl ?? spawn;

  return new Promise<T>((resolvePromise, rejectPromise) => {
    const child = spawnImpl("uv", args, {
      cwd: REPO_ROOT,
      stdio: ["pipe", "pipe", "pipe"],
      shell: false,
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let settled = false;
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill("SIGKILL");
      rejectPromise(new PythonCommandError("internal_error", "Python 审核命令超时"));
    }, options.timeoutMs ?? 60_000);

    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      rejectPromise(error);
    };

    child.on("error", (error) => {
      fail(new PythonCommandError("internal_error", "无法启动 Python 审核命令", error.message));
    });
    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > MAX_OUTPUT_BYTES) {
        child.kill("SIGKILL");
        fail(new PythonCommandError("internal_error", "Python 审核命令输出过大"));
        return;
      }
      stdout.push(Buffer.from(chunk));
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderrBytes += chunk.length;
      if (stderrBytes <= 64 * 1024) stderr.push(Buffer.from(chunk));
    });
    child.on("close", (exitCode) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      const output = Buffer.concat(stdout).toString("utf8").trim();
      const lastLine = output.split("\n").at(-1) ?? "";
      let parsed: unknown;
      try {
        parsed = JSON.parse(lastLine);
      } catch {
        rejectPromise(new PythonCommandError("internal_error", "Python 审核命令未返回有效 JSON"));
        return;
      }
      const envelope = parsed as {
        status?: string;
        data?: unknown;
        error?: { code?: string; message?: string; details?: unknown };
      };
      if (exitCode === 0 && envelope.status === "success") {
        resolvePromise(envelope.data as T);
        return;
      }
      if (envelope.status === "failed" && envelope.error) {
        rejectPromise(
          new PythonCommandError(
            envelope.error.code ?? "internal_error",
            envelope.error.message ?? "审核操作失败",
            envelope.error.details ?? {},
          ),
        );
        return;
      }
      rejectPromise(
        new PythonCommandError("internal_error", "Python 审核命令异常退出", {
          exitCode,
          // stderr 只保留长度信息，避免把潜在敏感内容带入 HTTP 日志。
          stderrBytes: Math.min(stderrBytes, 64 * 1024),
        }),
      );
    });
    child.stdin.on("error", (error) => {
      fail(new PythonCommandError("internal_error", "Python 审核命令输入失败", error.message));
    });
    child.stdin.end(JSON.stringify(payload));
  });
}
