"""执行层·Scribe 转写工具；真实付费 POST 绝不自动重试。

安全契约（详见 .agent/scribe-idempotency-design.md）：
- 本地幂等键 call_ref = transcribe:{guid}:{audio_hash}，账本 UNIQUE 防本地重复触发；
  服务端无幂等键，不携带 Idempotency-Key 头，call_ref 不出现在 HTTP 请求里。
- 每次调用最多一次同步 POST，不配置重试、不跟随重定向。
- 拿到 200 后先落 transcription_id sidecar，再落原始响应，最后 outcome 与 Episode；
  ID 落盘后的任何中断都走免费 GET 恢复，不再 POST。
- 读超时/连接中断等结果未知场景 → outcome=unknown，转人工对账，绝不自动重发。
"""
import argparse
import json
import math
import os
import re
import shlex
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from pipeline.download import sha256_file
from pipeline.provider_gate import acquire_or_raise
from state import db, episode, ledger, manifest

API_BASE = "https://api.elevenlabs.io"
TRANSCRIBE_URL = f"{API_BASE}/v1/speech-to-text"
TRANSCRIPT_URL = f"{API_BASE}/v1/speech-to-text/transcripts/{{transcription_id}}"
MODEL_ID = "scribe_v2"
USD_PER_HOUR = 0.22  # PAYG 实测单价（2026-11-06 充值后重测，零残差吻合官方定价）；估算口径，非对账实扣
DEFAULT_READ_TIMEOUT = 120.0
READ_TIMEOUT_DURATION_FACTOR = 3.0

ID_SIDECAR = "scribe_transcription_id.txt"
RAW_NAME = "scribe_raw.json"

_DOTENV_LOADED = False
_ENV_KEY = "ELEVENLABS_API_KEY"


class ScribeError(RuntimeError):
    """调用前校验、产物冲突等可预期错误；信息不得含 API key。"""


@dataclass(frozen=True)
class ScribeResult:
    status: str  # success / failed / unknown / duplicate（duplicate 仅本地拒绝，非账本状态）
    call_ref: str
    episode_id: int
    transcription_id: Optional[str] = None
    raw_path: Optional[str] = None
    error: Optional[str] = None


def build_call_ref(
    guid: str, audio_sha256: str, rebill_attempt: Optional[int] = None
) -> str:
    base = f"transcribe:{guid}:{audio_sha256}"
    if rebill_attempt is not None:
        if not isinstance(rebill_attempt, int) or rebill_attempt < 2:
            raise ScribeError("rebill_attempt 必须从 2 开始")
        base += f":rebill:{rebill_attempt}"
    return base


def read_timeout_for(duration_secs: float) -> float:
    if not math.isfinite(duration_secs) or duration_secs <= 0:
        raise ScribeError(f"非法音频时长: {duration_secs!r}")
    return max(DEFAULT_READ_TIMEOUT, duration_secs * READ_TIMEOUT_DURATION_FACTOR)


def _parse_dotenv(path: Path) -> dict:
    """窄格式 .env 解析：空行/整行注释/export 前缀/引号值；不支持插值与多行。"""
    values = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ScribeError(f".env 第 {lineno} 行格式非法: {path}")
        value = value.strip()
        if value[:1] in ("'", '"'):
            try:
                value = " ".join(shlex.split(value))
            except ValueError:
                raise ScribeError(f".env 第 {lineno} 行引号未闭合: {path}")
        values[key] = value
    return values


def _load_dotenv(path: Optional[Path] = None) -> None:
    """加载 .env 到环境；显式环境变量优先，只写不存在的键。默认路径幂等。"""
    global _DOTENV_LOADED
    if path is None:
        if _DOTENV_LOADED:
            return
        _DOTENV_LOADED = True
        path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for key, value in _parse_dotenv(path).items():
        if key not in os.environ:
            os.environ[key] = value


