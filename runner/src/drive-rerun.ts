// 局部重跑真实派发入口：node --experimental-strip-types src/drive-rerun.ts <dispatch.json>
import { driveDispatchFile } from "./dispatch-driver.ts";

const file = process.argv[2];
if (!file) throw new Error("用法: drive-rerun.ts <dispatch.json>");
await driveDispatchFile(file);
