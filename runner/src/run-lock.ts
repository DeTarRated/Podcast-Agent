// 执行层·跨进程共享运行锁：所有实际调度入口（含人工启动）竞争同一把 flock。
// 父进程经 flock 子进程持锁；持锁成功以子进程输出握手信号为准，释放绑定父进程生命周期。
// 由队列启动的子进程经 PODCAST_RUN_LOCK_TOKEN 识别父进程持锁身份，避免重复加锁死锁。
import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";

const REPO_ROOT = resolve(import.meta.dirname, "../..");
const LOCK_SIGNAL = "podcast-agent-run-lock";

export interface RunLock {
  release: () => void;
}

export function businessLockPath(): string {
  return process.env.PODCAST_BUSINESS_LOCK_FILE
    ?? resolve(REPO_ROOT, "var/business.lock");
}

/** 申请共享锁；被占用或锁进程异常时返回 null。成功时锁进程保持存活直到 release。 */
export async function acquireRunLock(spawnImpl: typeof spawn): Promise<RunLock | null> {
  const child = spawnImpl("flock", [
    "--nonblocking", businessLockPath(), "--command",
    `printf '%s\\n' '${LOCK_SIGNAL}'; exec sleep infinity`,
  ], { stdio: ["ignore", "pipe", "ignore"] });

  const acquired = await new Promise<boolean>((resolvePromise) => {
    let stdout = "";
    let settled = false;
    const finish = (ok: boolean) => {
      if (!settled) {
        settled = true;
        resolvePromise(ok);
      }
    };
    (child as ChildProcess).stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString();
      if (stdout.includes(LOCK_SIGNAL)) finish(true);
    });
    child.on("error", () => finish(false));
    // 竞争者 flock 以非零码退出且无任何输出；持锁成功路径不会走到这里。
    child.on("close", () => finish(false));
  });
  if (!acquired) {
    try { child.kill("SIGKILL"); } catch { /* 已退出 */ }
    return null;
  }
  const release = () => {
    try { child.kill("SIGKILL"); } catch { /* 已退出 */ }
    process.removeListener("exit", release);
  };
  // 父进程异常退出时不得遗留持锁进程；release 时移除监听避免累积。
  process.on("exit", release);
  return { release };
}

/**
 * 实际驱动入口的锁协议：父进程已持锁（token 注入）则跳过；
 * 人工直接启动时自行竞争共享锁，锁被占用返回 null。
 */
export async function ensureRunLock(spawnImpl: typeof spawn): Promise<RunLock | null> {
  if (process.env.PODCAST_RUN_LOCK_TOKEN) return { release: () => {} };
  return acquireRunLock(spawnImpl);
}

/** 队列 spawn 子进程时注入持锁身份。 */
export function runLockChildEnv(token: string): NodeJS.ProcessEnv {
  return { ...process.env, PODCAST_RUN_LOCK_TOKEN: token };
}
