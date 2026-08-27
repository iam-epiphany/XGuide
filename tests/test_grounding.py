"""Hybrid Claim-aware Grounding 单元测试（core/grounding.py）。

确定性设计：
  - embedder 用「角度表」伪替身（cos(a,b) = cos(angle_a - angle_b)），
    每个用例显式指定角度，cosine 完全可控，不触网；
  - Dice / Hard Guards / 事实性过滤 / Claim 拆分均为纯函数直测；
  - Entailment Judge 用可记录调用次数的 FakeJudge 验证批量语义。

覆盖：原子事实拆分、事实性过滤、全候选匹配（Top-K/组合分/诱饵）、
Hard Guards（金额/数字/百分比/日期/时间/星期/否定反义/作用域）、
Guard 覆盖高 cosine、Claim 级标注、模型 [n] 剥离、无证据不加引用、
非事实句 skip、批量 Judge、Judge fail-open、Trace 字段。
"""
from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import core.grounding as g

# ── 确定性伪 embedder ─────────────────────────────────────────────────────────


class _FakeEmbedder:
    """角度表驱动的伪嵌入：文本 → (cos a, sin a)，余弦 = cos(Δ角)。"""

    def __init__(self, angles=None, default_angle=0.0):
        self._angles = dict(angles or {})
        self._default = default_angle

    def embed_documents(self, texts):
        out = []
        for t in texts:
            a = self._angles.get(t, self._default)
            out.append([math.cos(a), math.sin(a)])
        return out


def angle_for_cos(c: float) -> float:
    return math.acos(max(-1.0, min(1.0, c)))


def _run(coro):
    return asyncio.run(coro)


def _embed(angles):
    return patch("mcp.embeddings.get_embedder", return_value=_FakeEmbedder(angles))


def _ev(title: str, content: str) -> dict:
    return {"title": title, "content": content}


# ── Claim 拆分 ────────────────────────────────────────────────────────────────


def test_split_claims_multi_fact():
    clauses = g.split_claims("补办校园卡需要身份证，费用为 20 元。")
    assert [c for c, _ in clauses] == ["补办校园卡需要身份证", "费用为 20 元。"]


def test_split_claims_rebuild_exact():
    sent = "补办校园卡需要身份证 ，费用为 20 元。"
    assert "".join(c + s for c, s in g.split_claims(sent)) == sent


def test_split_claims_keeps_thousand_separator():
    clauses = g.split_claims("价格 1,200 元，含税。")
    assert len(clauses) == 2
    assert clauses[0][0] == "价格 1,200 元"


def test_split_claims_keeps_dunhao_list():
    # 顿号不拆：周一、周三开放 是同一事实（星期列表），拆开会丢谓语
    assert g.split_claims("周一、周三开放") == [("周一、周三开放", "")]


def test_split_claims_keeps_quoted_commas():
    # 引号内逗号不拆分（引用语是整体），且标注后无损重建
    sent = "通知说“周一，周三开放”，办理需预约。"
    clauses = g.split_claims(sent)
    assert [c for c, _ in clauses] == ["通知说“周一，周三开放”", "办理需预约。"]
    assert "".join(c + s for c, s in clauses) == sent


# ── 事实性过滤 ────────────────────────────────────────────────────────────────


def test_is_factual_claim_skips_non_factual():
    for s in ("如果需要我可以继续查询", "建议提前确认最新通知",
              "祝您生活愉快", "以上是查询结果", "我帮你总结一下",
              "如有疑问请咨询教务处", "具体请以官方通知为准"):
        assert g.is_factual_claim(s) is False, s


def test_is_factual_claim_keeps_factual():
    for s in ("补办校园卡需要身份证", "费用为 20 元", "行政楼周日开放",
              "办理地点在师生服务大厅", "开放时间为周一至周五 9:00-17:00"):
        assert g.is_factual_claim(s) is True, s


# ── Hard Consistency Guards ───────────────────────────────────────────────────


def test_guard_money():
    assert g.check_hard_consistency("补办费用 20 元", "补办费用 200 元")["conflict"]
    assert not g.check_hard_consistency("补办费用 20 元", "补办费用 20 元")["conflict"]
    # 子集：claim 的值出现在证据中即通过（"费用 20 元" vs "费用 20 元，押金 200 元"）
    assert not g.check_hard_consistency("补办费用 20 元", "补办费用 20 元，押金 200 元")["conflict"]


