"""LLM 产品化特性：语义 Inbox、LLM 简报、行动步骤校验、课表文本解析。

所有 LLM 交互用 FakeGateway（确定性回复），嵌入用 _TopicEmbedder
（关键词命中维度 → 可控余弦），不依赖网络与本地模型缓存状态。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
from types import SimpleNamespace

import pytest

from campus.extractor import CampusEventExtractor, classify_category, rule_extract
from campus.radar import CampusRadar
import campus.semantic as semantic
from personal.briefing import BriefingService
from personal.schedule_parser import ScheduleTextParser
from personal.service import PersonalService
from personal.store import PersonalStore


class _AllowRobots:
    async def allowed(self, client, url, user_agent):
        return True


class _FakeGateway:
    """记录调用的确定性 gateway 替身；reply(messages, system) → 文本。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def call(self, *, client, model, messages, system=None, span_name="llm_call", **kwargs):
        self.calls.append({"messages": messages, "system": system, "span": span_name})
        text = self.reply(messages, system)
        return SimpleNamespace(response=SimpleNamespace(content=[SimpleNamespace(text=text)]))


class _TopicEmbedder:
    """文本命中词组 → 对应维度置 1：同组词余弦 1.0，异组 0.0，完全可控。"""

    def __init__(self, topic_groups):
        self.topic_groups = topic_groups

    def _vec(self, text):
        return [float(any(word in text for word in words)) for words in self.topic_groups]

    def embed_query(self, texts):
        return [self._vec(t) for t in texts]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]


@pytest.fixture(autouse=True)
def _clean_semantic_caches(monkeypatch):
    """向量缓存按 content_hash 键全局共享，测试间清空避免串味。"""
    monkeypatch.setattr(semantic, "_DOC_CACHE", {})
    monkeypatch.setattr(semantic, "_PROFILE_CACHE", {})


def _save_event(radar, url, title, summary, *, deadline=None, targets=None, actions=None, materials=None):
    """经 _upsert_event_sync 入库（与真实 refresh 链路一致，materials/category 均落库）。"""
    radar._upsert_event_sync(
        {
            "url": url,
            "title": title,
            "summary": summary,
            "body": f"{title}。{summary}",
            "source_name": "测试来源",
            "source_category": "academic",
            "published_at": datetime.now().astimezone().date().isoformat(),
            "deadline": deadline,
            "targets": targets or [],
            "actions": actions or [],
            "requirements": [],
            "materials": materials or [],
            "location": "",
            "event_type": "通知",
            "category": "",
            "content_hash": hashlib.sha256(f"{title}{summary}".encode()).hexdigest(),
        }
    )


# ── 步骤 1：extractor 分类（LLM 枚举校验 + 规则兜底）──────────────────────────


def test_classify_category_rules_match_radar_contract():
    assert classify_category("关于选课的通知", "请于本周内确认") == "action"
    assert classify_category("数学建模大赛启动", "欢迎报名") == "action"  # 报名 → action 优先
    assert classify_category("关于调整讲座时间的通知", "周五讲座改期") == "campus_life"
    assert classify_category("学校 pandemics 防控通告", "日常通报") == "announcement"


def test_rule_extract_emits_category():
    result = rule_extract("国家奖学金申请", "本科生请于 2026年9月15日 前提交申请材料。")
    assert result["category"] in ("action", "opportunity")


def test_extractor_llm_category_valid_and_invalid():
    body = "本科生请于 2026年9月15日 前提交申请材料。"

    def make_gateway(category):
        return _FakeGateway(
            lambda messages, system: (
                '{"event_type": "通知", "summary": "提交申请材料", "category": "'
                + category
                + '", "deadline": "2026-09-15", "targets": ["本科生"], "materials": [], "actions": ["提交"]}'
            )
        )

    ok = CampusEventExtractor(client=object(), model="m", gateway=make_gateway("opportunity"))
    result = asyncio.run(ok.extract("国家奖学金申请", body))
    assert result["category"] == "opportunity"  # LLM 枚举内选择生效
    assert result["extraction"] == "llm"

    bad = CampusEventExtractor(client=object(), model="m", gateway=make_gateway("热门吃瓜"))
    result = asyncio.run(bad.extract("国家奖学金申请", body))
    assert result["category"] == "action"  # 非法值回退规则分类（有 deadline → action）


