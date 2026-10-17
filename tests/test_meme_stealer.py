"""表情包小偷测试（移植自 astrbot_plugin_stealer）。

覆盖：偷取候选提取（商城表情/mface/表情图片段 vs 普通图片）、门控
（概率 0/1、冷却窗口）、SHA-256 去重与处理中锁、VLM 分类解析
（JSON 围栏/前后缀/拒绝/词表外情绪兜底）、入库与 sidecar 元数据、
容量淘汰、被拒哈希缓存、StickerManager sidecar 语义召回。全部离线：
下载与 VLM 调用均打桩。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
from types import SimpleNamespace
from typing import Any

from atri_qq_bot.meme_stealer import (
    MemeStealService,
    StealCandidate,
    extract_steal_candidates,
)
from atri_qq_bot.meme_stealer.classify import (
    CLASSIFY_EMOTIONS,
    Classification,
    VisionSettings,
    parse_classification,
)
from atri_qq_bot.stickers import StickerManager

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF_BYTES = b"GIF89a" + b"\x00" * 64


def _service(tmp_path, **overrides: Any) -> MemeStealService:
    config = dict(
        meme_steal_enabled=True,
        meme_steal_mode="cooldown",
        meme_steal_chance=0.5,
        meme_steal_cooldown_seconds=20,
        meme_steal_max_bytes=3_000_000,
        meme_steal_library_max=300,
        meme_steal_queue_size=8,
        sticker_dir=tmp_path / "stickers",
        toolbox_vision_enabled=False,
        toolbox_vision_model="",
        toolbox_vision_base_url="",
        toolbox_vision_api_key=None,
        toolbox_vision_timeout_seconds=55.0,
        toolbox_vision_max_bytes=8_000_000,
        openai_model="",
        openai_base_url="",
        openai_api_key=None,
    )
    config.update(overrides)
    return MemeStealService(SimpleNamespace(**config))


def _event(*segments: dict) -> dict:
    return {"message": list(segments), "group_id": 20001, "user_id": 10001}


def _image_segment(url: str, **extra: Any) -> dict:
    return {"type": "image", "data": {"url": url, **extra}}


def _patch_pipeline(
    monkeypatch, service: MemeStealService, content: bytes, classification: Classification
) -> None:
    async def fake_download(url: str) -> bytes | None:
        return content

    async def fake_classify(data: bytes) -> Classification:
        return classification

    monkeypatch.setattr(service, "_download", fake_download)
    monkeypatch.setattr(service, "_classify", fake_classify)


# ── 候选提取 ──


def test_extract_candidates_only_sticker_segments() -> None:
    event = _event(
        _image_segment("https://cdn.example.com/photo.jpg", sub_type="0"),
        _image_segment("https://cdn.example.com/sticker.gif", sub_type="1"),
        {"type": "mface", "data": {"url": "https://cdn.example.com/market.png"}},
        {"type": "marketface", "data": {"url": "https://cdn.example.com/market2.png"}},
        _image_segment("https://cdn.example.com/emoji.jpg", emoji_id="555"),
        {"type": "text", "data": {"text": "看这个"}},
    )
    urls = [c.url for c in extract_steal_candidates(event, "group:20001", "看这个")]
    assert urls == [
        "https://cdn.example.com/sticker.gif",
        "https://cdn.example.com/market.png",
        "https://cdn.example.com/market2.png",
        "https://cdn.example.com/emoji.jpg",
    ]


def test_extract_candidates_mface_alternate_url_fields() -> None:
    # NapCat 不同版本可能只给 cdnurl / cdn_url / raw_url 中的某一个。
    event = _event(
        {"type": "mface", "data": {"cdnurl": "https://cdn.example.com/a.gif"}},
        {"type": "mface", "data": {"cdn_url": "https://cdn.example.com/b.gif"}},
        {"type": "mface", "data": {"raw_url": "https://cdn.example.com/c.gif"}},
        # 认识的字段全没有时，兜底扫描任意字符串值里的 URL。
        {"type": "mface", "data": {"some_field": "https://cdn.example.com/d.gif"}},
        # 非 http 开头的值不算。
        {"type": "mface", "data": {"cdnurl": "base64://not-a-url"}},
    )
    urls = [c.url for c in extract_steal_candidates(event, "group:20001", "")]
    assert urls == [
        "https://cdn.example.com/a.gif",
        "https://cdn.example.com/b.gif",
        "https://cdn.example.com/c.gif",
        "https://cdn.example.com/d.gif",
    ]


def test_extract_candidates_summary_marks_sticker() -> None:
    # 个别 NapCat 版本收藏表情 sub_type 不为 1，只靠 summary「[动画表情]」标记。
    event = _event(
        _image_segment("https://cdn.example.com/anim.png", sub_type="0", summary="[动画表情]")
    )
    urls = [c.url for c in extract_steal_candidates(event, "group:20001", "")]
    assert urls == ["https://cdn.example.com/anim.png"]


def test_extract_candidates_plain_images_only_when_enabled() -> None:
    event = _event(
        _image_segment("https://cdn.example.com/meme.jpg", sub_type="0", summary=""),
        _image_segment("https://cdn.example.com/sticker.gif", sub_type="1"),
    )
    # 默认（未开投机）只偷确认表情包。
    assert [c.url for c in extract_steal_candidates(event, "group:20001", "")] == [
        "https://cdn.example.com/sticker.gif"
    ]
    # 开启后相册直发图也可偷，且确认表情包排在投机候选之前。
    candidates = extract_steal_candidates(event, "group:20001", "", plain_images_enabled=True)
    assert [c.url for c in candidates] == [
        "https://cdn.example.com/sticker.gif",
        "https://cdn.example.com/meme.jpg",
    ]
    # 投机候选带标记：门控据此走独立的投机冷却窗口。
    assert [c.speculative for c in candidates] == [False, True]


def test_service_reads_plain_images_config(tmp_path) -> None:
    service = _service(tmp_path, meme_steal_plain_images=False)
    assert service.steal_plain_images is False
    service2 = _service(tmp_path)  # 默认 True
    assert service2.steal_plain_images is True


def test_mixin_skips_private_messages(tmp_path) -> None:
    from atri_qq_bot.onebot.server import OneBotServer

    server = OneBotServer.__new__(OneBotServer)
    server.config = SimpleNamespace(meme_steal_enabled=True)
    service = _service(tmp_path)
    server._meme_steal_service = service
    calls: list[Any] = []

    def fake_steal(event: Any, context_text: Any = "", conversation_id: Any = "") -> bool:
        calls.append(conversation_id)
        return True

    service.maybe_steal_from_event = fake_steal  # type: ignore[method-assign]
    private_event = {
        "message": [{"type": "mface", "data": {"url": "https://x.com/a.gif"}}],
        "message_type": "private",
        "user_id": 10001,
    }
    group_event = {
        "message": [{"type": "mface", "data": {"url": "https://x.com/a.gif"}}],
        "message_type": "group",
        "group_id": 20001,
        "user_id": 10001,
    }
    server._maybe_steal_stickers(private_event, "")
    assert calls == []
    server._maybe_steal_stickers(group_event, "")
    assert calls == ["20001"]


def test_extract_candidates_ignores_invalid_messages() -> None:
    assert extract_steal_candidates({}) == []
    assert extract_steal_candidates({"message": "不是列表"}) == []


# ── 门控 ──


def test_gate_probability_zero_and_full(tmp_path) -> None:
    off = _service(tmp_path, meme_steal_mode="probability", meme_steal_chance=0.0)
    event = _event(_image_segment("https://x.com/a.gif", sub_type="1"))
    assert off.maybe_steal_from_event(event) is False

    on = _service(tmp_path, meme_steal_mode="probability", meme_steal_chance=1.0)
    assert on.maybe_steal_from_event(event) is True
    assert on._queue.qsize() == 1


def test_gate_disabled_service_returns_false(tmp_path) -> None:
    service = _service(tmp_path, meme_steal_enabled=False)
    event = _event(_image_segment("https://x.com/a.gif", sub_type="1"))
    assert service.maybe_steal_from_event(event) is False


def test_gate_consumed_only_by_confirmed_stickers(tmp_path) -> None:
    # 冷却窗口内普通图片不消耗冷却；真实表情包仍能立即偷取。
    service = _service(tmp_path, meme_steal_mode="cooldown", meme_steal_cooldown_seconds=60)
    service._last_steal_at = 0.0
    plain = _event(_image_segment("https://x.com/photo.jpg", sub_type="0"))
    assert service.maybe_steal_from_event(plain) is False
    assert service._last_steal_at == 0.0

    sticker = _event(_image_segment("https://x.com/s.gif", sub_type="1"))
    assert service.maybe_steal_from_event(sticker) is True
    assert service._last_steal_at == 0.0  # 冷却在 worker 开始处理时才消耗

    service._consume_cooldown()
    assert service._last_steal_at > 0
    again = _event(_image_segment("https://x.com/s2.gif", sub_type="1"))
    assert service.maybe_steal_from_event(again) is False


def test_speculative_and_confirmed_cooldowns_independent(tmp_path) -> None:
    # 投机冷却独立于确认冷却：照片流消耗投机窗口，不挡真表情包。
    service = _service(tmp_path, meme_steal_mode="cooldown", meme_steal_cooldown_seconds=60)
    service._last_steal_at = 0.0
    service._last_speculative_steal_at = 0.0  # 模拟投机窗口已开放

    plain = _event(_image_segment("https://x.com/meme.jpg", sub_type="0"))
    assert service.maybe_steal_from_event(plain) is True
    assert service._last_steal_at == 0.0  # 投机候选不消耗确认冷却

    service._consume_cooldown(speculative=True)
    assert service._last_speculative_steal_at > 0
    # 投机窗口活跃：再遇普通图片不偷。
    plain2 = _event(_image_segment("https://x.com/meme2.jpg", sub_type="0"))
    assert service.maybe_steal_from_event(plain2) is False
    # 但确认表情包不受投机冷却影响。
    sticker = _event(_image_segment("https://x.com/s.gif", sub_type="1"))
    assert service.maybe_steal_from_event(sticker) is True


def test_speculative_batch_capped_at_one_per_event(tmp_path) -> None:
    # 同一消息刷一排相册图只抽一张进 VLM，照片流不冲垮队列。
    service = _service(tmp_path, meme_steal_mode="cooldown", meme_steal_cooldown_seconds=60)
    service._last_speculative_steal_at = 0.0
    event = _event(
        _image_segment("https://x.com/p1.jpg", sub_type="0"),
        _image_segment("https://x.com/p2.jpg", sub_type="0"),
        _image_segment("https://x.com/p3.jpg", sub_type="0"),
    )
    assert service.maybe_steal_from_event(event) is True
    assert service._queue.qsize() == 1
    assert service._queue._queue[0].url == "https://x.com/p1.jpg"


def test_speculative_cooldown_starts_at_boot(tmp_path) -> None:
    # 服务刚启动（首个冷却窗口内）不偷投机图片：先观察流量。
    service = _service(tmp_path, meme_steal_mode="cooldown", meme_steal_cooldown_seconds=3600)
    plain = _event(_image_segment("https://x.com/fresh.jpg", sub_type="0"))
    assert service.maybe_steal_from_event(plain) is False
    # 确认表情包不受启动观察期影响。
    sticker = _event(_image_segment("https://x.com/s.gif", sub_type="1"))
    assert service.maybe_steal_from_event(sticker) is True


def test_queue_overflow_drops_candidates(tmp_path) -> None:
    service = _service(
        tmp_path, meme_steal_mode="probability", meme_steal_chance=1.0, meme_steal_queue_size=1
    )
    first = _event(_image_segment("https://x.com/a.gif", sub_type="1"))
    second = _event(_image_segment("https://x.com/b.gif", sub_type="1"))
    assert service.maybe_steal_from_event(first) is True
    assert service.maybe_steal_from_event(second) is False  # 队列满即丢弃
    assert service._queue.qsize() == 1


def test_invalid_mode_falls_back_to_cooldown(tmp_path) -> None:
    service = _service(tmp_path, meme_steal_mode="概率刷屏模式")
    assert service.mode == "cooldown"


# ── 分类解析 ──


def test_parse_classification_full_json() -> None:
    response = json.dumps(
        {
            "approved": True,
            "category": "happy",
            "tags": ["猫咪", "开心大笑", "猫咪"],
            "desc": "一只猫咧嘴大笑",
            "scenes": ["被夸奖时", "开心到飞起"],
            "overlay_text": "哈哈",
        },
        ensure_ascii=False,
    )
    result = parse_classification(response)
    assert result.approved and not result.rejected
    assert result.emotion == "happy"
    assert result.tags == ("猫咪", "开心大笑")  # 去重
    assert result.desc == "一只猫咧嘴大笑"
    assert result.scenes == ("被夸奖时", "开心到飞起")
    assert result.overlay_text == "哈哈"


def test_parse_classification_fenced_and_wrapped() -> None:
    response = "分析如下：\n```json\n{\"approved\": true, \"category\": \"pout\", \"tags\": [\"傲娇\"]}\n```\n完毕"
    result = parse_classification(response)
    assert result.approved is True
    assert result.emotion == "pout"


def test_parse_classification_rejected() -> None:
    assert parse_classification('{"approved": false}').rejected is True
    assert parse_classification('{"approved": "no"}').rejected is True


def test_parse_classification_unknown_emotion_falls_back() -> None:
    result = parse_classification('{"approved": true, "category": "excited", "tags": []}')
    # excited 不在词表也不在别名表 → 兜底 teasing 桶。
    assert result.emotion == "teasing"


def test_parse_classification_alias_emotion() -> None:
    result = parse_classification('{"approved": true, "category": "Tease", "tags": []}')
    assert result.emotion == "teasing"


def test_parse_classification_garbage_is_not_approved() -> None:
    result = parse_classification("这不是 JSON，模型跑偏了")
    assert result.approved is False
    assert result.rejected is False
    assert result.emotion == "teasing"


def test_classify_emotions_exclude_non_sendable() -> None:
    assert "neutral" not in CLASSIFY_EMOTIONS
    assert "unsorted" not in CLASSIFY_EMOTIONS
    assert "happy" in CLASSIFY_EMOTIONS


# ── 处理链路（下载与 VLM 打桩） ──


def test_process_candidate_stores_with_sidecar(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    classification = Classification(
        approved=True,
        emotion="teasing",
        tags=("抽象", "猫咪捂脸"),
        desc="一只猫捂脸表示没眼看",
        scenes=("群里整活时",),
        overlay_text="就这",
    )
    _patch_pipeline(monkeypatch, service, GIF_BYTES, classification)

    candidate = StealCandidate(
        url="https://cdn.example.com/sticker", conversation_id="group:20001", context_text=""
    )
    assert asyncio.run(service.process_candidate(candidate)) is True

    files = list((tmp_path / "stickers" / "teasing").glob("steal_*.gif"))
    assert len(files) == 1
    sidecar = files[0].with_suffix(".gif.json")
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["source"] == "meme_steal"
    assert data["emotion"] == "teasing"
    assert data["tags"] == ["抽象", "猫咪捂脸"]
    assert data["hash"] == hashlib.sha256(GIF_BYTES).hexdigest()
    assert data["origin_conversation"] == "group:20001"
    assert files[0].read_bytes() == GIF_BYTES


def test_process_candidate_dedup_by_sha256(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    classification = Classification(approved=True, emotion="happy")
    _patch_pipeline(monkeypatch, service, PNG_BYTES, classification)

    url = "https://cdn.example.com/same.png"
    first = asyncio.run(
        service.process_candidate(StealCandidate(url=url, conversation_id="", context_text=""))
    )
    second = asyncio.run(
        service.process_candidate(StealCandidate(url=url, conversation_id="", context_text=""))
    )
    assert first is True
    assert second is False  # 同 SHA-256 已入库
    images = list((tmp_path / "stickers" / "happy").glob("steal_*.png"))
    assert len(images) == 1


def test_process_candidate_rejected_remembered(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    _patch_pipeline(monkeypatch, service, PNG_BYTES, Classification(rejected=True))

    url = "https://cdn.example.com/photo.png"
    assert asyncio.run(
        service.process_candidate(StealCandidate(url=url, conversation_id="", context_text=""))
    ) is False
    # 被拒哈希进入缓存：再遇到不重新下载分类。
    assert asyncio.run(
        service.process_candidate(StealCandidate(url=url, conversation_id="", context_text=""))
    ) is False
    assert hashlib.sha256(PNG_BYTES).hexdigest() in service._rejected_hashes
    assert not list((tmp_path / "stickers").rglob("steal_*")) if (tmp_path / "stickers").exists() else True


def test_process_candidate_vlm_unavailable_falls_back_to_context(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    _patch_pipeline(monkeypatch, service, PNG_BYTES, Classification())

    candidate = StealCandidate(
        url="https://cdn.example.com/s.gif", conversation_id="", context_text="哈哈哈笑死"
    )
    assert asyncio.run(service.process_candidate(candidate)) is True
    images = list((tmp_path / "stickers" / "happy").glob("steal_*.png"))
    assert len(images) == 1
    data = json.loads(images[0].with_suffix(".png.json").read_text(encoding="utf-8"))
    assert data["tags"] == []
    assert data["emotion"] == "happy"


def test_speculative_candidate_requires_vlm_approval(tmp_path, monkeypatch) -> None:
    # 投机候选（相册直发普通图）VLM 不可用/失败时绝不兜底入库：
    # 普通照片不能混进表情库。
    service = _service(tmp_path)
    _patch_pipeline(monkeypatch, service, PNG_BYTES, Classification())

    candidate = StealCandidate(
        url="https://cdn.example.com/photo.jpg",
        conversation_id="",
        context_text="哈哈哈笑死",
        speculative=True,
    )
    assert asyncio.run(service.process_candidate(candidate)) is False
    assert not (tmp_path / "stickers").exists() or not list(
        (tmp_path / "stickers").rglob("steal_*")
    )
    # VLM 失败不是图片的错：不缓存拒绝哈希，VLM 恢复后同图可重判。
    assert service._rejected_hashes is not None
    assert len(service._rejected_hashes) == 0


def test_speculative_candidate_rejected_by_vlm(tmp_path, monkeypatch) -> None:
    # VLM 明确判定非表情包：丢弃并缓存哈希，同图不再重判。
    service = _service(tmp_path)
    _patch_pipeline(monkeypatch, service, PNG_BYTES, Classification(rejected=True))

    candidate = StealCandidate(
        url="https://cdn.example.com/photo.jpg", conversation_id="", context_text="", speculative=True
    )
    assert asyncio.run(service.process_candidate(candidate)) is False
    assert hashlib.sha256(PNG_BYTES).hexdigest() in service._rejected_hashes


def test_speculative_candidate_stores_when_vlm_approves(tmp_path, monkeypatch) -> None:
    # VLM 批准的投机候选（相册直发梗图）正常入库。
    service = _service(tmp_path)
    classification = Classification(approved=True, emotion="teasing", tags=("梗图",))
    _patch_pipeline(monkeypatch, service, PNG_BYTES, classification)

    candidate = StealCandidate(
        url="https://cdn.example.com/meme.jpg", conversation_id="", context_text="", speculative=True
    )
    assert asyncio.run(service.process_candidate(candidate)) is True
    images = list((tmp_path / "stickers" / "teasing").glob("steal_*.png"))
    assert len(images) == 1


def _jpeg_bytes(camera_exif: bool) -> bytes:
    import io

    from PIL import Image

    image = Image.new("RGB", (64, 64), (255, 0, 0))
    exif = image.getexif()
    if camera_exif:
        exif[271] = "TestCam"
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_looks_like_photo_camera_exif_rules() -> None:
    # 带相机制造商 EXIF 的 JPEG 判实拍照片；干净 JPEG、编辑器空 DateTime、PNG 放行。
    from atri_qq_bot.meme_stealer.classify import looks_like_photo

    assert looks_like_photo(_jpeg_bytes(camera_exif=True)) is not None
    assert looks_like_photo(_jpeg_bytes(camera_exif=False)) is None
    assert looks_like_photo(PNG_BYTES) is None
    assert looks_like_photo(b"") is None


def test_speculative_candidate_rejected_by_photo_signature(tmp_path, monkeypatch) -> None:
    # 投机候选命中确定性照片特征（相机 EXIF）：VLM 调用前直接丢弃并缓存拒绝哈希。
    service = _service(tmp_path)
    photo = _jpeg_bytes(camera_exif=True)
    _patch_pipeline(
        monkeypatch, service, photo, Classification(approved=True, emotion="teasing")
    )

    candidate = StealCandidate(
        url="https://cdn.example.com/photo.jpg", conversation_id="", context_text="", speculative=True
    )
    assert asyncio.run(service.process_candidate(candidate)) is False
    assert hashlib.sha256(photo).hexdigest() in service._rejected_hashes
    assert not (tmp_path / "stickers").exists() or not list(
        (tmp_path / "stickers").rglob("steal_*")
    )


def test_confirmed_candidate_skips_photo_signature(tmp_path, monkeypatch) -> None:
    # 确认表情包（平台背书）不做照片预审：带 EXIF 也照常走分类入库。
    service = _service(tmp_path)
    photo = _jpeg_bytes(camera_exif=True)
    _patch_pipeline(
        monkeypatch, service, photo, Classification(approved=True, emotion="happy")
    )

    candidate = StealCandidate(
        url="https://cdn.example.com/emoji.jpg", conversation_id="", context_text=""
    )
    assert asyncio.run(service.process_candidate(candidate)) is True
    images = list((tmp_path / "stickers" / "happy").glob("steal_*.jpg"))
    assert len(images) == 1


def test_download_failure_returns_false(tmp_path) -> None:
    service = _service(tmp_path)

    async def broken_download(url: str) -> bytes | None:
        return None

    service._download = broken_download
    result = asyncio.run(
        service.process_candidate(
            StealCandidate(url="https://x.com/a.gif", conversation_id="", context_text="")
        )
    )
    assert result is False


def test_capacity_eviction_removes_oldest(tmp_path, monkeypatch) -> None:
    # 构造器把 library_max 钳到 ≥20（生产防呆），测试直接改属性模拟小容量。
    service = _service(tmp_path)
    service.library_max = 2
    classification = Classification(approved=True, emotion="happy")
    _patch_pipeline(monkeypatch, service, PNG_BYTES, classification)

    variants = [PNG_BYTES + bytes([i]) * 4 for i in range(4)]
    for i, content in enumerate(variants):
        monkeypatch.setattr(
            service,
            "_download",
            (lambda c: (lambda url: asyncio.sleep(0, result=c)))(content),
        )
        asyncio.run(
            service.process_candidate(
                StealCandidate(url=f"https://x.com/{i}.png", conversation_id="", context_text="")
            )
        )
        # 写入过快时 mtime 分辨率不足，用显式时间戳保证淘汰顺序确定。
        for index, image in enumerate((tmp_path / "stickers" / "happy").glob("steal_*.png")):
            stamp = 1_000_000 + index * 100 + i * 10
            os.utime(image, (stamp, stamp))

    images = sorted((tmp_path / "stickers" / "happy").glob("steal_*.png"))
    assert len(images) == 2
    sidecars = [p.with_suffix(".png.json") for p in images]
    assert all(p.exists() for p in sidecars)
    # 被淘汰的哈希仍留在去重索引里，防止淘汰文件被反复偷回来。
    assert service._known_hashes is not None
    assert len(service._known_hashes) == 4


def test_library_scan_deduplicates_existing_files(tmp_path, monkeypatch) -> None:
    # 发送库里已有图片（人工放置），同哈希偷取应跳过。
    sticker_dir = tmp_path / "stickers" / "happy"
    sticker_dir.mkdir(parents=True)
    existing = sticker_dir / "手动可爱.png"
    existing.write_bytes(PNG_BYTES)

    service = _service(tmp_path)
    classification = Classification(approved=True, emotion="happy")
    _patch_pipeline(monkeypatch, service, PNG_BYTES, classification)
    result = asyncio.run(
        service.process_candidate(
            StealCandidate(url="https://x.com/dup.png", conversation_id="", context_text="")
        )
    )
    assert result is False


def test_suffix_sniffing_from_bytes(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    classification = Classification(approved=True, emotion="sad")
    _patch_pipeline(monkeypatch, service, PNG_BYTES, classification)
    asyncio.run(
        service.process_candidate(
            StealCandidate(url="https://x.com/noext?url=1", conversation_id="", context_text="")
        )
    )
    images = list((tmp_path / "stickers" / "sad").glob("steal_*.png"))
    assert len(images) == 1


def test_worker_processes_queue_and_shuts_down(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, meme_steal_mode="probability", meme_steal_chance=1.0)
    classification = Classification(approved=True, emotion="teasing", tags=("整活",))
    _patch_pipeline(monkeypatch, service, PNG_BYTES, classification)

    async def scenario() -> None:
        event = _event(_image_segment("https://x.com/queue.gif", sub_type="1"))
        assert service.maybe_steal_from_event(event) is True
        assert service._worker_task is not None
        await asyncio.wait_for(service._queue.join(), timeout=5)
        await service.shutdown()
        assert service._worker_task is None

    asyncio.run(scenario())
    images = list((tmp_path / "stickers" / "teasing").glob("steal_*.png"))
    assert len(images) == 1


def test_shutdown_without_worker_is_noop(tmp_path) -> None:
    service = _service(tmp_path)
    asyncio.run(service.shutdown())
    assert service._worker_task is None


# ── VisionSettings ──


def test_vision_settings_from_config() -> None:
    settings = VisionSettings.from_config(
        SimpleNamespace(
            toolbox_vision_enabled=True,
            toolbox_vision_model="deepseek-flash",
            toolbox_vision_base_url="https://api.deepseek.com/v1",
            toolbox_vision_api_key="sk-test",
            toolbox_vision_timeout_seconds=55.0,
            toolbox_vision_max_bytes=8_000_000,
            openai_model="chat-model",
            openai_base_url="https://api.openai.com/v1",
            openai_api_key="sk-openai",
        )
    )
    assert settings.enabled is True
    assert settings.model == "deepseek-flash"
    assert settings.api_key == "sk-test"

    disabled = VisionSettings.from_config(SimpleNamespace(toolbox_vision_enabled=False))
    assert disabled.enabled is False


# ── StickerManager sidecar 语义召回 ──


def _write_stolen(sticker_dir, emotion: str, name: str, tags, desc="", scenes=()):
    folder = sticker_dir / emotion
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / name
    image.write_bytes(PNG_BYTES)
    image.with_suffix(image.suffix + ".json").write_text(
        json.dumps(
            {
                "source": "meme_steal",
                "emotion": emotion,
                "tags": list(tags),
                "desc": desc,
                "scenes": list(scenes),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return image


def test_sidecar_terms_cached_and_invalidated(tmp_path) -> None:
    sticker_dir = tmp_path / "stickers"
    image = _write_stolen(sticker_dir, "teasing", "steal_1_abcd1234.png", ("抽象整活",), desc="猫猫无语")
    manager = StickerManager(sticker_dir)

    terms, emotion = manager._sidecar_terms(image)
    assert emotion == "teasing"
    assert "抽象整活" in terms
    assert "猫猫无语" in terms
    # 2 元子词也在检索词里。
    assert "抽象" in terms

    cached = manager._sidecar_cache[image]
    manager._sidecar_terms(image)
    assert manager._sidecar_cache[image] is cached

    # sidecar 更新后缓存失效。
    image.with_suffix(image.suffix + ".json").write_text(
        json.dumps(
            {"emotion": "teasing", "tags": ["新标签"], "desc": "", "scenes": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    terms3, _ = manager._sidecar_terms(image)
    assert "新标签" in terms3
    assert "抽象整活" not in terms3


def test_sidecar_terms_missing_file_returns_empty(tmp_path) -> None:
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    plain = sticker_dir / "普通表情.png"
    plain.write_bytes(PNG_BYTES)
    manager = StickerManager(sticker_dir)
    assert manager._sidecar_terms(plain) == ((), "")


def test_stolen_sticker_recalled_by_sidecar_tags(tmp_path) -> None:
    sticker_dir = tmp_path / "stickers"
    target = _write_stolen(
        sticker_dir,
        "teasing",
        "steal_2_0000aaaa.png",
        ("可爱猫咪摸头",),
        desc="猫咪摸头微笑",
        scenes=("撒娇求摸时",),
    )
    other = _write_stolen(sticker_dir, "sad", "steal_3_0000bbbb.png", ("深夜emo",), desc="雨夜流泪")
    manager = StickerManager(sticker_dir)

    choice = manager.choose(
        "你家猫真的好可爱哦",
        "可爱死了，摸摸头。",
        chance=1.0,
        cooldown_seconds=0,
    )
    assert choice is not None
    assert choice.file_url == str(target.resolve())
    assert choice.file_url != str(other.resolve())


def test_sidecar_emotion_match_boosts_selection(tmp_path) -> None:
    sticker_dir = tmp_path / "stickers"
    matched = _write_stolen(sticker_dir, "angry", "steal_4_aaaa1111.png", ("警告",))
    other = _write_stolen(sticker_dir, "happy", "steal_5_bbbb2222.png", ("警告",))
    manager = StickerManager(sticker_dir)

    # 两个 sidecar 检索词一致，会话情绪是 angry（「气死」命中愤怒关键词）：
    # 同情绪的偷取包靠 emotion_match 加分应确定性胜出。
    choice = manager.choose("气死我了，警告你别乱来", "警告你哦。", chance=1.0, cooldown_seconds=0)
    assert choice is not None
    assert choice.file_url == str(matched.resolve())


def test_filename_semantic_still_works_without_sidecar(tmp_path) -> None:
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    matching = sticker_dir / "让我想想.png"
    matching.write_bytes(b"fake")
    unrelated = sticker_dir / "无关.jpg"
    unrelated.write_bytes(b"fake")
    manager = StickerManager(sticker_dir)

    choice = manager.choose(
        "这个方案应该怎么选？", "让我想想，先比较一下。", chance=1.0, cooldown_seconds=0
    )
    assert choice is not None
    assert choice.file_url == str(matching.resolve())


# ── 发送配比（自带:偷取 7:3）与容量淘汰边界 ──


def test_capacity_eviction_spares_builtin_files(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    service.library_max = 2
    builtin_dir = tmp_path / "stickers" / "happy"
    builtin_dir.mkdir(parents=True)
    builtin = builtin_dir / "自带开心.png"
    builtin.write_bytes(PNG_BYTES + b"\x00builtin")

    classification = Classification(approved=True, emotion="happy")
    _patch_pipeline(monkeypatch, service, PNG_BYTES, classification)
    variants = [PNG_BYTES + bytes([i]) * 4 for i in range(4)]
    for i, content in enumerate(variants):
        monkeypatch.setattr(
            service,
            "_download",
            (lambda c: (lambda url: asyncio.sleep(0, result=c)))(content),
        )
        asyncio.run(
            service.process_candidate(
                StealCandidate(url=f"https://x.com/{i}.png", conversation_id="", context_text="")
            )
        )
        for index, image in enumerate(builtin_dir.glob("steal_*.png")):
            stamp = 1_000_000 + index * 100 + i * 10
            os.utime(image, (stamp, stamp))

    assert builtin.exists()
    assert len(list(builtin_dir.glob("steal_*.png"))) == 2


def test_send_ratio_mixes_builtin_and_stolen(tmp_path, monkeypatch) -> None:
    sticker_dir = tmp_path / "stickers"
    happy = sticker_dir / "happy"
    happy.mkdir(parents=True)
    builtin = happy / "自带笑.png"
    builtin.write_bytes(PNG_BYTES)
    stolen = _write_stolen(sticker_dir, "happy", "steal_1_aa000000.png", ())
    manager = StickerManager(sticker_dir, steal_send_ratio=0.3)

    # _choose_image_path 每次只消耗来源骰子一次：前 3 次掷中偷取（<0.3），
    # 后 7 次走自带，得到 7:3。
    dice = [0.29] * 3 + [0.5] * 7
    calls = iter(dice)
    monkeypatch.setattr(random, "random", lambda: next(calls))
    picks = [manager._choose_image_path("哈哈", "", "happy", None) for _ in range(10)]
    assert picks.count(builtin) == 7
    assert picks.count(stolen) == 3


def test_choose_respects_send_ratio(tmp_path, monkeypatch) -> None:
    sticker_dir = tmp_path / "stickers"
    happy = sticker_dir / "happy"
    happy.mkdir(parents=True)
    builtin = happy / "自带笑.png"
    builtin.write_bytes(PNG_BYTES)
    stolen = _write_stolen(sticker_dir, "happy", "steal_1_aa000000.png", ())
    manager = StickerManager(sticker_dir, steal_send_ratio=0.3)

    # choose 每次消耗两次 random()：先过 chance 检查，再掷来源骰子。
    calls = iter([0.5, 0.29, 0.5, 0.5])
    monkeypatch.setattr(random, "random", lambda: next(calls))
    first = manager.choose("哈哈", "", chance=1.0, cooldown_seconds=0)
    second = manager.choose("哈哈", "", chance=1.0, cooldown_seconds=0)
    assert first is not None and first.file_url == str(stolen.resolve())
    assert second is not None and second.file_url == str(builtin.resolve())


def test_stolen_branch_prefers_sidecar_semantic_match(tmp_path, monkeypatch) -> None:
    sticker_dir = tmp_path / "stickers"
    matched = _write_stolen(sticker_dir, "happy", "steal_1_aa000000.png", ("可爱猫咪",), desc="猫咪可爱")
    other = _write_stolen(sticker_dir, "happy", "steal_2_bb000000.png", ("深夜emo",), desc="雨夜流泪")
    manager = StickerManager(sticker_dir, steal_send_ratio=0.3)

    monkeypatch.setattr(random, "random", lambda: 0.29)
    assert manager._choose_image_path("好可爱的猫咪", "可爱哦。", "happy", None) == matched


def test_builtin_branch_excludes_stolen_when_ratio_enabled(tmp_path, monkeypatch) -> None:
    sticker_dir = tmp_path / "stickers"
    happy = sticker_dir / "happy"
    happy.mkdir(parents=True)
    builtin = happy / "自带笑.png"
    builtin.write_bytes(PNG_BYTES)
    stolen = _write_stolen(sticker_dir, "happy", "steal_1_aa000000.png", ("哈哈笑死",))

    manager = StickerManager(sticker_dir, steal_send_ratio=0.3)
    monkeypatch.setattr(random, "random", lambda: 0.9)
    # 骰子走自带：sidecar 语义更贴的偷取包被排除，选自带文件。
    assert manager._choose_image_path("哈哈笑死我了", "", "happy", None) == builtin

    # ratio=0（旧行为）：全库混选，语义命中的偷取包仍可被召回。
    legacy = StickerManager(sticker_dir)
    assert legacy._choose_image_path("哈哈笑死我了", "", "happy", None) == stolen


def test_ratio_falls_back_when_pool_empty(tmp_path, monkeypatch) -> None:
    # 偷取库空：骰子命中偷取也回退自带。
    sticker_dir = tmp_path / "stickers"
    happy = sticker_dir / "happy"
    happy.mkdir(parents=True)
    builtin = happy / "自带笑.png"
    builtin.write_bytes(PNG_BYTES)
    manager = StickerManager(sticker_dir, steal_send_ratio=0.3)
    monkeypatch.setattr(random, "random", lambda: 0.29)
    assert manager._choose_image_path("哈哈", "", "happy", None) == builtin

    # 自带库该情绪无图：回退偷取库。
    sticker_dir2 = tmp_path / "stickers2"
    stolen = _write_stolen(sticker_dir2, "happy", "steal_1_aa000000.png", ())
    manager2 = StickerManager(sticker_dir2, steal_send_ratio=0.3)
    monkeypatch.setattr(random, "random", lambda: 0.9)
    assert manager2._choose_image_path("哈哈", "", "happy", None) == stolen


def test_ratio_neutral_random_fallback_suppressed(tmp_path, monkeypatch) -> None:
    # neutral 消息且偷取包无语义命中：不随机发偷取包（交给 QQ 表情兜底）。
    sticker_dir = tmp_path / "stickers"
    _write_stolen(sticker_dir, "happy", "steal_1_aa000000.png", ())
    manager = StickerManager(sticker_dir, steal_send_ratio=0.3)
    monkeypatch.setattr(random, "random", lambda: 0.29)
    assert manager._choose_image_path("今天天气不错", "", "neutral", None) is None


def test_ratio_zero_keeps_legacy_whole_library_mix(tmp_path) -> None:
    # 默认构造（ratio=0）：偷取包靠 sidecar 语义在全库混选中被召回。
    sticker_dir = tmp_path / "stickers"
    happy = sticker_dir / "happy"
    happy.mkdir(parents=True)
    builtin = happy / "自带笑.png"
    builtin.write_bytes(PNG_BYTES)
    stolen = _write_stolen(sticker_dir, "happy", "steal_1_aa000000.png", ("开心猫咪",))
    manager = StickerManager(sticker_dir)

    choice = manager.choose("好可爱的猫咪", "可爱哦。", chance=1.0, cooldown_seconds=0)
    assert choice is not None
    assert choice.file_url == str(stolen.resolve())