def test_guard_percent_and_discount():
    assert g.check_hard_consistency("通过率 95%", "通过率 90%")["conflict"]
    # 折扣归一：9 折 == 90%，九五折 == 95%
    assert not g.check_hard_consistency("全场 9 折", "全场 90%")["conflict"]
    assert not g.check_hard_consistency("全场九五折", "全场 95%")["conflict"]


def test_guard_money_wan_unit():
    # 万元归一：2 万元 == 20000 元
    assert not g.check_hard_consistency("费用 2 万元", "费用 20000 元")["conflict"]
    assert g.check_hard_consistency("费用 2 万元", "费用 2 元")["conflict"]


def test_guard_bare_number():
    assert g.check_hard_consistency("费用为 20", "费用为 200")["conflict"]
    # 双方多数字时不比较裸数字（避免噪声）
    assert not g.check_hard_consistency("共 3 个食堂", "食堂 3 个，另 2 个窗口")["conflict"]


def test_guard_date_interval():
    assert not g.check_hard_consistency(
        "图书馆 5 月 2 日闭馆", "图书馆 5 月 1 日至 5 月 3 日闭馆")["conflict"]
    assert g.check_hard_consistency(
        "图书馆 5 月 5 日闭馆", "图书馆 5 月 1 日至 5 月 3 日闭馆")["conflict"]
    # 区间必须相等或严格包含：claim [5-1,5-3] vs evidence [5-1,5-3]
    assert not g.check_hard_consistency(
        "图书馆 5 月 1 日至 5 月 3 日闭馆", "图书馆 5 月 1 日至 5 月 3 日闭馆")["conflict"]


def test_guard_time():
    assert not g.check_hard_consistency(
        "教务处 10:00 可办理业务", "教务处办公时间为 9:00-17:00")["conflict"]
    assert g.check_hard_consistency(
        "教务处办公时间为 9:00-18:00", "教务处办公时间为 9:00-17:00")["conflict"]
    # 12h/24h 归一：下午 3:00 == 15:00
    assert not g.check_hard_consistency("下午 3:00 下班", "15:00 下班")["conflict"]


def test_guard_weekday():
    assert g.check_hard_consistency("校园卡服务厅周六开放", "校园卡服务厅周日开放")["conflict"]
    assert not g.check_hard_consistency("行政楼周一开放", "行政楼周一至周五开放")["conflict"]
    assert not g.check_hard_consistency(
        "行政楼周一至周五开放", "行政楼开放时间为周一至周五")["conflict"]


def test_guard_negation():
    assert g.check_hard_consistency("行政楼周日开放", "行政楼周日不开放")["conflict"]
    assert g.check_hard_consistency("使用自习室需要预约", "使用自习室无需预约")["conflict"]
    assert g.check_hard_consistency("校园卡补办免费", "校园卡补办收费 20 元")["conflict"]


def test_guard_scoped_negation():
    # 周六开放 vs 周六休息（同一作用域反义）→ 硬冲突
    rec = g.check_hard_consistency(
        "校园卡服务厅周六开放", "校园卡服务厅周日开放，周六休息")
    assert rec["conflict"] and rec["level"] == "hard"
    # claim 笼统（未限定星期）vs evidence 带限定的休息 → 软信号（不硬拦截）
    rec = g.check_hard_consistency("图书馆开放", "图书馆开放，周末休息")
    assert rec["conflict"] is False and rec["level"] == "soft"
    assert "[polarity_soft]" in rec["reasons"][0]
    # claim 限定 vs evidence 笼统否定 → 硬冲突
    rec = g.check_hard_consistency("行政楼周日开放", "行政楼不开放")
    assert rec["conflict"] and rec["level"] == "hard"


def test_soft_guard_routes_high_confidence_to_judge():
    """高置信相似度 + 软信号：不直接 supported，交给 Judge 复核。"""
    claim = "图书馆开放"
    evs = [_ev("通知", "图书馆开放，周末休息。")]
    angles = {claim: 0.0, "图书馆开放，周末休息。": angle_for_cos(0.9)}  # cos 0.9 高置信
    with _embed(angles):
        rec = _run(g.decide_claim(claim, evs))  # 无 Judge → 兜底 insufficient
    assert rec["status"] == "insufficient"
    assert rec["soft_guard"] is True
    assert rec["fuzzy"] is True
    assert rec["citation"] is None
    # 有 Judge：由 Judge 决定（supported → 挂引用）
    judge = _FakeJudge({claim: {"verdict": "supported", "evidence_ids": [1]}})
    with _embed(angles):
        rec2 = _run(g.decide_claim(claim, evs, entailment_judge=judge))
    assert rec2["status"] == "supported"
    assert rec2["decision_source"] == "entailment"
    assert rec2["citation"] == 1


