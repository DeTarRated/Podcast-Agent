// 执行层·仓库 .env 统一加载：所有驱动入口在任何锁/暂停判断之前调用。
// 仅允许文件不存在（开发环境）；格式错误一律抛错，失败关闭。
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const REPO_ROOT = resolve(import.meta.dirname, "../..");

export function loadRepoEnv(path?: string): void {
  const target = path ?? resolve(REPO_ROOT, ".env");
  let content: string;
  try {
    content = readFileSync(target, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw error;
  }
  content.split("\n").forEach((raw, index) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    const match = /^([A-Z][A-Z0-9_]*)=(.*)$/.exec(line);
    if (!match) {
      throw new Error(`.env 第 ${index + 1} 行格式非法（要求 KEY=VALUE）: ${line.slice(0, 50)}`);
    }
    // 与 Python 侧 .env 解析一致：成对引号包裹的值去掉引号，保留内部空格。
    let value = match[2];
    if (value.length >= 2 && ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")))) {
      value = value.slice(1, -1);
    }
    if (!(match[1] in process.env)) process.env[match[1]] = value;
  });
}
