// 执行层·业务暂停标记：所有实际调度入口与付费派发前检查，失败关闭。
import { existsSync } from "node:fs";
import { resolve } from "node:path";

const REPO_ROOT = resolve(import.meta.dirname, "../..");

export class BusinessPausedError extends Error {
  constructor(path: string) {
    super(`业务暂停标记存在，停止新增派发: ${path}`);
    this.name = "BusinessPausedError";
  }
}

export function businessPausePath(): string {
  return process.env.PODCAST_BUSINESS_PAUSE_FILE
    ?? resolve(REPO_ROOT, "var/business.pause");
}

/** 暂停标记存在即抛错；调用方在任何领取／入队／付费派发前执行。 */
export function assertBusinessRunning(): void {
  const path = businessPausePath();
  if (existsSync(path)) throw new BusinessPausedError(path);
}