def test_guard_no_false_positive_on_consistent_texts():
    assert not g.check_hard_consistency("补办校园卡需携带本人身份证",
                                        "补办校园卡需携带本人身份证原件及复印件")["conflict"]


# ── 全证据候选匹配（P0-1）─────────────────────────────────────────────────────


def test_candidates_rank_by_combined_not_dice():
    """Dice 最大的 Evidence 不一定是语义最相关的——组合分决定排序。"""
    claim = "补办费用 20 元"
    evs = [
        _ev("A", "补办费用 20 元押金"),           # dice 高（词面近）
        _ev("B", "校园卡补办需交费用 20 元"),      # dice 略低但语义更相关
    ]
    angles = {
        claim: 0.0,
        "补办费用 20 元押金": angle_for_cos(0.30),
        "校园卡补办需交费用 20 元": angle_for_cos(0.70),
    }
    with _embed(angles):
        cands = _run(g.match_evidence_candidates(claim, evs))
    assert cands[0]["evidence_idx"] == 1  # B 组合分更高
    assert cands[0]["dice"] < cands[1]["dice"]  # 纯 Dice 排序会选 A


def test_candidates_topk():
    evs = [_ev(f"E{i}", f"内容 {i} 元") for i in range(4)]
    with _embed({}):
        cands = _run(g.match_evidence_candidates("补办费用 1 元", evs, top_k=3))
    assert len(cands) == 3
    assert {c["evidence_idx"] for c in cands} <= {0, 1, 2, 3}


# ── 决策：Guard 覆盖相似度 ────────────────────────────────────────────────────


def test_guard_beats_high_cosine_and_picks_second_candidate():
    """旧链路按 Dice 选 A（费用 200 元）→ 挂错引用；新链路 Guard 拦截 A。"""
    claim = "校园卡补办费用 20 元"
    evs = [
        _ev("旧通知", "校园卡补办费用为 200 元"),  # dice/cos 都更高（诱饵）
        _ev("最新通知", "补办费用为 20 元"),
    ]
    angles = {claim: 0.0, "校园卡补办费用为 200 元": angle_for_cos(0.95),
              "补办费用为 20 元": angle_for_cos(0.55)}
    with _embed(angles):
        rec = _run(g.decide_claim(claim, evs))
    assert rec["status"] == "supported"
    assert rec["selected_evidence_idx"] == 1  # 引用最新通知，不是旧通知
    assert rec["citation"] == 2
    assert rec["decision_source"] == "deterministic"
    assert rec["candidates"][0]["evidence_idx"] == 0  # 诱饵仍排在候选第 1
    assert rec["candidates"][0]["guard"]["conflict"]


def test_guard_blocks_supported_looking_pair():
    claim = "行政楼周日开放"
    evs = [_ev("通知", "行政楼周日不开放")]
    angles = {claim: 0.0, "行政楼周日不开放": angle_for_cos(0.9)}  # cos 0.9 也不能通过
    with _embed(angles):
        rec = _run(g.decide_claim(claim, evs))
    assert rec["status"] == "contradicted"
    assert rec["decision_source"] == "hard_guard"
    assert rec["citation"] is None


def test_decide_claim_insufficient_when_unrelated():
    angles = {"校园卡补办免费": 0.0, "选课时间在 9 月": angle_for_cos(0.05)}
    with _embed(angles):
        rec = _run(g.decide_claim("校园卡补办免费", [_ev("指南", "选课时间在 9 月")]))
    assert rec["status"] == "insufficient"
    assert rec["citation"] is None


def test_match_evidence_backcompat():
    with _embed({}):  # 默认角度相同 → 余弦 1.0
        idx, dice, cos = _run(g.match_evidence("补办费用 20 元", [_ev("A", "补办费用 20 元")]))
    assert idx == 0 and dice > 0.5 and cos == 1.0


def test_match_evidence_empty():
    with _embed({}):
        assert _run(g.match_evidence("任意", [])) == (-1, 0.0, 0.0)


# ── Claim 级标注（P1-1）───────────────────────────────────────────────────────