def _require_api_key() -> str:
    _load_dotenv()
    key = os.environ.get(_ENV_KEY, "").strip()
    if not key:
        raise ScribeError(f"缺少 {_ENV_KEY}（环境变量或项目根 .env）")
    return key


def _id_path(workspace: Path) -> Path:
    return workspace / ID_SIDECAR


def _raw_path(workspace: Path) -> Path:
    return workspace / RAW_NAME


def _create_only(path: Path, data: bytes) -> None:
    """create-only 写：临时文件 fsync 后 os.link 原子创建；已存在拒绝覆盖。"""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise ScribeError(f"产物已存在，拒绝覆盖: {path}")
    finally:
        tmp.unlink(missing_ok=True)


def _ensure_raw_artifact(conn: sqlite3.Connection, episode_id: int, raw_path: Path) -> None:
    """登记或补登记 scribe_raw 产物（done + sha256）。"""
    sha = sha256_file(raw_path)
    for art in manifest.list_for(conn, episode_id):
        if art["step"] == "scribe_raw" and art["path"] == str(raw_path):
            if art["status"] != "done" or art["sha256"] != sha:
                manifest.finish(conn, art["id"], sha256=sha)
            return
    art_id = manifest.add(
        conn,
        episode_id=episode_id,
        step="scribe_raw",
        kind="json",
        path=str(raw_path),
        version=1,
        sha256=sha,
    )
    manifest.finish(conn, art_id, sha256=sha)


def _http_client(transport, timeout: httpx.Timeout, trust_env: bool) -> httpx.Client:
    return httpx.Client(
        transport=transport, timeout=timeout, trust_env=trust_env, follow_redirects=False
    )


def _timeout(duration_secs: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=30.0, read=read_timeout_for(duration_secs), write=120.0, pool=30.0
    )


def _error_summary(exc: httpx.RequestError) -> str:
    return f"{type(exc).__name__}: 结果未知，服务端可能已计费；转人工对账，不自动重发"


