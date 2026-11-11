// Python CLI 适配测试。
//
// 模拟场景：
// - 成功 JSON 返回与 stdin payload 传递
// - 领域 failed 保留稳定 code/details
// - 非零退出、损坏 JSON 与 stderr 不回显
// - 超时终止子进程并返回 internal_error
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough, Readable, Writable } from "node:stream";
import type { spawn } from "node:child_process";

import { PythonCommandError, runPython } from "./python.ts";

function fakeSpawn(stdout: string, exitCode = 0, stderr = ""): typeof spawn {
  return ((_command: string, _args: string[], _options: object) => {
    const child = new EventEmitter() as any;
    child.stdout = Readable.from([stdout]);
    child.stderr = Readable.from([stderr]);
    child.stdin = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
    child.kill = () => true;
    child.stdout.on("end", () => child.emit("close", exitCode));
    return child;
  }) as typeof spawn;
}

test("Python 成功结果与 JSON stdin", async () => {
  let received = "";
  const base = fakeSpawn('{"status":"success","data":{"value":42}}\n');
  const spawnImpl = ((command: string, args: string[], options: object) => {
    assert.equal(command, "uv");
    assert.ok(args.includes("detail"));
    assert.equal((options as { shell?: boolean }).shell, false);
    const child = base("uv", args, options as any) as any;
    child.stdin = new Writable({
      write(chunk, _encoding, callback) {
        received += chunk.toString();
        callback();
      },
    });
    return child;
  }) as typeof spawn;
  const result = await runPython<{ value: number }>("detail", { episode_id: 7 }, { spawnImpl });
  assert.equal(result.value, 42);
  assert.equal(JSON.parse(received).episode_id, 7);
});

test("Python 领域错误映射稳定 code 与 details", async () => {
  const spawnImpl = fakeSpawn(
    '{"status":"failed","error":{"code":"stale_revision","message":"旧版本","details":{"current":9}}}',
    1,
  );
  await assert.rejects(
    runPython("approve", {}, { spawnImpl }),
    (error: unknown) => {
      assert.ok(error instanceof PythonCommandError);
      assert.equal(error.code, "stale_revision");
      assert.deepEqual(error.details, { current: 9 });
      return true;
    },
  );
});

test("损坏 JSON 与非零无领域结果均为 internal_error", async () => {
  await assert.rejects(
    runPython("detail", {}, { spawnImpl: fakeSpawn("not-json", 0) }),
    (error: unknown) => error instanceof PythonCommandError && error.code === "internal_error",
  );
  await assert.rejects(
    runPython("detail", {}, { spawnImpl: fakeSpawn("{}", 2, "secret stderr") }),
    (error: unknown) => {
      assert.ok(error instanceof PythonCommandError);
      assert.equal(error.code, "internal_error");
      assert.equal(JSON.stringify(error.details).includes("secret stderr"), false);
      return true;
    },
  );
});

test("领域 HTTP 成功与失败映射，不启动 Python 子进程", async () => {
  let capturedUrl = "";
  let capturedHeaders: Record<string, string> = {};
  const fetchImpl = async (input: any, init: any) => {
    capturedUrl = String(input);
    capturedHeaders = init.headers;
    return new Response(JSON.stringify({ status: "success", data: { value: 7 } }), { status: 200 });
  };
  const result = await runPython<{ value: number }>("inbox", {}, {
    domainUrl: "http://127.0.0.1:8788/", domainToken: "secret", fetchImpl,
  });
  assert.equal(result.value, 7);
  assert.equal(capturedUrl, "http://127.0.0.1:8788/rpc/review/inbox");
  assert.equal(capturedHeaders["X-Domain-Token"], "secret");

  const failed = async () => new Response(JSON.stringify({
    status: "failed", error: { code: "stale_revision", message: "旧版", details: {} },
  }), { status: 200 });
  await assert.rejects(
    runPython("approve", {}, { domainUrl: "http://domain", fetchImpl: failed }),
    (error: unknown) => error instanceof PythonCommandError && error.code === "stale_revision",
  );
  const network = async () => { throw new Error("offline"); };
  await assert.rejects(
    runPython("inbox", {}, { domainUrl: "http://domain", fetchImpl: network }),
    (error: unknown) => error instanceof PythonCommandError && error.code === "internal_error",
  );
});

test("Python 命令超时 kill 并拒绝", async () => {
  const spawnImpl = (() => {
    const child = new EventEmitter() as any;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
    child.kill = () => true;
    return child;
  }) as typeof spawn;
  await assert.rejects(
    runPython("detail", {}, { spawnImpl, timeoutMs: 5 }),
    (error: unknown) => error instanceof PythonCommandError && error.message.includes("超时"),
  );
});
