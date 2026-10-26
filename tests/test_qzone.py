"""QQ 空间模块测试：解析层、会话借取、存储状态机、生成规则与开发日志闭环。"""

from __future__ import annotations

import asyncio
import subprocess
import time
from types import SimpleNamespace

import pytest

import atri_qq_bot.qzone as qzone
from atri_qq_bot.qzone.api import QzoneAPI, parse_response
from atri_qq_bot.qzone.devlog import (
    DevLog,
    classify_commit,
    collect_commits,
    devlog_material_prompt,
    is_sensitive_subject,
)
from atri_qq_bot.qzone.rules import (
    build_comment_prompt,
    build_post_prompt,
    comment_quality_issues,
    post_quality_issues,
)
from atri_qq_bot.qzone.service import QzoneService
from atri_qq_bot.qzone.session import QzoneContext, QzoneSession
from atri_qq_bot.qzone.store import QzoneStore


def _config(**overrides):
    base = {
        "qzone_enabled": True,
        "qzone_ai_tools_enabled": True,
        "qzone_timeout_seconds": 20.0,
        "qzone_session_ttl_seconds": 600.0,
        "qzone_visibility": 1,
        "qzone_timezone": "Asia/Shanghai",
        "qzone_auto_comment_enabled": True,
        "qzone_auto_comment_time": "23:30",
        "qzone_auto_comment_jitter_minutes": 10,
        "qzone_auto_comment_max": 2,
        "qzone_random_comment_probability": 0.001,
        "qzone_random_comment_cooldown_minutes": 30,
        "qzone_devlog_enabled": True,
        "qzone_devlog_auto_publish": True,
        "qzone_devlog_daily_cap": 2,
        "qzone_devlog_visibility_types": ("feat", "fix", "perf", "refactor"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def store(tmp_path):
    return QzoneStore(tmp_path / "qzone_automation.sqlite3")


@pytest.fixture()
def git_repo(tmp_path):
    def run(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    run("init", "-q")
    run("config", "user.email", "atri@example.com")
    run("config", "user.name", "ATRI Test")
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "core: 新增启动流程")
    return tmp_path


def _commit(git_repo, name, subject):
    (git_repo / name).write_text("v", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "-A"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-q", "-m", subject],
        check=True, capture_output=True,
    )


def _head_sha(git_repo, *extra):
    completed = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", *extra, "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return completed.stdout.strip()


class TestParseResponse:
    def test_plain_json(self):
        assert parse_response('{"code": 0, "msglist": []}')["code"] == 0

    def test_jsonp_callback(self):
        parsed = parse_response('_preloadCallback({"code": 0, "msglist": []});')
        assert parsed["code"] == 0

    def test_undefined_becomes_null(self):
        assert parse_response('{"code": undefined, "data": null}')["code"] is None

    def test_trailing_comma_recovered(self):
        parsed = parse_response('{"code": 0, "list": [1, 2,],}')
        assert parsed["code"] == 0
        assert parsed["list"] == [1, 2]

    def test_failures_return_chinese_error(self):
        assert parse_response("")["ok"] is False
        assert "空响应" in parse_response("")["message"]
        assert parse_response("nothing here")["ok"] is False
        assert parse_response("[1,2,3]")["ok"] is False

    def test_feed_from_html_extracts_content(self):
        entry = {
            "appid": "311",
            "uin": "123456",
            "key": "abc",
            "nickname": "好友A",
            "abstime": 1700000000,
            "html": '<div class="f-info">今晚的<span>月色</span>真不错&nbsp;呀</div>',
        }
        feed = QzoneAPI._feed_from_html(entry)
        assert feed == {
            "tid": "abc",
            "uin": 123456,
            "name": "好友A",
            "content": "今晚的 月色 真不错 呀",
            "create_time": 1700000000,
            "comments": [],
            "like_count": 0,
        }

    def test_feed_from_html_rejects_invalid_entries(self):
        assert QzoneAPI._feed_from_html({"appid": "999", "uin": "1", "key": "k"}) is None
        assert QzoneAPI._feed_from_html({"appid": "311", "uin": "x", "key": "k"}) is None
        assert QzoneAPI._feed_from_html({"appid": "311", "uin": "1", "key": "k", "html": ""}) is None


class TestSession:
    def test_gtk2_known_values(self):
        assert QzoneContext(uin=1, skey="s", p_skey="").gtk2 == "5381"
        assert QzoneContext(uin=1, skey="s", p_skey="@abc").gtk2 == "2088882539"

    def test_cookies_and_headers(self):
        ctx = QzoneContext(uin=123456, skey="SK", p_skey="PS")
        assert ctx.cookies() == {"uin": "o123456", "skey": "SK", "p_skey": "PS"}
        assert ctx.headers()["Referer"].endswith("/123456")

    def _provider(self, calls):
        async def provider(domain):
            calls.append(domain)
            return {"cookies": "uin=o123456; skey=SK; p_skey=@abc"}
        return provider

    def test_borrows_once_and_caches(self):
        calls: list[str] = []

        async def run():
            session = QzoneSession(self._provider(calls))
            ctx = await session.get_ctx()
            assert ctx.uin == 123456
            assert (await session.get_ctx()) is ctx

        asyncio.run(run())
        assert calls == ["user.qzone.qq.com"]

    def test_invalidate_forces_refresh(self):
        calls: list[str] = []

        async def run():
            session = QzoneSession(self._provider(calls))
            await session.get_ctx()
            await session.invalidate()
            await session.get_ctx()

        asyncio.run(run())
        assert len(calls) == 2

    def test_missing_uin_raises(self):
        async def provider(domain):
            return {"cookies": "skey=SK"}

        async def run():
            session = QzoneSession(provider)
            try:
                await session.get_ctx()
            except RuntimeError as exc:
                assert "uin" in str(exc)
            else:
                raise AssertionError("应抛出 RuntimeError")

        asyncio.run(run())


class TestStore:
    def test_devlog_commit_lifecycle(self, store):
        added = store.devlog_add_commits(
            [
                {"sha": "s1", "subject": "feat: 新增功能", "commit_type": "feat", "visible": True},
                {"sha": "s2", "subject": "chore: 杂项", "commit_type": "chore", "visible": False},
            ]
        )
        assert added == 2
        assert store.devlog_add_commits([{"sha": "s1", "subject": "feat: 新增功能"}]) == 0
        pending = store.devlog_pending_commits()
        assert [item["sha"] for item in pending] == ["s1"]

    def test_devlog_draft_state_machine(self, store):
        store.devlog_add_commits([{"sha": "s1", "subject": "fix: 修复", "commit_type": "fix"}])
        item = store.devlog_create_post_if_absent("手记内容", commit_shas=["s1"])
        assert item is not None and item["status"] == "draft"
        assert store.devlog_create_post_if_absent("另一篇", commit_shas=["s1"]) is None
        store.devlog_update_post_content(int(item["id"]), "修改后的手记内容")
        assert store.devlog_draft()["content"] == "修改后的手记内容"
        assert store.devlog_begin_publish(int(item["id"])) is True
        assert store.devlog_begin_publish(int(item["id"])) is False
        assert store.devlog_draft() is None

    def test_devlog_finish_publish_marks_commits(self, store):
        store.devlog_add_commits(
            [
                {"sha": "s1", "subject": "fix: 修复A", "commit_type": "fix"},
                {"sha": "s2", "subject": "fix: 修复B", "commit_type": "fix"},
            ]
        )
        item = store.devlog_create_post_if_absent("手记", commit_shas=["s1", "s2"])
        store.devlog_begin_publish(int(item["id"]))
        shas = store.devlog_finish_publish(int(item["id"]), "tid-9")
        assert shas == ["s1", "s2"]
        posts = store.devlog_posts()
        assert posts[0]["status"] == "published" and posts[0]["tid"] == "tid-9"
        assert store.devlog_pending_commits() == []
        assert store.devlog_count_published_since(time.time() - 3600) == 1

    def test_devlog_fail_publish_controls_commit_consumption(self, store):
        store.devlog_add_commits([{"sha": "s1", "subject": "fix: 修复", "commit_type": "fix"}])
        item = store.devlog_create_post_if_absent("手记", commit_shas=["s1"])
        store.devlog_begin_publish(int(item["id"]))
        store.devlog_fail_publish(int(item["id"]), "connection refused", consume_commits=False)
        assert store.devlog_pending_commits()[0]["sha"] == "s1"
        assert store.devlog_posts()[0]["status"] == "failed"

        second = store.devlog_create_post_if_absent("重试手记", commit_shas=["s1"])
        assert second is not None
        store.devlog_begin_publish(int(second["id"]))
        store.devlog_fail_publish(int(second["id"]), "timed out", consume_commits=True)
        assert store.devlog_pending_commits() == []

    def test_devlog_content_validation(self, store):
        with pytest.raises(ValueError):
            store.devlog_create_post_if_absent("", commit_shas=[])
        with pytest.raises(ValueError):
            store.devlog_create_post_if_absent("字" * 1001, commit_shas=[])

    def test_action_log_idempotent(self, store):
        store.log("like", tid="t1", status="success")
        store.log("like", tid="t1", status="success")
        assert store.already_logged("like", "t1") is True
        assert store.already_logged("like", "t2") is False
        assert len(store.logs(action="like")) == 1

    def test_recent_post_contents_and_count(self, store):
        store.log("post", tid="t1", content="第一条说说", status="success")
        store.log("post", tid="t2", content="第二条说说", status="success")
        store.log("post", tid="t3", content="失败草稿", status="failed")
        assert store.recent_post_contents() == ["第二条说说", "第一条说说"]
        assert store.count_success_since("post", time.time() - 3600) == 2

    def test_settings_roundtrip(self, store):
        store.save_settings({"auto_comment_next_run": 123.0})
        assert store.settings()["auto_comment_next_run"] == 123.0


class TestRules:
    def test_build_post_prompt_rotations(self):
        prompt = build_post_prompt({"timezone": "Asia/Shanghai"}, time.time())
        for key in ("主题方向", "切入角度", "写作形态", "发布时间"):
            assert key in prompt
        assert "「主人」" in prompt or "主人" in prompt

    def test_build_post_prompt_recent_hint(self):
        prompt = build_post_prompt(
            {"timezone": "Asia/Shanghai", "recent_contents": ["旧动态内容一"]},
            time.time(),
        )
        assert "旧动态内容一" in prompt
        assert "排重" in prompt

    def test_build_comment_prompt(self):
        prompt = build_comment_prompt({"name": "小明", "content": "今天去爬山了"})
        assert "小明" in prompt
        assert "今天去爬山了" in prompt
        assert "不使用「主人」" in prompt

    def test_post_quality_ok(self):
        content = "把遥控器的电池正负极装反，指示灯会暗一半，这是电压反向击穿的信号。"
        assert post_quality_issues(content) == []

    def test_post_quality_short(self):
        assert "过短" in "；".join(post_quality_issues("太短了"))

    def test_post_quality_fake_experience(self):
        content = "我在楼下的便利店看到新到的杨梅，个头比上周小一圈，味道也淡。"
        issues = "；".join(post_quality_issues(content))
        assert "虚构亲历" in issues

    def test_post_quality_cliche_density(self):
        content = "阳光真美好，海风轻轻吹过，微笑可以治愈一切，加油加油加油。"
        issues = "；".join(post_quality_issues(content))
        assert "密度过高" in issues

    def test_post_quality_master_word(self):
        content = "写给主人的一句话，愿你的每一天都被认真对待，不被辜负。"
        issues = "；".join(post_quality_issues(content))
        assert "主人" in issues

    def test_post_quality_unsourced_claims(self):
        content = "最新研究显示，每天喝两杯咖啡可以显著降低某类风险，值得一试。"
        issues = "；".join(post_quality_issues(content))
        assert "来源" in issues

    def test_post_quality_duplicate_and_similar(self):
        recent = ["今天学会了用压力锅煮粥，失败两次后终于掌握火候和水量。"]
        assert "与近期内容重复" in "；".join(post_quality_issues(recent[0], recent))
        similar = "今天学会了用压力锅煮汤，失败两次后终于掌握火候和水量。"
        issues = "；".join(post_quality_issues(similar, recent))
        assert "表述高度相似" in issues

    def test_comment_quality_issues(self):
        assert comment_quality_issues("") != []
        assert comment_quality_issues("x" * 201) != []
        assert any("链接" in issue for issue in comment_quality_issues("看看 https://example.com"))
        assert any("主人" in issue for issue in comment_quality_issues("主人真厉害"))
        assert comment_quality_issues("这个观察有意思，我上次也注意到类似的细节。") == []


class TestDevlogHelpers:
    def test_classify_commit(self):
        assert classify_commit("feat: add voice pipeline") == "feat"
        assert classify_commit("修复夜间排期错乱") == "fix"
        assert classify_commit("qzone: 新增说说接口") == "feat"
        assert classify_commit("core: 修复启动流程") == "fix"
        assert classify_commit("docs: 更新说明") == "docs"
        assert classify_commit("随手一改") == "other"

    def test_is_sensitive_subject(self):
        for subject in (
            "更新 config.env 配置",
            "参见 https://example.com/x",
            "修复 C:\\data\\log.py 的解析",
            "轮换 api_key",
            "联系 atri@example.com",
        ):
            assert is_sensitive_subject(subject) is True
        assert is_sensitive_subject("修复夜间排期错乱") is False

    def test_material_prompt_folds_overflow(self):
        commits = [
            {"commit_type": "fix", "subject": f"修复问题{i}"} for i in range(17)
        ]
        prompt = devlog_material_prompt(commits)
        assert prompt.count("- fix：") == 15
        assert "另有 2 项" in prompt
        assert "不得虚构" in prompt


class TestCollectCommits:
    def test_initial_head_only(self, git_repo):
        commits = collect_commits(git_repo, "")
        assert len(commits) == 1
        assert commits[0]["init_only"] is True
        assert commits[0]["sha"] == _head_sha(git_repo)

    def test_incremental_collect(self, git_repo):
        first = _head_sha(git_repo)
        _commit(git_repo, "b.txt", "fix: 修复夜间排期")
        commits = collect_commits(git_repo, first)
        assert len(commits) == 1
        assert commits[0]["subject"] == "fix: 修复夜间排期"
        assert commits[0]["sha"] == _head_sha(git_repo)


GOOD_DEVLOG_CONTENT = "今晚把好友动态的评论链路修好了，重复评论和过期动态都拦住了，还留了每日上限。"


def _generator_returning(content):
    async def generator(prompt):
        return {"ok": True, "content": content}
    return generator


def _publisher_returning(tid):
    async def publisher(content):
        return {"ok": True, "data": {"tid": tid}}
    return publisher


def _raising_publisher(exc):
    async def publisher(content):
        raise exc
    return publisher


class TestDevLog:
    def _devlog(self, store, git_repo, **overrides):
        return DevLog(
            store,
            _config(**overrides),
            post_generator=None,
            publisher=None,
            root=git_repo,
        )

    def test_scan_initializes_watermark_without_broadcast(self, store, git_repo):
        result = self._devlog(store, git_repo).scan(force=True)
        assert result["initialized"] is True
        assert result["added"] == 0
        assert store.settings()["devlog_last_scan_sha"] == _head_sha(git_repo)

    def test_scan_increment_adds_visible(self, store, git_repo):
        devlog = self._devlog(store, git_repo)
        devlog.scan(force=True)
        first = _head_sha(git_repo)
        _commit(git_repo, "b.txt", "fix: 修复夜间排期")
        _commit(git_repo, "c.txt", "chore: 杂项整理")
        result = devlog.scan(force=True)
        assert result["added"] == 2
        assert result["visible_added"] == 1
        pending = store.devlog_pending_commits()
        assert [item["subject"] for item in pending] == ["fix: 修复夜间排期"]
        assert store.settings()["devlog_last_scan_sha"] == _head_sha(git_repo)

    def test_generate_skips_without_pending_or_generator(self, store, git_repo):
        devlog = self._devlog(store, git_repo)
        assert asyncio.run(devlog.generate(force=True))["skipped"] == "没有待写进手记的提交"
        store.devlog_add_commits(
            [{"sha": "s1", "subject": "fix: 修复", "commit_type": "fix", "visible": True}]
        )
        assert asyncio.run(devlog.generate(force=True))["skipped"] == "生成器尚未就绪"

    def test_generate_creates_draft_and_rejects_bad_content(self, store, git_repo):
        devlog = self._devlog(store, git_repo)
        store.devlog_add_commits(
            [{"sha": "s1", "subject": "fix: 修复", "commit_type": "fix", "visible": True}]
        )
        devlog.post_generator = _generator_returning("太短")
        result = asyncio.run(devlog.generate(force=True))
        assert result["ok"] is False and "检查" in result["skipped"]

        devlog.post_generator = _generator_returning(GOOD_DEVLOG_CONTENT)
        result = asyncio.run(devlog.generate(force=True))
        assert result["ok"] is True and result["item"]["status"] == "draft"
        assert asyncio.run(devlog.generate(force=True))["skipped"] == "已有草稿"

    def test_publish_success_flow(self, store, git_repo):
        devlog = self._devlog(store, git_repo)
        store.devlog_add_commits(
            [{"sha": "s1", "subject": "fix: 修复", "commit_type": "fix", "visible": True}]
        )
        devlog.post_generator = _generator_returning(GOOD_DEVLOG_CONTENT)
        devlog.publisher = _publisher_returning("tid-1")
        assert asyncio.run(devlog.generate(force=True))["ok"] is True
        result = asyncio.run(devlog.publish(force=True))
        assert result["ok"] is True and result["tid"] == "tid-1"
        assert store.devlog_pending_commits() == []
        assert store.devlog_posts()[0]["status"] == "published"
        assert asyncio.run(devlog.publish(force=True))["skipped"] == "没有可发布的草稿"

    def test_publish_timeout_consumes_commits(self, store, git_repo):
        devlog = self._devlog(store, git_repo)
        store.devlog_add_commits(
            [{"sha": "s1", "subject": "fix: 修复", "commit_type": "fix", "visible": True}]
        )
        devlog.post_generator = _generator_returning(GOOD_DEVLOG_CONTENT)
        devlog.publisher = _raising_publisher(RuntimeError("请求 timed out"))
        asyncio.run(devlog.generate(force=True))
        result = asyncio.run(devlog.publish(force=True))
        assert result["ok"] is False and result["uncertain"] is True
        assert store.devlog_pending_commits() == []

    def test_publish_failure_keeps_commits_for_retry(self, store, git_repo):
        devlog = self._devlog(store, git_repo)
        store.devlog_add_commits(
            [{"sha": "s1", "subject": "fix: 修复", "commit_type": "fix", "visible": True}]
        )
        devlog.post_generator = _generator_returning(GOOD_DEVLOG_CONTENT)
        devlog.publisher = _raising_publisher(RuntimeError("connection refused"))
        asyncio.run(devlog.generate(force=True))
        result = asyncio.run(devlog.publish(force=True))
        assert result["ok"] is False and result["uncertain"] is False
        assert store.devlog_pending_commits()[0]["sha"] == "s1"
        devlog.publisher = _publisher_returning("tid-2")
        asyncio.run(devlog.generate(force=True))
        assert asyncio.run(devlog.publish(force=True))["ok"] is True

    def test_daily_cap_blocks_publish(self, store, git_repo):
        devlog = self._devlog(store, git_repo, qzone_devlog_daily_cap=1)
        store.devlog_add_commits(
            [{"sha": "s1", "subject": "fix: 修复", "commit_type": "fix", "visible": True}]
        )
        devlog.post_generator = _generator_returning(GOOD_DEVLOG_CONTENT)
        devlog.publisher = _publisher_returning("tid-1")
        asyncio.run(devlog.generate(force=True))
        assert asyncio.run(devlog.publish(force=True))["ok"] is True

        store.devlog_add_commits(
            [{"sha": "s2", "subject": "fix: 修复B", "commit_type": "fix", "visible": True}]
        )
        asyncio.run(devlog.generate(force=True))
        assert asyncio.run(devlog.publish(force=True))["skipped"] == "已达每日开发手记上限"

    def test_cycle_disabled(self, store, git_repo):
        result = asyncio.run(
            self._devlog(store, git_repo, qzone_devlog_enabled=False).cycle(force=True)
        )
        assert result["ok"] is False

    def test_status_snapshot(self, store, git_repo):
        status = self._devlog(store, git_repo).status()
        assert status["enabled"] is True
        assert status["visibility_types"] == ["feat", "fix", "perf", "refactor"]
        assert "pending_count" in status and "draft" in status


class TestServiceSingleton:
    def test_disabled_returns_none(self):
        assert qzone.get_qzone_service(_config(qzone_enabled=False)) is None

    def test_singleton_and_reset(self, tmp_path, monkeypatch):
        monkeypatch.setattr("atri_qq_bot.qzone.store.PROJECT_ROOT", tmp_path)
        qzone.reset_qzone_service()
        try:
            service = qzone.get_qzone_service(_config())
            assert isinstance(service, QzoneService)
            assert qzone.get_qzone_service(_config()) is service
        finally:
            qzone.reset_qzone_service()


class TestService:
    @pytest.fixture()
    def service(self, store):
        svc = QzoneService(_config(), store=store)
        svc.bind_cookie_provider(_fake_cookie_provider)
        return svc

    def test_owner_publish_explicit_text(self, service, monkeypatch, store):
        published: list[str] = []

        async def fake_publish(self, text, *, visibility=1):
            published.append(text)
            return {"ok": True, "message": "", "data": {"tid": "tid-1", "abstime": 1}}

        monkeypatch.setattr(QzoneAPI, "publish", fake_publish)
        result = asyncio.run(service.owner_publish_post("直接发的正文"))
        assert result["ok"] is True
        assert published == ["直接发的正文"]
        assert store.recent_post_contents() == ["直接发的正文"]

    def test_owner_publish_generated_passes_quality_gate(self, service, monkeypatch):
        async def fake_publish(self, text, *, visibility=1):
            return {"ok": True, "message": "", "data": {"tid": "tid-2", "abstime": 1}}

        async def generator(prompt):
            return {"ok": True, "content": "把遥控器的电池正负极装反，指示灯会暗一半，这是电压反向击穿的信号。"}

        monkeypatch.setattr(QzoneAPI, "publish", fake_publish)
        service.bind_generators(generator, generator)
        result = asyncio.run(service.owner_publish_post())
        assert result["ok"] is True

    def test_owner_publish_generated_fails_quality_gate(self, service, monkeypatch):
        async def generator(prompt):
            return {"ok": True, "content": "太短"}

        service.bind_generators(generator, generator)
        result = asyncio.run(service.owner_publish_post())
        assert result["ok"] is False
        assert "质量检查" in result["message"]

    def test_owner_publish_requires_generator(self, service):
        result = asyncio.run(service.owner_publish_post())
        assert result["ok"] is False
        assert "生成器尚未就绪" in result["message"]

    def test_auto_comment_filters_and_comments(self, service, store, monkeypatch):
        now = time.time()
        feeds = {
            "ok": True,
            "message": "",
            "data": {
                "feeds": [
                    {"uin": 123456, "tid": "self", "content": "自己的动态", "create_time": now, "name": "我"},
                    {"uin": 111, "tid": "stale", "content": "过期动态", "create_time": now - 5 * 86400, "name": "旧好友"},
                    {"uin": 222, "tid": "done", "content": "已评论过的动态", "create_time": now, "name": "已评论"},
                    {"uin": 333, "tid": "fresh", "content": "新鲜动态，值得评论", "create_time": now, "name": "新好友"},
                ]
            },
        }

        async def fake_recent(self, *, num=20):
            return feeds

        async def fake_comment(self, target_uin, tid, content):
            return {"ok": True, "message": "", "data": {"tid": tid, "uin": target_uin, "content": content}}

        async def generator(prompt):
            return {"ok": True, "content": "这个观察有意思，我上次也注意到类似的细节。"}

        monkeypatch.setattr(QzoneAPI, "get_recent_feeds", fake_recent)
        monkeypatch.setattr(QzoneAPI, "comment", fake_comment)
        store.log("comment", tid="done", status="success")
        service.bind_generators(None, generator)
        result = asyncio.run(service.auto_comment_run(max_count=5))
        assert result["ok"] is True
        assert [item["tid"] for item in result["commented"]] == ["fresh"]
        assert result["commented"][0]["content"].startswith("这个观察")

        again = asyncio.run(service.auto_comment_run(max_count=5))
        assert again["commented"] == []

    def test_auto_comment_disabled(self, service):
        service._config = _config(qzone_auto_comment_enabled=False)
        result = asyncio.run(service.auto_comment_run())
        assert result["skipped"] == "自动评论未启用"

    def test_comment_feed_validates_input(self, service):
        assert asyncio.run(service.comment_feed("abc", "t1", "你好"))["ok"] is False
        assert asyncio.run(service.comment_feed("111", "t1", ""))["ok"] is False

    def test_publish_post_validates_content(self, service):
        assert asyncio.run(service.publish_post(""))["ok"] is False
        assert asyncio.run(service.publish_post("字" * 1001))["ok"] is False

    def test_status_snapshot(self, service):
        status = service.status()
        assert status["settings"]["enabled"] is True
        assert status["settings"]["auto_comment_time"] == "23:30"
        assert "devlog" in status and "logs" in status


async def _fake_cookie_provider(domain):
    return {"cookies": "uin=o123456; skey=SK; p_skey=@abc"}


class TestQzoneCommandLayer:
    def _server(self, **overrides):
        from atri_qq_bot.onebot.server_parts.server_qzone import OneBotQzoneMixin

        server = object.__new__(OneBotQzoneMixin)
        server.config = _config(**overrides)
        return server

    def test_normalize_command(self):
        from atri_qq_bot.onebot.server_parts.server_qzone import _normalize_command

        assert _normalize_command("发条说说") == "发条说说"
        assert _normalize_command(" 发条 说说！～ ") == "发条说说"
        assert _normalize_command("@亚托莉 更新日志？") == "更新日志"

    def test_anchor_parsing(self):
        server = self._server(qzone_auto_comment_time="9:05")
        assert server._qzone_anchor() == (9, 5)
        assert self._server(qzone_auto_comment_time="bad")._qzone_anchor() == (23, 30)

    def test_plan_next_run_respects_anchor_without_jitter(self):
        from datetime import datetime

        from atri_qq_bot.qzone.rules import resolve_timezone

        server = self._server(qzone_auto_comment_time="23:30", qzone_auto_comment_jitter_minutes=0)
        zone = resolve_timezone("Asia/Shanghai")
        today = datetime.now(zone)
        now = today.replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
        planned = server._qzone_plan_next_run(now)
        expected = today.replace(hour=23, minute=30, second=0, microsecond=0).timestamp()
        assert planned == expected

    def test_devlog_reply_text_branches(self):
        server = self._server()
        assert "对齐" in server._qzone_devlog_reply_text({"ok": True, "scan": {"ok": True, "initialized": True}})
        assert "没读出来" in server._qzone_devlog_reply_text({"ok": True, "scan": {"ok": False, "error": "失败"}})
        assert "发到空间啦" in server._qzone_devlog_reply_text(
            {"ok": True, "scan": {"ok": True}, "generate": {"ok": True}, "publish": {"ok": True, "content": "手记内容"}}
        )
        assert "每日上限" in server._qzone_devlog_reply_text(
            {"ok": True, "scan": {"ok": True}, "generate": {"ok": True}, "publish": {"ok": True, "skipped": "已达每日开发手记上限"}}
        )
        assert "没有新的可见提交" in server._qzone_devlog_reply_text(
            {"ok": True, "scan": {"ok": True}, "generate": {"ok": True, "skipped": "没有待写进手记的提交"}, "publish": {}}
        )