def transcribe(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    audio_path: Path,
    workspace: Path,
    transport: Optional[httpx.BaseTransport] = None,
    trust_env: bool = True,
    rebill_attempt: Optional[int] = None,
) -> ScribeResult:
    """同步转写一期音频；rebill 仅允许从既有 unknown 显式新建付费尝试。"""
    audio_path = Path(audio_path)
    workspace = Path(workspace)
    ep = episode.get(conn, episode_id)
    if ep is None:
        raise ScribeError(f"Episode 不存在: {episode_id}")
    if not audio_path.is_file():
        raise ScribeError(f"音频文件不存在: {audio_path}")
    duration = ep["duration_secs"]
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ScribeError("Episode 缺有效音频时长，先补下载元数据")
    if not ep["audio_sha256"]:
        raise ScribeError("Episode 缺 audio_sha256，先完成下载固化")
    actual_hash = sha256_file(audio_path)
    if actual_hash != ep["audio_sha256"]:
        raise ScribeError("音频 hash 与首版固化不一致，拒绝付费调用")
    base_call_ref = build_call_ref(ep["guid"], actual_hash)
    if rebill_attempt is not None:
        base = ledger.get_by_ref(conn, base_call_ref)
        if base is None or base["status"] != "unknown":
            raise ScribeError("显式 re-bill 要求原转写账本为 unknown")
        call_ref = build_call_ref(ep["guid"], actual_hash, rebill_attempt)
    else:
        call_ref = base_call_ref

    # duplicate 闸门先于状态检查：已成功（transcribed）的重复调用返回 duplicate 而非状态报错
    existing = ledger.get_by_ref(conn, call_ref)
    if existing is not None:
        return ScribeResult(
            status="duplicate",
            call_ref=call_ref,
            episode_id=episode_id,
            error=f"账本已存在(status={existing['status']})，不再付费；恢复走 fetch / 对账",
        )
    if ep["status"] != "downloaded":
        raise ScribeError(f"Episode 状态为 {ep['status']}，仅 downloaded 可新起转写")
    for p in (_id_path(workspace), _raw_path(workspace)):
        if p.exists():
            raise ScribeError(f"账本无记录但工作区存在残留: {p}，拒绝重新付费调用")
    for art in manifest.list_for(conn, episode_id):
        if art["step"] == "scribe_raw":
            raise ScribeError("账本无记录但已存在 scribe_raw 产物，拒绝重新付费调用")

    api_key = _require_api_key()
    timeout = _timeout(duration)
    amount = duration / 3600 * USD_PER_HOUR
    # 先过分布式限流／并发闸门，再写付费 intent；被限流时不产生调用记录或外部请求。
    provider_gate, gate_decision = acquire_or_raise("scribe")
    try:
        ledger.record_intent(
            conn,
            episode_id=episode_id,
            purpose="transcribe",
            provider="elevenlabs",
            model=MODEL_ID,
            usage_basis=f"audio_duration_secs={duration:.3f}",
            amount_est=amount,
            call_ref=call_ref,
        )
    except sqlite3.IntegrityError:
        provider_gate.release("scribe", gate_decision.owner)
        return ScribeResult(
            status="duplicate",
            call_ref=call_ref,
            episode_id=episode_id,
            error="call_ref UNIQUE 冲突（并发重复触发），未发起付费调用",
        )
    except Exception:
        provider_gate.release("scribe", gate_decision.owner)
        raise

    try:
        with open(audio_path, "rb") as audio_file:
            with _http_client(transport, timeout, trust_env) as client:
                try:
                    resp = client.post(
                    TRANSCRIBE_URL,
                    headers={"xi-api-key": api_key},
                    data={
                        "model_id": MODEL_ID,
                        "diarize": "true",
                        "timestamps_granularity": "word",
                        "webhook": "false",
                    },
                    files={"file": (audio_path.name, audio_file, "audio/mpeg")},
                )
                except httpx.RequestError as exc:
                    ledger.record_outcome(conn, call_ref, status="unknown")
                    return ScribeResult(
                        status="unknown", call_ref=call_ref, episode_id=episode_id,
                        error=_error_summary(exc),
                    )
    finally:
        provider_gate.release("scribe", gate_decision.owner)

    if resp.status_code != 200:
        ledger.record_outcome(conn, call_ref, status="failed")
        episode.update_status(conn, episode_id, "failed")
        return ScribeResult(
            status="failed", call_ref=call_ref, episode_id=episode_id,
            error=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )

    try:
        data = resp.json()
    except ValueError:
        data = None
    tid = data.get("transcription_id") if isinstance(data, dict) else None
    if not isinstance(tid, str) or not tid.strip():
        ledger.record_outcome(conn, call_ref, status="unknown")
        return ScribeResult(
            status="unknown", call_ref=call_ref, episode_id=episode_id,
            error="HTTP 200 但响应缺 transcription_id，结果未知；人工对账，禁止自动重发",
        )
    tid = tid.strip()

    workspace.mkdir(parents=True, exist_ok=True)
    try:
        # 顺序即安全契约：先锚定 ID，再落原始响应，最后 outcome 与 Episode
        _create_only(_id_path(workspace), (tid + "\n").encode())
        if not isinstance(data.get("words"), list):
            # 缺 words 的响应不是有效产物：原始字节落诊断文件，规范 raw 留空供 GET 恢复
            _create_only(workspace / "scribe_raw.invalid.json", resp.content)
            ledger.record_outcome(conn, call_ref, status="unknown")
            return ScribeResult(
                status="unknown", call_ref=call_ref, episode_id=episode_id,
                transcription_id=tid,
                error="响应缺有效 words；原始字节已存 scribe_raw.invalid.json，"
                      "ID 已锚定，用同 ID GET 再取，不重发 POST",
            )
        _create_only(_raw_path(workspace), resp.content)
        _ensure_raw_artifact(conn, episode_id, _raw_path(workspace))
    except (ScribeError, OSError):
        ledger.record_outcome(conn, call_ref, status="unknown")
        raise

    ledger.record_outcome(conn, call_ref, status="success", amount_est=amount)
    episode.update_status(conn, episode_id, "transcribed")
    return ScribeResult(
        status="success", call_ref=call_ref, episode_id=episode_id,
        transcription_id=tid, raw_path=str(_raw_path(workspace)),
    )


