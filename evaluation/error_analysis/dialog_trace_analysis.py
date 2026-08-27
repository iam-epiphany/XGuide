"""端到端回答 Grounding Trace 分析：定位 Faithfulness / Citation 低的根因。

方法（计划 §5.1，先 Trace 后改代码）：
  1. 跑对话用例的 Agent 生成（与 run_dialog_eval 同一 Orchestrator，跳过 Judge
     评分以省成本），记录每轮：question / answer / tool_evidence（真实使用的证据）。
  2. 用 core.grounding.grounding_trace 对回答做 claim-level 证据匹配
     （原子事实拆分 → 事实性过滤 → 全候选 Dice+bge 组合分 → Hard Guards →
     高置信直接判定；模糊区间无 Judge 时按 insufficient 兜底），得到逐 Claim
     的证据支持度、决策来源与最终状态（与生产链路同一套判定逻辑）。
  3. 解析回答中已有的 [n] 引用（模型自标引用）。
  4. 输出分类统计：
     情况 A：答案正确但无引用 → citation pipeline 问题
     情况 B：答案含证据不支持的句子 → generation grounding 问题
     情况 C：证据已检索但未被利用 → context format 问题

用法：
  python evaluation/error_analysis/dialog_trace_analysis.py [--smoke] [--out ...]

输出：data/eval/error_analysis/dialog_trace.json（每轮完整 trace + 汇总）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import statistics
import sys

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv()

from evaluation.cases import load_dialog_cases  # noqa: E402


def extract_citations(answer: str) -> list[int]:
    """解析回答中的 [n] 引用（剔除 Markdown 链接标签）。"""
    stripped = re.sub(r"\[[^\]]*\]\([^)]*\)", "", answer or "")
    return sorted({int(m) for m in re.findall(r"\[(\d+)\]", stripped)})


async def analyze_answer(answer: str, evidences: list[dict]) -> list[dict]:
    """用生产链路（core.grounding）对回答做 claim-level 证据匹配。"""
    from core.grounding import grounding_trace

    trace = await grounding_trace(answer, evidences)
    analyzed = []
    for claim in trace["claims"]:
        analyzed.append({
            "sentence": claim["claim"],
            "evidence_idx": claim["selected_evidence_idx"],
            "evidence_title": claim["selected_title"],
            "dice": claim["best_dice"],
            "cos": claim["best_cos"],
            "supported": claim["status"] == "supported",
            "status": claim["status"],
            "decision_source": claim["decision_source"],
            "factual": claim["factual"],
            "guard_reasons": claim["hard_guard"]["reasons"],
            "citation": claim["citation"],
        })
    return analyzed


async def run_trace(cases, smoke: bool) -> list[dict]:
    from agents.agent_orchestrator import Request as OrcReq
    from api import state

    state._build_runtime()
    orchestrator = state._orchestrator

    turns_trace = []
    for i, case in enumerate(cases):
        questions = case.get("turns") or [case.get("question")]
        questions = [str(q) for q in questions if str(q).strip()]
        conv_id = f"trace_{i}"
        user_id = "trace_user"
        history: list[dict] = []
        for turn_idx, question in enumerate(questions):
            orch_req = OrcReq(
                message=question,
                user_id=user_id,
                conv_id=conv_id,
                context="",
                history=history[-6:] if history else None,
            )
            orch_result = await orchestrator.run(orch_req)
            answer = orch_result.response
            evidences = list(getattr(orch_result, "tool_evidence", []) or [])
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer})

            analyzed = await analyze_answer(answer, evidences)
            citations = extract_citations(answer)
            n_sources = len(evidences)
            turns_trace.append({
                "case_id": case.get("id", f"case-{i}"),
                "turn": turn_idx,
                "question": question,
                "answer": answer,
                "n_sources": n_sources,
                "source_titles": [str(e.get("title", "")) for e in evidences],
                "citations_in_answer": citations,
                "citation_valid": all(1 <= c <= n_sources for c in citations),
                "has_citation": len(citations) > 0,
                "claims": analyzed,
                "supported_claim_ratio": round(
                    sum(1 for a in analyzed if a["supported"]) / len(analyzed), 4
                ) if analyzed else 0.0,
            })
            if smoke:
                break
        if smoke:
            break
    return turns_trace


def _reason_kind(reason: str) -> str:
    """从 [kind] 前缀提取 Guard reason 分类（无前缀归 other）。"""
    m = re.match(r"\[([a-z_]+)\]", reason or "")
    return m.group(1) if m else "other"


def summarize(turns_trace: list[dict]) -> dict:
    n = len(turns_trace)
    has_ev = [t for t in turns_trace if t["n_sources"] > 0]
    no_ev = [t for t in turns_trace if t["n_sources"] == 0]
    cited = [t for t in turns_trace if t["has_citation"]]
    valid_cited = [t for t in cited if t["citation_valid"]]
    unsupported_sents = [
        (t, a) for t in turns_trace for a in t["claims"] if not a["supported"]
    ]
    # Hard/Soft Guard 命中分类统计：定位哪种冲突在真实对话中最常见
    reason_kinds: dict[str, int] = {}
    for t in turns_trace:
        for a in t["claims"]:
            for r in a.get("guard_reasons", []):
                kind = _reason_kind(r)
                reason_kinds[kind] = reason_kinds.get(kind, 0) + 1
    return {
        "total_turns": n,
        "turns_with_evidence": len(has_ev),
        "turns_without_evidence": len(no_ev),
        "turns_with_any_citation": len(cited),
        "citation_rate": round(len(cited) / n, 4) if n else 0.0,
        "turns_with_valid_citation": len(valid_cited),
        "citation_correctness": round(len(valid_cited) / len(cited), 4) if cited else None,
        "avg_supported_claim_ratio": round(
            statistics.mean(t["supported_claim_ratio"] for t in has_ev), 4
        ) if has_ev else None,
        "unsupported_claim_count": len(unsupported_sents),
        "guard_reason_kinds": dict(sorted(reason_kinds.items(), key=lambda kv: -kv[1])),
        "unsupported_examples": [
            {"case_id": t["case_id"], "turn": t["turn"], "sentence": a["sentence"],
             "dice": a["dice"], "cos": a["cos"], "evidence_title": a["evidence_title"],
             "status": a["status"], "decision_source": a["decision_source"],
             "guard_reasons": a["guard_reasons"], "question": t["question"]}
            for t, a in unsupported_sents[:15]
        ],
        "no_evidence_turns": [
            {"case_id": t["case_id"], "turn": t["turn"], "question": t["question"]}
            for t in no_ev
        ],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="回答 Grounding Trace 分析")
    parser.add_argument("--smoke", action="store_true", help="只跑前 3 轮（联调）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    cases = load_dialog_cases()
    if args.smoke:
        cases = cases[:2]
    trace = await run_trace(cases, smoke=args.smoke)
    summary = summarize(trace)
    report = {"summary": summary, "turns": trace}

    out = args.out or ROOT / "data" / "eval" / "error_analysis" / "dialog_trace.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