def test_extractor_without_llm_keeps_rule_category():
    extractor = CampusEventExtractor()
    result = asyncio.run(extractor.extract("关于举办学术讲座的通知", "本周五下午在图书馆举行讲座。"))
    assert result["extraction"] == "rule"
    assert result["category"] == "campus_life"


# ── 步骤 2：语义相关度 / 聚类合并 / 查询检索 ──────────────────────────────────


def test_inbox_semantic_boost_admits_keyword_miss(tmp_path, monkeypatch):
    store = PersonalStore(str(tmp_path / "semantic.db"))
    radar = CampusRadar(store, robots=_AllowRobots())
    _save_event(radar, "https://n.example/1", "卫星创新大赛报名开始", "研究生报名时间截至本月底。")
    profile = {"education": "", "interests": ["航天"], "college": ""}
    monkeypatch.setattr(semantic, "get_embedder", lambda: _TopicEmbedder([["航天", "卫星"]]))

    inbox = asyncio.run(radar.inbox("u", profile))

    # 关键词分数只有 1（"报名"词），语义强相关 +3 跨过准入阈值
    assert len(inbox) == 1
    assert "语义相关" in inbox[0]["reason"]
    assert inbox[0]["relevance"] >= 2


def test_inbox_without_embedder_keeps_keyword_threshold(tmp_path, monkeypatch):
    store = PersonalStore(str(tmp_path / "no-embedder.db"))
    radar = CampusRadar(store, robots=_AllowRobots())
    _save_event(radar, "https://n.example/2", "卫星创新大赛报名开始", "研究生报名时间截至本月底。")
    monkeypatch.setattr(semantic, "get_embedder", lambda: None)

    assert asyncio.run(radar.inbox("u", {"education": "", "interests": ["航天"], "college": ""})) == []


def test_inbox_semantic_never_demotes_keyword_hits(tmp_path, monkeypatch):
    store = PersonalStore(str(tmp_path / "no-demotion.db"))
    radar = CampusRadar(store, robots=_AllowRobots())
    _save_event(radar, "https://n.example/3", "国家奖学金申请", "本科生请提交申请材料。", targets=["本科生"])
    profile = {"education": "本科生", "interests": ["奖学金"], "college": ""}
    # 语义层判定完全不相关（余弦 0）也不扣分，关键词命中的通知照常进入
    monkeypatch.setattr(semantic, "get_embedder", lambda: _TopicEmbedder([["完全不相关词"]]))

    inbox = asyncio.run(radar.inbox("u", profile))

    assert len(inbox) == 1
    assert "奖学金" in inbox[0]["reason"]


def test_briefing_merges_semantically_identical_events(tmp_path, monkeypatch):
    store = PersonalStore(str(tmp_path / "cluster.db"))
    radar = CampusRadar(store, robots=_AllowRobots())
    _save_event(radar, "https://n.example/a", "全国大学生数学建模竞赛报名开始", "报名截至月底。")
    # 第二条通知不含任何关键词命中（"大赛/参赛"不在词表），靠语义加成进入 Inbox，
    # 再由语义聚类与第一条合并为同一事件
    _save_event(radar, "https://n.example/b", "数学建模大赛参赛细则发布", "请参赛队伍查阅细则。")
    monkeypatch.setattr(semantic, "get_embedder", lambda: _TopicEmbedder([["数学建模", "竞赛"]]))

    profile = {"education": "", "interests": ["竞赛"], "college": ""}
    briefing = asyncio.run(radar.inbox_briefing("u", profile))

    assert len(briefing["events"]) == 1  # 两个关键词分组被语义合并为一个事件
    assert briefing["events"][0]["notification_count"] == 2