def test_annotate_claim_level_mixed():
    """多事实句子：只有被证据支持的 Claim 获得引用。"""
    answer = "补办校园卡需要身份证，费用为 20 元。"
    evs = [_ev("补办指南", "补办校园卡需携带本人身份证。")]
    angles = {
        "补办校园卡需要身份证": 0.0,
        "费用为 20 元。": angle_for_cos(0.05),  # key 必须与 claim 实际文本一致（含句号）
        "补办校园卡需携带本人身份证。": 0.0,
    }
    with _embed(angles):
        out = _run(g.annotate_citations(answer, evs))
    assert out["text"] == "补办校园卡需要身份证[1]，费用为 20 元。"
    assert out["citation_indices"] == [1]
    statuses = {r["claim"]: r["status"] for r in out["claims"]}
    assert statuses["补办校园卡需要身份证"] == "supported"
    assert statuses["费用为 20 元。"] == "insufficient"
    assert out["unsupported_sentences"] == ["费用为 20 元。"]


def test_annotate_strips_model_citations_and_keeps_in_range():
    # 模型自标 [3] 越界 → 剥离后重标 [1]（标注沿用句级风格，位于句尾标点之后）
    answer = "校园卡补办费用为 20 元[3]。"
    evs = [_ev("指南", "校园卡补办费用为 20 元。")]
    with _embed({"校园卡补办费用为 20 元。": 0.0}):
        out = _run(g.annotate_citations(answer, evs))
    assert out["text"] == "校园卡补办费用为 20 元。[1]"
    assert out["citation_indices"] == [1]


def test_annotate_no_evidence_no_citation():
    answer = "校园卡补办费用为 20 元。"
    out = _run(g.annotate_citations(answer, []))
    assert out["text"] == answer
    assert out["citation_indices"] == []
    assert out["claims"] == []


def test_annotate_skips_non_factual():
    answer = "如果需要我可以继续查询。"
    evs = [_ev("指南", "补办校园卡需携带本人身份证。")]
    with _embed({}):
        out = _run(g.annotate_citations(answer, evs))
    assert out["text"] == answer
    assert out["citation_indices"] == []
    assert out["unsupported_sentences"] == []  # 非事实句不计入 unsupported
    assert out["claims"][0]["status"] == "skipped"


def test_annotate_citation_indices_in_range():
    answer = "补办需身份证。费用为 20 元。"
    evs = [_ev("指南", "补办校园卡需携带本人身份证。"),
           _ev("收费", "补办费用为 20 元。")]
    angles = {"补办需身份证": 0.0, "费用为 20 元": 0.0,
              "补办校园卡需携带本人身份证。": 0.0, "补办费用为 20 元。": 0.0}
    with _embed(angles):
        out = _run(g.annotate_citations(answer, evs))
    assert out["citation_indices"] == [1, 2]
    assert all(1 <= i <= len(evs) for i in out["citation_indices"])


# ── Entailment Judge（P1-3）───────────────────────────────────────────────────


class _FakeJudge:
    """可记录调用（验证批量语义）的 Judge 替身。"""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    async def judge(self, claims, evidences):
        self.calls.append((list(claims), [e.get("content", "") for e in evidences]))
        return {c: self.results.get(c, {"verdict": "insufficient", "evidence_ids": []})
                for c in claims}


def _fuzzy_angles(claims, evs):
    angles = {}
    for c in claims:
        angles[c] = 0.0
    for ev in evs:
        angles[ev["content"]] = angle_for_cos(0.45)  # 模糊带 [0.40, 0.52)
    return angles


def test_judge_batch_single_call_and_applies_verdicts():
    answer = "支持线上补办。补办无需费用。"
    evs = [_ev("指南", "补办校园卡需携带本人身份证。"),
           _ev("线上", "线上办理入口在服务大厅。")]
    judge = _FakeJudge({
        "支持线上补办。": {"verdict": "supported", "evidence_ids": [2]},
        "补办无需费用。": {"verdict": "contradicted", "evidence_ids": []},
    })
    with _embed(_fuzzy_angles(["支持线上补办。", "补办无需费用。"], evs)):
        out = _run(g.annotate_citations(answer, evs, entailment_judge=judge))
    # 两个模糊 Claim 合并为一次 Judge 请求
    assert len(judge.calls) == 1
    assert set(judge.calls[0][0]) == {"支持线上补办。", "补办无需费用。"}
    # 标注沿用句级风格：位于 Claim 句尾标点之后
    assert out["text"] == "支持线上补办。[2]补办无需费用。"
    statuses = {r["claim"]: (r["status"], r["decision_source"]) for r in out["claims"]}
    assert statuses["支持线上补办。"] == ("supported", "entailment")
    assert statuses["补办无需费用。"] == ("contradicted", "entailment")