def _validate_raw(raw_bytes: bytes, tid: str) -> dict:
    try:
        data = json.loads(raw_bytes)
    except ValueError:
        raise ScribeError("scribe_raw 不是有效 JSON")
    if not isinstance(data, dict) or not isinstance(data.get("words"), list):
        raise ScribeError("scribe_raw 缺有效 words")
    embedded = data.get("transcription_id")
    if embedded is not None and embedded != tid:
        raise ScribeError("scribe_raw 内嵌 transcription_id 与传入 ID 不一致")
    return data


def _finalize_recovery(conn, ep, call_ref: str, row, tid: str, raw_path: Path) -> ScribeResult:
    episode_id = ep["id"]
    status = row["status"]
    if status == "failed":
        return ScribeResult(
            status="failed", call_ref=call_ref, episode_id=episode_id,
            transcription_id=tid, raw_path=str(raw_path),
            error="账本为 failed 与外部 ID 并存，人工核查，不自动翻转",
        )
    if status == "intent":
        ledger.record_outcome(conn, call_ref, status="success")
    elif status == "unknown":
        ledger.reconcile_success(conn, call_ref)
    if ep["status"] != "transcribed":
        episode.update_status(conn, episode_id, "transcribed")
    return ScribeResult(
        status="success", call_ref=call_ref, episode_id=episode_id,
        transcription_id=tid, raw_path=str(raw_path),
    )


def fetch_transcript(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    transcription_id: str,
    workspace: Path,
    transport: Optional[httpx.BaseTransport] = None,
    trust_env: bool = True,
    call_ref_override: Optional[str] = None,
) -> ScribeResult:
    """凭 transcription_id 恢复结果；本地完整时零 HTTP，否则单次免费 GET。"""
    tid = transcription_id.strip()
    if not tid:
        raise ScribeError("transcription_id 不能为空")
    workspace = Path(workspace)
    ep = episode.get(conn, episode_id)
    if ep is None:
        raise ScribeError(f"Episode 不存在: {episode_id}")
    if not ep["audio_sha256"]:
        raise ScribeError("Episode 缺 audio_sha256，无法关联账本")
    call_ref = call_ref_override or build_call_ref(ep["guid"], ep["audio_sha256"])
    row = ledger.get_by_ref(conn, call_ref)
    if row is None:
        raise ScribeError("找不到对应转写账本记录，拒绝认领外部 ID")
    id_p, raw_p = _id_path(workspace), _raw_path(workspace)
    if id_p.exists() and id_p.read_text(encoding="utf-8").strip() != tid:
        raise ScribeError("ID sidecar 与传入 ID 不一致，拒绝覆盖")

    if raw_p.exists():
        if not id_p.exists():
            raise ScribeError("raw 已存在但 ID sidecar 缺失，拒绝自动认领")
        _validate_raw(raw_p.read_bytes(), tid)
        _ensure_raw_artifact(conn, episode_id, raw_p)
        return _finalize_recovery(conn, ep, call_ref, row, tid, raw_p)

    api_key = _require_api_key()
    timeout = _timeout(ep["duration_secs"] or DEFAULT_READ_TIMEOUT)
    url = TRANSCRIPT_URL.format(transcription_id=quote(tid, safe=""))
    with _http_client(transport, timeout, trust_env) as client:
        try:
            resp = client.get(url, headers={"xi-api-key": api_key})
        except httpx.RequestError as exc:
            return ScribeResult(
                status="failed", call_ref=call_ref, episode_id=episode_id,
                transcription_id=tid,
                error=f"GET 恢复失败({type(exc).__name__})，原 outcome 不变；可免费再次 GET",
            )
    if resp.status_code != 200:
        return ScribeResult(
            status="failed", call_ref=call_ref, episode_id=episode_id,
            transcription_id=tid,
            error=f"GET HTTP {resp.status_code}: {resp.text[:200]}；原 outcome 不变",
        )
    _validate_raw(resp.content, tid)
    workspace.mkdir(parents=True, exist_ok=True)
    if not id_p.exists():
        _create_only(id_p, (tid + "\n").encode())
    _create_only(raw_p, resp.content)
    _ensure_raw_artifact(conn, episode_id, raw_p)
    return _finalize_recovery(conn, ep, call_ref, row, tid, raw_p)