def test_relevant_events_ranks_semantically(tmp_path, monkeypatch):
    store = PersonalStore(str(tmp_path / "retrieve.db"))
    radar = CampusRadar(store, robots=_AllowRobots())
    _save_event(radar, "https://n.example/x", "国家奖学金申请开始", "本科生请提交申请材料。")
    _save_event(radar, "https://n.example/y", "图书馆数据库讲座", "周五下午图书馆见。")
    monkeypatch.setattr(semantic, "get_embedder", lambda: _TopicEmbedder([["奖学金", "奖助学金"], ["讲座"]]))

    profile = {"education": "", "interests": ["奖学金"], "college": ""}
    events = asyncio.run(radar.relevant_events("u", profile, "奖助学金评定通知"))

    assert [e["title"] for e in events] == ["国家奖学金申请开始"]  # 换一种说法也能语义召回


def test_relevant_events_falls_back_to_substring_without_embedder(tmp_path, monkeypatch):
    store = PersonalStore(str(tmp_path / "substring.db"))
    radar = CampusRadar(store, robots=_AllowRobots())
    _save_event(radar, "https://n.example/z", "国家奖学金申请开始", "请提交材料。")
    monkeypatch.setattr(semantic, "get_embedder", lambda: None)

    events = asyncio.run(radar.relevant_events("u", {}, "奖学金"))
    assert len(events) == 1
    assert asyncio.run(radar.relevant_events("u", {}, "留学交流")) == []


# ── 步骤 3：LLM 简报（指纹缓存）───────────────────────────────────────────────


def _overview(**overrides):
    base = {
        "date": "2026-09-07",
        "weekday": "周一",
        "week_num": 1,
        "courses": [{"start_time": "08:30", "end_time": "10:05", "course": "高数", "location": "B-101"}],
        "todos": [{"id": 1, "content": "交实验报告"}],
        "upcoming": [{"id": 2, "content": "奖学金申请", "due_at": "2026-09-10", "status": "还剩3天", "kind": "ddl", "days_left": 3}],
        "reminders": [{"id": 2, "content": "奖学金申请", "label": "3 天内到期", "level": "important"}],
    }
    base.update(overrides)
    return base


def test_today_briefing_cache_and_regeneration(tmp_path):
    gateway = _FakeGateway(lambda messages, system: "今天上午有高数课，奖学金申请 3 天后截止，建议今天完成实验报告。")
    service = BriefingService(client=object(), model="m", gateway=gateway, store=PersonalStore(str(tmp_path / "b.db")))
    overview = _overview()

    first = asyncio.run(service.today_briefing("u", overview, []))
    assert first["available"] and not first["cached"]
    assert gateway.calls and "高数" in gateway.calls[0]["messages"][0]["content"]

    second = asyncio.run(service.today_briefing("u", overview, []))
    assert second["cached"] is True  # 数据未变 → 命中缓存，不再调用 LLM
    assert len(gateway.calls) == 1

    changed = asyncio.run(service.today_briefing("u", _overview(todos=[{"id": 9, "content": "新待办"}]), []))
    assert changed["cached"] is False  # 输入指纹变化 → 重新生成
    assert len(gateway.calls) == 2


def test_today_briefing_empty_day_skips_llm():
    gateway = _FakeGateway(lambda messages, system: "不应被调用")
    service = BriefingService(client=object(), model="m", gateway=gateway, store=None)
    empty = _overview(courses=[], todos=[], upcoming=[], reminders=[])
    result = asyncio.run(service.today_briefing("u", empty, []))
    assert result["available"] and "没有课程" in result["text"]
    assert gateway.calls == []


def test_briefing_service_without_llm_is_unavailable(tmp_path):
    service = BriefingService(store=PersonalStore(str(tmp_path / "n.db")))
    result = asyncio.run(service.today_briefing("u", _overview(), []))
    assert result == {"available": False, "reason": "LLM 未配置"}


def test_inbox_narrative_generation(tmp_path):
    gateway = _FakeGateway(lambda messages, system: "奖学金申请即将截止，请优先处理；讲座信息可自行安排时间了解。")
    service = BriefingService(client=object(), model="m", gateway=gateway, store=None)
    result = asyncio.run(
        service.inbox_narrative(
            "u",
            {
                "today_focus": [{"title": "国家奖学金申请", "deadline": "2026-09-10"}],
                "recommended": [{"title": "图书馆讲座", "category": "campus_life", "deadline": ""}],
            },
        )
    )
    assert result["available"]
    assert "奖学金" in result["text"]


