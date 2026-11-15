// 执行层·子任务终态等待：单一绝对期限，只停止等待，不取消或重发底层任务。
export const DEFAULT_TASK_WAIT_MS = 30 * 60 * 1000;

export function taskWaitTimeout(raw: string | undefined): number {
  const value = raw === undefined ? DEFAULT_TASK_WAIT_MS : Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0 || value > 2_147_483_647) {
    throw new Error("PODCAST_SUBAGENT_WAIT_TIMEOUT_MS 必须为有效的正整数毫秒");
  }
  return value;
}

export class TaskWaitTimeout extends Error {
  constructor() { super("等待子任务终态超时；底层任务未取消，禁止重发付费请求"); }
}

export async function waitForTerminalTask<T extends { state?: { status?: string } }>(
  wait: () => Promise<T>,
  timeoutMs: number,
  pollMs = 1000,
): Promise<T> {
  taskWaitTimeout(String(timeoutMs));
  if (!Number.isSafeInteger(pollMs) || pollMs <= 0) throw new Error("pollMs 必须为正整数");
  const deadline = performance.now() + timeoutMs;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new TaskWaitTimeout()), timeoutMs);
  });
  const run = async (): Promise<T> => {
    while (true) {
      if (performance.now() >= deadline) throw new TaskWaitTimeout();
      const record = await wait();
      if (performance.now() >= deadline) throw new TaskWaitTimeout();
      const status = record?.state?.status;
      if (status === "terminal") return record;
      if (!["pending", "running", "waiting", "sleeping", "completing"].includes(status ?? "")) {
        throw new Error("子任务返回非法状态，拒绝将其视为完成");
      }
      await new Promise((resolve) => setTimeout(resolve, Math.min(pollMs, Math.max(0, deadline - performance.now()))));
    }
  };
  try {
    return await Promise.race([run(), expired]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
