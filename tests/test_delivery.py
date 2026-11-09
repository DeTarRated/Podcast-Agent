"""R2 适配、发布与私人播客库测试；所有对象存储均为 fake，零真实 R2。

模拟场景：
- 完整上传与当前发布、节目库查询、动态签名与双语字幕
- 缺少看点时返回 Runner dispatch 描述，补齐后继续交付
- 部分上传失败不 delivered、不改 current，重试复用同一 delivery
- 发布后修订上传失败旧版仍可查，新版成功后原子切换
- 初次交付被新修订取代后标记 superseded，不上线
- R2 HEAD 同 hash 跳过、不同 hash 拒绝覆盖、put 后校验与签名参数
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from delivery import library, publish
from delivery.errors import DeliveryError
from delivery.r2 import R2Settings, R2Store
from pipeline.download import sha256_file
from review import service
from state import db, delivery_state, episode, manifest

START = "2026-11-01T12:00:00+00:00"
LATER = "2026-11-01T13:00:00+00:00"
SOURCE = """1
00:00:01,000 --> 00:00:03,000
Hello world

2
00:00:04,000 --> 00:00:06,000
Next sentence
"""
REVIEW = """1
00:00:01,000 --> 00:00:03,000
旧译文

2
00:00:04,000 --> 00:00:06,000
下一句
"""
HIGHLIGHTS = {"summary": "本期摘要", "items": [{"text": "看点", "timestampMs": 1000}]}


class FakeStore:
    def __init__(self):
        self.objects = {}
        self.fail_contains = None
        self.settings = SimpleNamespace(expires_seconds=900)

    def ensure_bytes(self, key, payload, content_type):
        if self.fail_contains and self.fail_contains in key:
            raise DeliveryError("r2_error", "simulated upload failure")
        self.objects[key] = bytes(payload)
        return True

    def ensure_file(self, key, path, content_type):
        return self.ensure_bytes(key, Path(path).read_bytes(), content_type)

    def sign_get(self, key, *, download_name=None):
        suffix = f"?download={download_name}" if download_name else ""
        return f"signed://{key}{suffix}"


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "delivery.db")
    db.init_schema(connection)
    yield connection
    connection.close()


def _artifact(conn, episode_id, step, path, kind="srt"):
    artifact_id = manifest.add(
        conn, episode_id=episode_id, step=step, kind=kind, path=str(path), sha256=sha256_file(path)
    )
    manifest.finish(conn, artifact_id, sha256=sha256_file(path))
    return artifact_id


def _seed_approved(conn, tmp_path, *, with_highlights=True):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = workspace / "en_fix.srt"
    review = workspace / "en_zh_fix.srt"
    split = workspace / "en_zh_split.srt"
    audio = workspace / "audio.mp3"
    source.write_text(SOURCE, encoding="utf-8")
    review.write_text(REVIEW, encoding="utf-8")
    split.write_text(REVIEW, encoding="utf-8")
    audio.write_bytes(b"audio-fixture")
    episode_id = episode.create(
        conn, guid="guid-1", feed="feed", title="Episode title", published_at=START, duration_secs=6.0
    )
    episode.set_audio(conn, episode_id, sha256=sha256_file(audio), duration_secs=6.0)
    source_id = _artifact(conn, episode_id, "transcript_fix", source)
    review_id = _artifact(conn, episode_id, "review", review)
    _artifact(conn, episode_id, "split", split)
    _artifact(conn, episode_id, "download", audio, kind="audio")
    revision = service.create_review_revision(
        conn, episode_id=episode_id, base_artifact_id=review_id, variant="review", now=START
    )
    approval = service.approve(
        conn,
        episode_id=episode_id,
        base_revision_id=revision["revision_id"],
        method="manual",
        request_id="approve-delivery",
        now=LATER,
    )
    artifact = conn.execute("SELECT * FROM artifact WHERE id=?", (approval["artifact_id"],)).fetchone()
    if with_highlights:
        _write_highlights(artifact)
    return {
        "episode_id": episode_id,
        "workspace": workspace,
        "source_id": source_id,
        "review_id": review_id,
        "revision_id": revision["revision_id"],
        "approval_id": approval["approval_id"],
        "artifact_id": approval["artifact_id"],
    }


def _write_highlights(artifact, value=None):
    json_path, _ = publish.highlights_paths(artifact)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(value or HIGHLIGHTS), encoding="utf-8")
    return json_path


def test_full_publish_library_and_signing(conn, tmp_path):
    seeded = _seed_approved(conn, tmp_path)
    store = FakeStore()
    result = publish.publish_approved(conn, now=LATER, store=store)
    assert result["published"] == 1
    assert len(store.objects) == 3
    assert episode.get(conn, seeded["episode_id"])["status"] == "delivered"
    current = delivery_state.current_for(conn, seeded["episode_id"])
    assert current["is_current"] == 1
    assert current["highlights_json"]
    listed = library.list_episodes(conn)
    assert listed["count"] == 1
    assert listed["items"][0]["summary"] == "本期摘要"
    detail = library.episode_detail(conn, episode_id=seeded["episode_id"], store=store)
    assert detail["audio_url"].startswith("signed://episodes/guid-1/audio-")
    assert detail["subtitle_url"].endswith("?download=episode-1-zh.srt")
    assert detail["cues"][0]["source_text"] == "Hello world"
    assert detail["highlights"]["items"][0]["timestampMs"] == 1000
    assert publish.publish_approved(conn, now=LATER, store=store)["published"] == 0


def test_missing_highlights_returns_dispatch_then_resumes(conn, tmp_path):
    seeded = _seed_approved(conn, tmp_path, with_highlights=False)
    store = FakeStore()
    waiting = publish.publish_approved(conn, now=LATER, store=store)
    assert waiting["waiting"] == 1
    item = waiting["items"][0]
    dispatch = json.loads(Path(item["dispatchFile"]).read_text(encoding="utf-8"))["dispatch"]
    assert dispatch["role"] == "highlights"
    assert Path(dispatch["inputPath"]).is_absolute()
    Path(dispatch["outputPath"]).write_text(json.dumps(HIGHLIGHTS), encoding="utf-8")
    completed = publish.publish_approved(conn, now=LATER, store=store)
    assert completed["published"] == 1


def test_partial_upload_failure_retries_same_delivery(conn, tmp_path):
    seeded = _seed_approved(conn, tmp_path)
    store = FakeStore()
    store.fail_contains = "subtitles/"
    failed = publish.publish_approved(conn, now=LATER, store=store)
    assert failed["failed"] == 1
    assert episode.get(conn, seeded["episode_id"])["status"] == "approved"
    delivery = delivery_state.current_for(conn, seeded["episode_id"])
    assert delivery is None
    row = conn.execute("SELECT * FROM delivery WHERE approval_id=?", (seeded["approval_id"],)).fetchone()
    assert row["status"] == "failed"
    store.fail_contains = None
    completed = publish.publish_approved(conn, now=LATER, store=store)
    assert completed["published"] == 1
    assert completed["items"][0]["delivery_id"] == row["id"]


def test_revision_failure_keeps_old_library_until_new_publish(conn, tmp_path):
    seeded = _seed_approved(conn, tmp_path)
    store = FakeStore()
    assert publish.publish_approved(conn, now=LATER, store=store)["published"] == 1
    old_delivery = delivery_state.current_for(conn, seeded["episode_id"])
    revised = service.revise_published(conn, episode_id=seeded["episode_id"], now=LATER)
    saved = service.save_override(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=revised["revision_id"],
        base_entry_number=1,
        source_artifact_id=seeded["source_id"],
        start_ms=1000,
        end_ms=3000,
        source_text="Hello world",
        after_text="修订译文",
        request_id="revision-edit",
        now=LATER,
    )
    service.approve(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=saved["revision_id"],
        method="manual",
        request_id="revision-approve",
        now=LATER,
    )
    artifact = conn.execute("SELECT * FROM artifact WHERE id=?", (saved["rendered_artifact_id"],)).fetchone()
    _write_highlights(artifact)
    store.fail_contains = "subtitles/"
    assert publish.publish_approved(conn, now=LATER, store=store)["failed"] == 1
    assert delivery_state.current_for(conn, seeded["episode_id"])["id"] == old_delivery["id"]
    assert library.episode_detail(conn, episode_id=seeded["episode_id"], store=store)["cues"][0]["text"] == "旧译文"
    store.fail_contains = None
    assert publish.publish_approved(conn, now=LATER, store=store)["published"] == 1
    assert delivery_state.current_for(conn, seeded["episode_id"])["id"] != old_delivery["id"]
    assert library.episode_detail(conn, episode_id=seeded["episode_id"], store=store)["cues"][0]["text"] == "修订译文"


def test_initial_delivery_superseded_before_upload_cannot_publish(conn, tmp_path):
    seeded = _seed_approved(conn, tmp_path)
    approval = conn.execute("SELECT * FROM approval WHERE id=?", (seeded["approval_id"],)).fetchone()
    pending = publish._ensure_delivery(conn, approval)
    service.revise_published(conn, episode_id=seeded["episode_id"], now=LATER)
    result = publish.publish_approved(conn, now=LATER, store=FakeStore())
    assert result["published"] == 0
    row = conn.execute("SELECT * FROM delivery WHERE id=?", (pending["id"],)).fetchone()
    assert row["status"] == "superseded"
    assert row["is_current"] == 0


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.signed = []

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        size, digest, content = self.objects[Key]
        return {"ContentLength": size, "Metadata": {"sha256": digest}}

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata):
        payload = Body.read()
        self.objects[Key] = (len(payload), Metadata["sha256"], payload)
        self.puts.append((Key, payload, ContentType, Metadata))

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.signed.append((operation, Params, ExpiresIn))
        return f"signed://{Params['Key']}"


def test_r2_adapter_upload_head_skip_conflict_and_signing():
    client = FakeS3Client()
    store = R2Store(
        R2Settings("https://r2.example", "bucket", "id", "secret", expires_seconds=123),
        client=client,
    )
    payload = b"subtitle"
    assert store.ensure_bytes("episode/zh.srt", payload, "application/x-subrip") is True
    assert store.ensure_bytes("episode/zh.srt", payload, "application/x-subrip") is False
    assert len(client.puts) == 1
    with pytest.raises(DeliveryError) as error:
        store.ensure_bytes("episode/zh.srt", b"different", "application/x-subrip")
    assert error.value.code == "object_conflict"
    assert store.sign_get("episode/zh.srt", download_name="zh.srt") == "signed://episode/zh.srt"
    operation, params, expires = client.signed[0]
    assert operation == "get_object"
    assert expires == 123
    assert params["ResponseContentDisposition"] == 'attachment; filename="zh.srt"'
