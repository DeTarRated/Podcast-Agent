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
  // 持锁子进程自己监测父进程存活：父进程被信号杀死时（SIGTERM/SIGKILL），
  // 子进程在 ~2s 内自行退出并释放 flock，避免锁被永久占用。
  const parentPid = process.pid;
  const hold = `printf '%s\\n' '${LOCK_SIGNAL}'; while kill -0 ${parentPid} 2>/dev/null; do sleep 2; done`;
  const child = spawnImpl("flock", [
    "--nonblocking", businessLockPath(), "--command", hold,
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
  // 握手后必须释放管道并 unref：否则持锁子进程的 stdout 会拖住事件循环，
  // 父进程无法退出，而子进程又在等父进程退出，形成互等死锁。
  try {
    child.stdout?.removeAllListeners();
    child.stdout?.destroy();
    child.unref();
  } catch { /* 老版本 fake 可能未实现 unref */ }
  const release = () => {
    try { child.kill("SIGKILL"); } catch { /* 已退出 */ }
    process.removeListener("exit", release);
  };
  // 正常退出与信号终止都要释放；release 时移除监听避免累积。
  const onExit = () => { release(); };
  const onSignal = (signal: NodeJS.Signals) => {
    release();
    process.off("exit", onExit);
    process.off("SIGINT", onSigint);
    process.off("SIGTERM", onSigterm);
    process.off("SIGHUP", onSighup);
    // 交给默认处理让进程退出，锁已释放。
    process.kill(process.pid, signal);
  };
  const onSigint = () => onSignal("SIGINT");
  const onSigterm = () => onSignal("SIGTERM");
  const onSighup = () => onSignal("SIGHUP");
  process.on("exit", onExit);
  process.on("SIGINT", onSigint);
  process.on("SIGTERM", onSigterm);
  process.on("SIGHUP", onSighup);
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
