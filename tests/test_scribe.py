"""Scribe 转写工具测试；全部 HTTP 由 httpx.MockTransport 提供，禁止真实付费调用。

模拟场景：
- 正常 POST：multipart 参数、认证头、无 Idempotency-Key、ID 先于 raw 锚定、原始响应/产物/账本/Episode 完整
- 同 call_ref 重复触发与并发 UNIQUE 冲突：拒绝第二次 POST，不改原 outcome
- 调用前状态、音频 hash、时长、API key 校验失败：无 intent、无 HTTP
- 读超时与连接中断：outcome=unknown、Episode 不推进、后续重复调用零重发
- HTTP 4xx / 5xx：outcome=failed、Episode=failed、单次请求、无成功产物
- 200 非 JSON、缺 transcription_id、缺 words：不误记 success、不自动重发
- POST 后模拟崩溃留下 intent：mark-unknown 补账；已有 ID 时拒绝误标并转恢复
- 已拿 ID 后 GET 恢复：只 GET 不 POST，原始响应落盘，outcome 与 Episode 补齐
- 本地 raw 已完整：恢复不 GET、不覆盖，补 manifest / Episode
- raw 或 sidecar 内容冲突：拒绝覆盖、拒绝跨期认领
- GET 404 / 网络错误：原 outcome 不变，可免费再次 GET
- unknown 经明确 ID 恢复：reconcile_success 核销为 success
- failed 账本与外部 ID 并存：返回冲突不自动翻转
- .env 窄格式解析、显式环境优先、缺 key 零 intent
- read timeout 时长公式与 CLI 单行 JSON / 退出码
- 显式 re-bill：仅 unknown 可创建新尝试，旧 unknown 保留，新尝试成功，重复 attempt 不重发
"""
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from pipeline import scribe
from pipeline.download import sha256_file
from state import db, episode, ledger, manifest

TID = "tid-test-1"
AUDIO_BYTES = b"fake-audio-bytes"


def ok_payload(tid=TID, with_words=True):
    payload = {
        "transcription_id": tid,
        "language_code": "eng",
        "text": "hello world",
    }
    if with_words:
        payload["words"] = [
            {"text": "hello", "start": 0.0, "end": 0.5, "type": "word", "speaker_id": "speaker_0"},
            {"text": " ", "start": 0.5, "end": 0.5, "type": "spacing", "speaker_id": "speaker_0"},
            {"text": "world", "start": 0.5, "end": 1.0, "type": "word", "speaker_id": "speaker_0"},
        ]
    return payload


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """测试不读项目根 .env，key 用假值，绝不发真实请求。"""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(scribe, "_DOTENV_LOADED", True)
    class FakeGate:
        def release(self, provider, owner):
            return True
    monkeypatch.setattr(
        scribe, "acquire_or_raise",
        lambda provider: (FakeGate(), type("Decision", (), {"owner": "test-owner"})()),
    )


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture()
def audio(tmp_path) -> Path:
    p = tmp_path / "audio.mp3"
    p.write_bytes(AUDIO_BYTES)
    return p


@pytest.fixture()
def workspace(tmp_path) -> Path:
    return tmp_path / "ws"


def make_episode(conn, audio, *, guid="guid-1", duration=30.0, status="downloaded"):
    ep_id = episode.create(conn, guid=guid, feed="feed-1")
    episode.set_audio(conn, ep_id, sha256=sha256_file(audio), duration_secs=duration)
    episode.update_status(conn, ep_id, status)
    return ep_id


def mock_transport(calls, handler):
    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(_handler)


def json_response(payload, status=200):
    return httpx.Response(status, json=payload)


def transcribe(conn, ep_id, audio, workspace, calls, handler):
    return scribe.transcribe(
        conn, episode_id=ep_id, audio_path=audio, workspace=workspace,
        transport=mock_transport(calls, handler), trust_env=False,
    )


def fetch(conn, ep_id, workspace, calls, handler, tid=TID):
    return scribe.fetch_transcript(
        conn, episode_id=ep_id, transcription_id=tid, workspace=workspace,
        transport=mock_transport(calls, handler), trust_env=False,
    )


def call_ref_for(conn, ep_id):
    ep = episode.get(conn, ep_id)
    return scribe.build_call_ref(ep["guid"], ep["audio_sha256"])


def seed_intent(conn, ep_id):
    ledger.record_intent(
        conn, episode_id=ep_id, purpose="transcribe", provider="elevenlabs",
        model="scribe_v2", usage_basis="audio_duration_secs=30.000",
        amount_est=0.003, call_ref=call_ref_for(conn, ep_id),
    )