# ── 步骤 4：行动步骤生成 + 逐条对原文校验 ─────────────────────────────────────


def _steps_gateway(steps):
    import json

    return _FakeGateway(lambda messages, system: json.dumps({"steps": steps}, ensure_ascii=False))


def test_plan_steps_drops_steps_without_verbatim_evidence():
    body = "请登录奖学金系统在线填写申请表，并于9月15日前将纸质材料交至学生工作处。"
    gateway = _steps_gateway(
        [
            {"step": "登录奖学金系统在线填写申请表", "evidence": "请登录奖学金系统在线填写申请表"},
            {"step": "把纸质材料交到学生工作处", "evidence": "在月球表面完成材料提交"},  # 依据不在原文
            {"step": "给院长写感谢信", "evidence": ""},  # 无依据
        ]
    )
    extractor = CampusEventExtractor(client=object(), model="m", gateway=gateway)
    steps = asyncio.run(extractor.plan_steps("奖学金申请", body, []))
    assert [s["step"] for s in steps] == ["登录奖学金系统在线填写申请表"]


def test_plan_steps_dedupes_known_steps_and_requires_llm():
    gateway = _steps_gateway([{"step": "准备成绩证明", "evidence": "准备成绩证明和获奖材料"}])
    extractor = CampusEventExtractor(client=object(), model="m", gateway=gateway)
    steps = asyncio.run(extractor.plan_steps("奖学金申请", "请准备成绩证明和获奖材料。", ["准备成绩证明"]))
    assert steps == []  # 与既有步骤重复 → 不产出

    assert asyncio.run(CampusEventExtractor().plan_steps("t", "b", [])) == []  # 无 LLM → 空


def test_create_action_plan_appends_validated_llm_steps(tmp_path):
    store = PersonalStore(str(tmp_path / "plan.db"))
    body = (
        "本科生国家奖学金申请，截止 2026年9月15日。请登录奖学金系统在线填写申请表，"
        "并将纸质材料交至学生工作处。"
    )
    radar = CampusRadar(
        store,
        robots=_AllowRobots(),
        extractor=CampusEventExtractor(
            client=object(),
            model="m",
            gateway=_steps_gateway(
                [
                    {"step": "登录奖学金系统在线填写申请表", "evidence": "请登录奖学金系统在线填写申请表"},
                    {"step": "编造一个原文没有的步骤", "evidence": "原文里根本不存在这句话"},
                ]
            ),
        ),
    )
    _save_event(
        radar,
        "https://n.example/plan",
        "国家奖学金申请",
        body,
        deadline="2026-09-15",
        targets=["本科生"],
        actions=["提交申请"],
        materials=["成绩证明"],
    )
    personal = PersonalService(store)
    event = asyncio.run(radar.get_event(1))
    plan = asyncio.run(radar.create_action_plan("u", event["id"], personal))

    contents = [item["content"] for item in plan["items"]]
    assert "准备成绩证明" in contents and "提交申请" in contents  # 确定性步骤仍在
    assert "登录奖学金系统在线填写申请表" in contents  # 有原文依据的 LLM 步骤被采纳
    assert "编造一个原文没有的步骤" not in contents  # 无有效依据的步骤被丢弃
    llm_item = next(item for item in plan["items"] if item["content"] == "登录奖学金系统在线填写申请表")
    assert "奖学金系统" in llm_item["evidence"]  # 溯源字段入库
    assert plan["llm_steps"] == 1


def test_create_action_plan_without_llm_keeps_legacy_behavior(tmp_path):
    store = PersonalStore(str(tmp_path / "legacy.db"))
    radar = CampusRadar(store, robots=_AllowRobots())
    _save_event(radar, "https://n.example/legacy", "讲座报名", "请报名参加。", deadline="2026-09-20")
    personal = PersonalService(store)
    event = asyncio.run(radar.get_event(1))
    plan = asyncio.run(radar.create_action_plan("u", event["id"], personal))
    assert plan["items"][0]["content"] == "讲座报名"  # 无材料/行动 → 标题兜底，行为不变
    assert plan["llm_steps"] == 0