def test_judge_evidence_ids_out_of_range_dropped():
    """Judge 返回越界 evidence_ids → 不产生 Citation（索引必须落在真实范围内）。"""
    judge = _FakeJudge({"模糊陈述": {"verdict": "supported", "evidence_ids": [9]}})
    with _embed(_fuzzy_angles(["模糊陈述"], [_ev("A", "补办校园卡需携带本人身份证。")])):
        out = _run(g.annotate_citations("模糊陈述。", [_ev("A", "补办校园卡需携带本人身份证。")],
                                        entailment_judge=judge))
    assert out["citation_indices"] == []
    assert out["claims"][0]["status"] == "insufficient"


def test_judge_fail_open_on_exception():
    class _BoomJudge:
        async def judge(self, claims, evidences):
            raise RuntimeError("模型不可用")

    with _embed(_fuzzy_angles(["模糊陈述"], [_ev("A", "补办校园卡需携带本人身份证。")])):
        out = _run(g.annotate_citations("模糊陈述。", [_ev("A", "补办校园卡需携带本人身份证。")],
                                        entailment_judge=_BoomJudge()))
    assert out["citation_indices"] == []
    assert out["claims"][0]["status"] == "insufficient"


def test_judge_retries_once_on_parse_failure():
    """Judge 输出非法 JSON → 重试一次；第二次合法则正常消费。"""
    client = AsyncMock()
    client.messages.create = AsyncMock(side_effect=[
        SimpleNamespace(content=[SimpleNamespace(type="text", text="不是JSON")]),
        SimpleNamespace(content=[SimpleNamespace(type="text", text=(
            '{"decisions": [{"claim": "模糊陈述。", "verdict": "supported", '
            '"evidence_ids": [1]}]}'
        ))]),
    ])
    judge = g.LLMEntailmentJudge(client=client, model="m", enabled=True)
    out = _run(judge.judge(["模糊陈述。"], [_ev("A", "补办校园卡需携带本人身份证。")]))
    assert client.messages.create.await_count == 2  # 恰好重试一次
    assert out["模糊陈述。"]["verdict"] == "supported"
    assert out["模糊陈述。"]["evidence_ids"] == [1]


def test_judge_retry_exhausted_falls_back():
    """连续两次非法输出 → judge() 返回 {}（调用方 insufficient 兜底）。"""
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=SimpleNamespace(
        content=[SimpleNamespace(type="text", text="仍然不是JSON")],
    ))
    judge = g.LLMEntailmentJudge(client=client, model="m", enabled=True)
    out = _run(judge.judge(["模糊陈述。"], [_ev("A", "补办校园卡需携带本人身份证。")]))
    assert out == {}
    assert client.messages.create.await_count == 2


def test_fuzzy_without_judge_falls_back_to_insufficient():
    claim = "支持线上补办"
    evs = [_ev("指南", "补办校园卡需携带本人身份证。")]
    angles = {claim: 0.0, "补办校园卡需携带本人身份证。": angle_for_cos(0.45)}
    with _embed(angles):
        rec = _run(g.decide_claim(claim, evs))  # 无 Judge
    assert rec["status"] == "insufficient"
    assert rec["fuzzy"] is True
    assert rec["citation"] is None


# ── Trace（P1 增强）───────────────────────────────────────────────────────────


def test_grounding_trace_fields():
    evs = [_ev("指南", "补办校园卡需携带本人身份证。")]
    angles = {"补办校园卡需要身份证": 0.0, "费用为 20 元。": angle_for_cos(0.05),
              "补办校园卡需携带本人身份证。": 0.0}
    with _embed(angles):
        trace = _run(g.grounding_trace("补办校园卡需要身份证，费用为 20 元。", evs))
    assert trace["sentence_count"] == 1
    assert trace["claim_count"] == 2
    claim = trace["claims"][0]
    for key in ("claim", "factual", "candidates", "selected_evidence_idx",
                "best_dice", "best_cos", "hard_guard", "decision_source",
                "status", "citation", "fuzzy"):
        assert key in claim, key
    assert claim["candidates"][0]["guard"]["conflict"] is False
    assert claim["status"] == "supported"
    assert claim["citation"] == 1


def test_grounding_trace_no_evidence():
    trace = _run(g.grounding_trace("补办校园卡需要身份证。", []))
    assert trace["claim_count"] == 0
    assert trace["supported_ratio"] == 0.0