def test_transcribe_success_full_flow(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    calls = []
    raw_bytes = json.dumps(ok_payload()).encode()
    result = transcribe(conn, ep_id, audio, workspace, calls,
                        lambda req: httpx.Response(200, content=raw_bytes))

    assert result.status == "success"
    assert result.transcription_id == TID
    assert len(calls) == 1
    req = calls[0]
    assert req.method == "POST"
    assert str(req.url) == scribe.TRANSCRIBE_URL
    assert req.headers["xi-api-key"] == "test-key"
    assert "idempotency-key" not in req.headers
    body = req.content
    assert b'scribe_v2' in body and b'diarize' in body and b'word' in body
    assert AUDIO_BYTES in body
    assert b"transcribe:guid-1" not in body  # call_ref 不出现在 HTTP 请求

    assert (workspace / scribe.ID_SIDECAR).read_text().strip() == TID
    assert (workspace / scribe.RAW_NAME).read_bytes() == raw_bytes  # 原始字节，不重排不包装
    arts = manifest.list_for(conn, ep_id)
    assert len(arts) == 1 and arts[0]["status"] == "done"
    assert arts[0]["sha256"] == sha256_file(workspace / scribe.RAW_NAME)
    row = ledger.get_by_ref(conn, call_ref_for(conn, ep_id))
    assert row["status"] == "success"
    assert row["amount_est"] == pytest.approx(30 / 3600 * scribe.USD_PER_HOUR)
    assert episode.get(conn, ep_id)["status"] == "transcribed"


def test_id_sidecar_written_before_raw(conn, audio, workspace, monkeypatch):
    ep_id = make_episode(conn, audio)
    observed = []
    original = scribe._create_only

    def spy(path, data):
        if path.name == scribe.RAW_NAME:
            observed.append((workspace / scribe.ID_SIDECAR).exists())
        return original(path, data)

    monkeypatch.setattr(scribe, "_create_only", spy)
    calls = []
    result = transcribe(conn, ep_id, audio, workspace, calls,
                        lambda req: json_response(ok_payload()))
    assert result.status == "success"
    assert observed == [True]  # 写 raw 时 ID sidecar 必须已存在


def test_duplicate_after_success_no_second_post(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    calls = []
    handler = lambda req: json_response(ok_payload())
    first = transcribe(conn, ep_id, audio, workspace, calls, handler)
    assert first.status == "success"
    second = transcribe(conn, ep_id, audio, workspace, calls, handler)
    assert second.status == "duplicate"
    assert len(calls) == 1
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "success"


def test_duplicate_with_existing_intent(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    calls = []
    result = transcribe(conn, ep_id, audio, workspace, calls,
                        lambda req: json_response(ok_payload()))
    assert result.status == "duplicate"
    assert calls == []
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "intent"


def test_unique_conflict_returns_duplicate(conn, audio, workspace, monkeypatch):
    """get_by_ref 检查与 INSERT 之间的并发竞态由 UNIQUE 约束兜底。"""
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    original = scribe.ledger.get_by_ref
    state = {"checked": False}

    def fake_get_by_ref(c, ref):
        if not state["checked"]:
            state["checked"] = True
            return None  # 模拟竞态：检查时还没看到，INSERT 时冲突
        return original(c, ref)

    monkeypatch.setattr(scribe.ledger, "get_by_ref", fake_get_by_ref)
    calls = []
    result = transcribe(conn, ep_id, audio, workspace, calls,
                        lambda req: json_response(ok_payload()))
    assert result.status == "duplicate"
    assert calls == []


def test_validation_failures_no_intent_no_http(conn, audio, workspace, monkeypatch):
    calls = []
    handler = lambda req: json_response(ok_payload())

    # Episode 状态不许可
    ep_id = make_episode(conn, audio, guid="g-status", status="discovered")
    with pytest.raises(scribe.ScribeError):
        transcribe(conn, ep_id, audio, workspace, calls, handler)

    # 音频 hash 与首版不一致
    ep_id = make_episode(conn, audio, guid="g-hash")
    audio.write_bytes(b"tampered")
    with pytest.raises(scribe.ScribeError):
        transcribe(conn, ep_id, audio, workspace, calls, handler)
    audio.write_bytes(AUDIO_BYTES)

    # 缺时长
    ep_id = make_episode(conn, audio, guid="g-nodur")
    conn.execute("UPDATE episode SET duration_secs = NULL WHERE id = ?", (ep_id,))
    conn.commit()
    with pytest.raises(scribe.ScribeError):
        transcribe(conn, ep_id, audio, workspace, calls, handler)

    # 缺 API key
    ep_id = make_episode(conn, audio, guid="g-nokey")
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    with pytest.raises(scribe.ScribeError):
        transcribe(conn, ep_id, audio, workspace, calls, handler)

    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_read_timeout_marks_unknown_and_never_reposts(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    calls = []

    def timeout_handler(req):
        raise httpx.ReadTimeout("mock", request=req)

    result = transcribe(conn, ep_id, audio, workspace, calls, timeout_handler)
    assert result.status == "unknown"
    assert len(calls) == 1
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "unknown"
    assert episode.get(conn, ep_id)["status"] == "downloaded"
    assert not (workspace / scribe.ID_SIDECAR).exists()
    assert not (workspace / scribe.RAW_NAME).exists()

    # unknown 后重复调用被 duplicate 闸门挡住，零重发
    again = transcribe(conn, ep_id, audio, workspace, calls,
                       lambda req: json_response(ok_payload()))
    assert again.status == "duplicate"
    assert len(calls) == 1


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError])
def test_connection_errors_mark_unknown(conn, audio, workspace, exc_type):
    ep_id = make_episode(conn, audio, guid=f"g-{exc_type.__name__}")
    calls = []

    def handler(req):
        raise exc_type("mock", request=req)

    result = transcribe(conn, ep_id, audio, workspace, calls, handler)
    assert result.status == "unknown"
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "unknown"


@pytest.mark.parametrize("status_code", [400, 422, 500])
def test_http_error_marks_failed(conn, audio, workspace, status_code):
    ep_id = make_episode(conn, audio, guid=f"g-{status_code}")
    calls = []
    result = transcribe(conn, ep_id, audio, workspace, calls,
                        lambda req: httpx.Response(status_code, text="error detail"))
    assert result.status == "failed"
    assert len(calls) == 1
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "failed"
    assert episode.get(conn, ep_id)["status"] == "failed"
    assert not (workspace / scribe.RAW_NAME).exists()


def test_200_invalid_json_marks_unknown(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    calls = []
    result = transcribe(conn, ep_id, audio, workspace, calls,
                        lambda req: httpx.Response(200, content=b"not json"))
    assert result.status == "unknown"
    assert not (workspace / scribe.RAW_NAME).exists()


def test_200_missing_id_marks_unknown(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    payload = ok_payload()
    del payload["transcription_id"]
    calls = []
    result = transcribe(conn, ep_id, audio, workspace, calls,
                        lambda req: json_response(payload))
    assert result.status == "unknown"
    assert not (workspace / scribe.ID_SIDECAR).exists()


def test_200_missing_words_keeps_id_for_recovery(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    calls = []
    result = transcribe(conn, ep_id, audio, workspace, calls,
                        lambda req: json_response(ok_payload(with_words=False)))
    assert result.status == "unknown"
    assert result.transcription_id == TID
    assert (workspace / scribe.ID_SIDECAR).read_text().strip() == TID
    assert not (workspace / scribe.RAW_NAME).exists()  # 规范 raw 留空供 GET 恢复
    assert (workspace / "scribe_raw.invalid.json").exists()  # 原始字节入诊断文件

    # 用同 ID GET 恢复，不再 POST
    recovered = fetch(conn, ep_id, workspace, calls, lambda req: json_response(ok_payload()))
    assert recovered.status == "success"
    assert len(calls) == 2  # 第一次 POST + 一次 GET
    assert calls[1].method == "GET"


def test_mark_unknown_after_crash(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)  # 模拟 POST 后崩溃，只留下 intent

    calls = []
    dup = transcribe(conn, ep_id, audio, workspace, calls,
                     lambda req: json_response(ok_payload()))
    assert dup.status == "duplicate" and calls == []

    result = scribe.mark_unknown(conn, episode_id=ep_id, workspace=workspace)
    assert result.status == "unknown"
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "unknown"

    # 幂等：再次 mark-unknown 不报错
    again = scribe.mark_unknown(conn, episode_id=ep_id, workspace=workspace)
    assert again.status == "unknown"


def test_mark_unknown_rejected_when_id_exists(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    workspace.mkdir(parents=True)
    (workspace / scribe.ID_SIDECAR).write_text(TID + "\n")
    with pytest.raises(scribe.ScribeError):
        scribe.mark_unknown(conn, episode_id=ep_id, workspace=workspace)


def test_fetch_recovers_with_single_get(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    workspace.mkdir(parents=True)
    (workspace / scribe.ID_SIDECAR).write_text(TID + "\n")

    calls = []
    result = fetch(conn, ep_id, workspace, calls, lambda req: json_response(ok_payload()))
    assert result.status == "success"
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert str(calls[0].url) == scribe.TRANSCRIPT_URL.format(transcription_id=TID)
    assert (workspace / scribe.RAW_NAME).exists()
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "success"
    assert episode.get(conn, ep_id)["status"] == "transcribed"


def test_fetch_local_raw_complete_no_http(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    workspace.mkdir(parents=True)
    raw_bytes = json.dumps(ok_payload()).encode()
    (workspace / scribe.ID_SIDECAR).write_text(TID + "\n")
    (workspace / scribe.RAW_NAME).write_bytes(raw_bytes)

    calls = []
    result = fetch(conn, ep_id, workspace, calls, lambda req: json_response(ok_payload()))
    assert result.status == "success"
    assert calls == []  # 本地完整，零 HTTP
    assert (workspace / scribe.RAW_NAME).read_bytes() == raw_bytes  # 不覆盖
    assert episode.get(conn, ep_id)["status"] == "transcribed"


def test_fetch_success_ledger_only_repairs_episode(conn, audio, workspace):
    """success 后 Episode 更新前崩溃：fetch 只补 Episode，不重写 outcome。"""
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    ledger.record_outcome(conn, call_ref_for(conn, ep_id), status="success")
    workspace.mkdir(parents=True)
    (workspace / scribe.ID_SIDECAR).write_text(TID + "\n")
    (workspace / scribe.RAW_NAME).write_bytes(json.dumps(ok_payload()).encode())

    calls = []
    result = fetch(conn, ep_id, workspace, calls, lambda req: json_response(ok_payload()))
    assert result.status == "success"
    assert calls == []
    assert episode.get(conn, ep_id)["status"] == "transcribed"


def test_fetch_unknown_reconciles_to_success(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    ledger.record_outcome(conn, call_ref_for(conn, ep_id), status="unknown")
    workspace.mkdir(parents=True)
    (workspace / scribe.ID_SIDECAR).write_text(TID + "\n")

    calls = []
    result = fetch(conn, ep_id, workspace, calls, lambda req: json_response(ok_payload()))
    assert result.status == "success"
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "success"
    assert episode.get(conn, ep_id)["status"] == "transcribed"


def test_fetch_failed_ledger_conflict(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    ledger.record_outcome(conn, call_ref_for(conn, ep_id), status="failed")
    episode.update_status(conn, ep_id, "failed")
    workspace.mkdir(parents=True)
    (workspace / scribe.ID_SIDECAR).write_text(TID + "\n")
    (workspace / scribe.RAW_NAME).write_bytes(json.dumps(ok_payload()).encode())

    calls = []
    result = fetch(conn, ep_id, workspace, calls, lambda req: json_response(ok_payload()))
    assert result.status == "failed"
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "failed"
    assert episode.get(conn, ep_id)["status"] == "failed"


def test_fetch_sidecar_mismatch_rejected(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    workspace.mkdir(parents=True)
    (workspace / scribe.ID_SIDECAR).write_text("other-id\n")
    calls = []
    with pytest.raises(scribe.ScribeError):
        fetch(conn, ep_id, workspace, calls, lambda req: json_response(ok_payload()))
    assert calls == []
    assert (workspace / scribe.ID_SIDECAR).read_text().strip() == "other-id"


def test_fetch_raw_without_sidecar_rejected(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    workspace.mkdir(parents=True)
    (workspace / scribe.RAW_NAME).write_bytes(json.dumps(ok_payload()).encode())
    with pytest.raises(scribe.ScribeError):
        fetch(conn, ep_id, workspace, [], lambda req: json_response(ok_payload()))


def test_fetch_without_ledger_rejected(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    with pytest.raises(scribe.ScribeError):
        fetch(conn, ep_id, workspace, [], lambda req: json_response(ok_payload()))


def test_fetch_get_error_keeps_original_outcome(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    workspace.mkdir(parents=True)
    (workspace / scribe.ID_SIDECAR).write_text(TID + "\n")

    calls = []
    result = fetch(conn, ep_id, workspace, calls, lambda req: httpx.Response(404, text="nf"))
    assert result.status == "failed"
    assert ledger.get_by_ref(conn, call_ref_for(conn, ep_id))["status"] == "intent"
    assert episode.get(conn, ep_id)["status"] == "downloaded"
    assert not (workspace / scribe.RAW_NAME).exists()

    # GET 免费可安全重试：第二次成功
    recovered = fetch(conn, ep_id, workspace, calls, lambda req: json_response(ok_payload()))
    assert recovered.status == "success"
    assert len(calls) == 2


def test_dotenv_narrow_format(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nexport FOO=bar\nQUOTED=\"a b\"\nSINGLE='c d'\n", encoding="utf-8"
    )
    values = scribe._parse_dotenv(env_file)
    assert values == {"FOO": "bar", "QUOTED": "a b", "SINGLE": "c d"}

    bad = tmp_path / "bad.env"
    bad.write_text("not a valid line\n", encoding="utf-8")
    with pytest.raises(scribe.ScribeError):
        scribe._parse_dotenv(bad)


def test_dotenv_explicit_env_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ELEVENLABS_API_KEY=from-file\nOTHER_KEY=other\n", encoding="utf-8")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "explicit")
    monkeypatch.delenv("OTHER_KEY", raising=False)
    scribe._load_dotenv(path=env_file)
    assert scribe.os.environ["ELEVENLABS_API_KEY"] == "explicit"
    assert scribe.os.environ["OTHER_KEY"] == "other"


def test_read_timeout_formula():
    assert scribe.read_timeout_for(30) == 120.0
    assert scribe.read_timeout_for(60) == 180.0
    assert scribe.read_timeout_for(2940) == 8820.0
    with pytest.raises(scribe.ScribeError):
        scribe.read_timeout_for(0)
    with pytest.raises(scribe.ScribeError):
        scribe.read_timeout_for(float("nan"))


def test_rebill_from_unknown_creates_separate_success_attempt(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    seed_intent(conn, ep_id)
    base_ref = call_ref_for(conn, ep_id)
    ledger.record_outcome(conn, base_ref, status="unknown")
    calls = []
    result = scribe.transcribe(
        conn, episode_id=ep_id, audio_path=audio, workspace=workspace,
        transport=mock_transport(calls, lambda req: json_response(ok_payload("tid-rebill"))),
        trust_env=False, rebill_attempt=2,
    )
    assert result.status == "success"
    ep = episode.get(conn, ep_id)
    rebill_ref = scribe.build_call_ref(ep["guid"], ep["audio_sha256"], 2)
    assert result.call_ref == rebill_ref
    assert ledger.get_by_ref(conn, base_ref)["status"] == "unknown"
    assert ledger.get_by_ref(conn, rebill_ref)["status"] == "success"
    assert len(calls) == 1
    repeated = scribe.transcribe(
        conn, episode_id=ep_id, audio_path=audio, workspace=workspace,
        transport=mock_transport(calls, lambda req: json_response(ok_payload("tid-rebill"))),
        trust_env=False, rebill_attempt=2,
    )
    assert repeated.status == "duplicate"
    assert len(calls) == 1


def test_rebill_requires_existing_unknown(conn, audio, workspace):
    ep_id = make_episode(conn, audio)
    calls = []
    with pytest.raises(scribe.ScribeError, match="要求原转写账本为 unknown"):
        scribe.transcribe(
            conn, episode_id=ep_id, audio_path=audio, workspace=workspace,
            transport=mock_transport(calls, lambda req: json_response(ok_payload())),
            trust_env=False, rebill_attempt=2,
        )
    assert calls == []


def test_cli_json_and_exit_code(tmp_path, monkeypatch, capsys):
    fake_result = scribe.ScribeResult(status="success", call_ref="r", episode_id=1)

    def fake_transcribe(conn, **kwargs):
        return fake_result

    monkeypatch.setattr(scribe, "transcribe", fake_transcribe)
    code = scribe.main(["transcribe", "1", "a.mp3", "--db", str(tmp_path / "cli.db")])
    assert code == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["status"] == "success"
    assert payload["episode_id"] == 1

    def raise_error(conn, **kwargs):
        raise scribe.ScribeError("boom")

    monkeypatch.setattr(scribe, "transcribe", raise_error)
    code = scribe.main(["transcribe", "1", "a.mp3", "--db", str(tmp_path / "cli.db")])
    assert code == 1
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line)["status"] == "failed"