def mark_unknown(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    workspace: Path,
) -> ScribeResult:
    """崩溃后补账：仅 intent 且无 ID sidecar 时可显式补记 unknown；不发 HTTP。"""
    workspace = Path(workspace)
    ep = episode.get(conn, episode_id)
    if ep is None:
        raise ScribeError(f"Episode 不存在: {episode_id}")
    if not ep["audio_sha256"]:
        raise ScribeError("Episode 缺 audio_sha256，无法关联账本")
    call_ref = build_call_ref(ep["guid"], ep["audio_sha256"])
    row = ledger.get_by_ref(conn, call_ref)
    if row is None:
        raise ScribeError("找不到对应转写账本记录")
    if _id_path(workspace).exists():
        raise ScribeError("ID sidecar 已存在，应走 fetch 恢复而非 mark-unknown")
    if row["status"] == "unknown":
        return ScribeResult(status="unknown", call_ref=call_ref, episode_id=episode_id,
                            error="已是 unknown；请人工对账，不自动重发")
    if row["status"] != "intent":
        raise ScribeError(f"当前账本状态为 {row['status']}，只有 intent 可补记 unknown")
    ledger.record_outcome(conn, call_ref, status="unknown")
    return ScribeResult(status="unknown", call_ref=call_ref, episode_id=episode_id,
                        error="已补记 unknown；请人工对账，不自动重发")


def main(argv=None) -> int:
    """CLI：供执行层工具经 bash 调用。stdout 最后一行是单个 JSON，日志只去 stderr。"""
    _load_dotenv()  # 在读取 PODCAST_AGENT_DB 之前加载
    parser = argparse.ArgumentParser(description="Scribe 转写工具（付费 POST 绝不自动重试）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_trans = sub.add_parser("transcribe")
    p_trans.add_argument("episode_id", type=int)
    p_trans.add_argument("audio")
    p_trans.add_argument("--rebill-attempt", type=int)
    p_trans.add_argument("--direct", action="store_true", help="显式 re-bill 时绕过系统代理")
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("episode_id", type=int)
    p_fetch.add_argument("transcription_id")
    p_fetch.add_argument("--call-ref")
    p_unknown = sub.add_parser("mark-unknown")
    p_unknown.add_argument("episode_id", type=int)
    for p in (p_trans, p_fetch, p_unknown):
        p.add_argument("--workspace")
        p.add_argument("--db")

    args = parser.parse_args(argv)
    workspace = Path(args.workspace) if args.workspace else Path(f"var/episodes/{args.episode_id}")
    if args.db:
        conn = db.connect(args.db)
        db.init_schema(conn)
    else:
        conn = db.open_default()
    try:
        if args.command == "transcribe":
            result = transcribe(
                conn, episode_id=args.episode_id, audio_path=Path(args.audio),
                workspace=workspace, trust_env=not args.direct,
                rebill_attempt=args.rebill_attempt,
            )
        elif args.command == "fetch":
            result = fetch_transcript(
                conn, episode_id=args.episode_id, transcription_id=args.transcription_id,
                workspace=workspace, call_ref_override=args.call_ref,
            )
        else:
            result = mark_unknown(conn, episode_id=args.episode_id, workspace=workspace)
    except ScribeError as exc:
        result = ScribeResult(status="failed", call_ref="",
                              episode_id=args.episode_id, error=str(exc))
    finally:
        conn.close()
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
