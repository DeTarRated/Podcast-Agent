#!/usr/bin/env bash
# 业务循环 cron 入口：统一工作目录与 PATH，失败即非零退出。
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$HOME/.local/bin:$PATH"
exec node --experimental-strip-types runner/src/business-cycle.ts "$@"