# ── 步骤 5：课表文本解析 + 空档建议 ───────────────────────────────────────────


def test_schedule_parser_validates_llm_rows():
    gateway = _FakeGateway(
        lambda messages, system: (
            '{"courses": ['
            '{"course": "高等数学", "day_of_week": 0, "start_time": "08:00", "end_time": "09:40", "location": "B-101", "weeks": [1,2,3]},'
            '{"course": "坏行", "day_of_week": 7, "start_time": "08:00", "end_time": "09:40"},'
            '{"course": "时间倒挂", "day_of_week": 1, "start_time": "15:00", "end_time": "14:00"},'
            '{"course": "周次越界", "day_of_week": 2, "start_time": "10:00", "end_time": "11:40", "weeks": [99]}'
            "]}"
        )
    )
    parser = ScheduleTextParser(client=object(), model="m", gateway=gateway)
    result = asyncio.run(parser.parse("教务系统复制的课表文本"))
    assert result["parser"] == "llm"
    assert len(result["courses"]) == 2  # 非法星期/时间倒挂的行被拒
    assert result["courses"][0]["weeks"] == "1-3"
    assert result["courses"][1]["weeks"] == []  # 周次越界被过滤 → 全周
    assert result["skipped"] == 2


def test_schedule_parser_repairs_truncated_llm_output():
    """max_tokens 截断的 JSON 应抢救完整的课程行（截断对象丢弃），而不是整表回退规则。"""
    truncated = '{"courses": [{"course": "高等数学", "day_of_week": 0, "start_time": "08:00", "end_time": "09:40"}, {"course": "大学英语", "day_of_week": 2, "start_time": "10:00", "end'
    gateway = _FakeGateway(lambda messages, system: truncated)
    parser = ScheduleTextParser(client=object(), model="m", gateway=gateway)
    result = asyncio.run(parser.parse("课表文本"))
    assert result["parser"] == "llm"
    assert [c["course"] for c in result["courses"]] == ["高等数学"]  # 完整行救回，残缺行丢弃


def test_schedule_parser_rule_fallback_without_llm():
    parser = ScheduleTextParser()
    result = asyncio.run(parser.parse("大学英语 周三 14:00-15:40 东楼201\n这行没有课表要素"))
    assert result["parser"] == "rule"
    assert len(result["courses"]) == 1
    course = result["courses"][0]
    assert course["course"].startswith("大学英语")
    assert course["day_of_week"] == 2 and course["start_time"] == "14:00"


def test_free_time_advice_validates_against_real_todos_and_periods():
    gateway = _FakeGateway(
        lambda messages, system: (
            "```json\n"
            '{"suggestions": ['
            '{"todo_id": 1, "start": "14:00", "end": "15:00", "why": "DDL 最紧"},'
            '{"todo_id": 99, "start": "15:00", "end": "15:30", "why": "不存在的待办"},'
            '{"todo_id": 2, "start": "20:00", "end": "21:00", "why": "落在课内"},'
            '{"todo_id": 1, "start": "15:00", "end": "15:30", "why": "重复待办"}'
            "]}\n```"
        )
    )
    service = BriefingService(client=object(), model="m", gateway=gateway, store=None)
    todos = [
        {"id": 1, "content": "提交奖学金申请", "kind": "ddl", "due_at": "2026-09-10"},
        {"id": 2, "content": "预习高数", "kind": "todo", "due_at": ""},
    ]
    result = asyncio.run(
        service.free_time_advice("u", "2026-09-07", [{"start_time": "14:00", "end_time": "16:00"}], todos, [])
    )
    assert result["available"]
    assert result["suggestions"] == [
        {"todo_id": 1, "content": "提交奖学金申请", "due_at": "2026-09-10", "start": "14:00", "end": "15:00", "why": "DDL 最紧"}
    ]


def test_free_time_advice_without_candidates_is_unavailable():
    service = BriefingService(client=object(), model="m", gateway=_FakeGateway(lambda m, s: "{}"), store=None)
    result = asyncio.run(service.free_time_advice("u", "2026-09-07", [{"start_time": "14:00", "end_time": "16:00"}], [], []))
    assert result["available"] is False
