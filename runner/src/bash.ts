// 执行层·工具基础设施：bash 子进程调用（工具经 bash 调 Python 的既定形态）
import { spawn } from "node:child_process";

export interface RunResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

export function runCommand(
  cmd: string,
  args: string[],
  opts: { cwd: string; timeoutMs?: number },
): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { cwd: opts.cwd });
    let stdout = "";
    let stderr = "";
    const timer = opts.timeoutMs
      ? setTimeout(() => child.kill("SIGKILL"), opts.timeoutMs)
      : undefined;
    child.stdout.on("data", (d: Buffer) => {
      stdout += d.toString();
    });
    child.stderr.on("data", (d: Buffer) => {
      stderr += d.toString();
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
      resolve({ exitCode: code ?? -1, stdout, stderr });
    });
  });
}
