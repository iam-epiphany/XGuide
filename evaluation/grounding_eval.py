"""Grounding 评测与阈值标定（P0-3）：Claim-level Precision / Recall / F1。

评测集：evaluation/cases/grounding_cases.json（supported / unsupported /
skipped，覆盖直接引用、同义改写、词面相似但事实相反、数字错误、日期/时间
错误、否定关系、多证据候选、非事实句）。

用法：
  python evaluation/grounding_eval.py              # 真实 embedder（不可用自动降级）
  python evaluation/grounding_eval.py --no-embed   # 纯词面（dice-only，离线可跑）
  python evaluation/grounding_eval.py --grid       # 阈值网格标定 + F1 表
  python evaluation/grounding_eval.py --grid --apply  # 用最优阈值回写 grounding.py

指标口径：positive = supported。skipped 用例不计入 P/R/F1（Factuality
Filter 的误判单独报告）。核心目标：优先 Citation Precision —— 宁可少引用，
也不要给没有证据支持的事实挂引用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.grounding as g

CASES = ROOT / "evaluation" / "cases" / "grounding_cases.json"
GROUNDING_PY = ROOT / "core" / "grounding.py"

POSITIVE = "supported"


def load_cases() -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    return list(data["cases"])


def _predicted_status(rec: dict) -> str:
    """决策记录 → 评测口径状态（needs_judge 无 Judge 时按 insufficient）。"""
    if rec["status"] == "needs_judge":
        return "insufficient"
    return rec["status"]


def evaluate(cases: list[dict], results: list[dict]) -> dict:
    """按 expected（supported/unsupported）统计 TP/FP/FN/TN 与严格状态。"""
    tp = fp = fn = tn = 0
    skipped_ok = 0
    expected_skip_processed = []
    strict_ok = 0
    strict_total = 0
    citation_errors = []
    for case, rec in zip(cases, results):
        expected = case.get("expected", "unsupported")
        predicted = rec["predicted"]
        if expected == "skip":
            if predicted == "skipped":
                skipped_ok += 1
            else:
                expected_skip_processed.append(case["id"])
            continue
        if predicted == POSITIVE and expected == POSITIVE:
            tp += 1
        elif predicted == POSITIVE and expected != POSITIVE:
            fp += 1
        elif predicted != POSITIVE and expected == POSITIVE:
            fn += 1
        else:
            tn += 1
        exp_status = case.get("expected_status")
        if exp_status:
            strict_total += 1
            if rec["status"] == exp_status:
                strict_ok += 1
        exp_evidence = case.get("expected_evidence")
        if exp_evidence is not None and predicted == POSITIVE and rec.get("citation") != exp_evidence:
            citation_errors.append({"id": case["id"], "citation": rec.get("citation"),
                                    "expected": exp_evidence})
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "accuracy": round((tp + tn) / (tp + fp + fn + tn), 4) if tp + fp + fn + tn else None,
        "strict_ok": strict_ok, "strict_total": strict_total,
        "skipped_ok": skipped_ok,
        "expected_skip_processed": expected_skip_processed,
        "citation_errors": citation_errors,
        "fuzzy_count": sum(1 for r in results if r["fuzzy"]),
    }


def format_case_line(case: dict, rec: dict) -> str:
    return (
        f"  {case['id']:>6}  expected={case.get('expected', '-'):<11} "
        f"predicted={rec['predicted']:<12} status={rec['status']:<13} "
        f"source={rec['decision_source']:<11} dice={rec['best_dice']:.3f} "
        f"cos={rec['best_cos']:.3f} citation={rec.get('citation')}"
    )


async def run_default(cases: list[dict], use_embed: bool) -> list[dict]:
    """用当前常量跑全部用例（公开入口 decide_claim，无 Judge → 模糊兜底）。"""
    results = []
    for case in cases:
        claim, evidences = case["claim"], case.get("evidences") or []
        if use_embed:
            rec = await g.decide_claim(claim, evidences)
        else:
            from unittest.mock import patch

            with patch("mcp.embeddings.get_embedder", return_value=None):
                rec = await g.decide_claim(claim, evidences)
        rec["predicted"] = _predicted_status(rec)
        results.append(rec)
    return results


async def run_grid(cases: list[dict], use_embed: bool) -> list[dict]:
    """每个用例只算一次候选分数（Dice/cos/Guard 与阈值无关），供网格复用。"""
    results = []
    for case in cases:
        claim, evidences = case["claim"], case.get("evidences") or []
        if use_embed:
            cos = await g._batch_cosines_multi([claim], evidences)
            cos_scores = (cos or {}).get(claim, [0.0] * len(evidences))
        else:
            cos_scores = [0.0] * len(evidences)
        cands = await g.match_evidence_candidates(claim, evidences, cos_scores)
        active = [c for c in cands if c["guard"]["level"] != "hard"]
        results.append({"case": case, "best": active[0] if active else None,
                        "active": active, "claim": claim})
    return results


def grid_metrics(cases: list[dict], pre: list[dict], min_dice: float, min_cos: float) -> dict:
    tp = fp = fn = tn = 0
    for p in pre:
        case = p["case"]
        if case.get("expected") == "skip":
            continue
        expected = case.get("expected", "unsupported")
        verdict = g.decide_by_scores(p["best"], p["active"], p["claim"],
                                     min_dice=min_dice, min_cos=min_cos)
        predicted = verdict == POSITIVE
        if predicted and expected == POSITIVE:
            tp += 1
        elif predicted and expected != POSITIVE:
            fp += 1
        elif not predicted and expected == POSITIVE:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"min_dice": min_dice, "min_cos": min_cos, "tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1}


def print_report(cases: list[dict], results: list[dict], stats: dict, grid: list[dict] | None) -> None:
    print("\n=== 逐用例结果 ===")
    for case, rec in zip(cases, results):
        print(format_case_line(case, rec))
    print("\n=== 指标（positive = supported；skipped 不计入）===")
    print(f"  TP={stats['tp']} FP={stats['fp']} FN={stats['fn']} TN={stats['tn']}")
    print(f"  Precision={stats['precision']}  Recall={stats['recall']}  F1={stats['f1']}"
          f"  Accuracy={stats['accuracy']}")
    print(f"  严格状态匹配 {stats['strict_ok']}/{stats['strict_total']}；"
          f"模糊区间 Claim（无 Judge 兜底 insufficient）{stats['fuzzy_count']} 条")
    print(f"  skipped 用例正确处理 {stats['skipped_ok']}/{sum(1 for c in cases if c.get('expected') == 'skip')}；"
          f"应 skip 却被处理: {stats['expected_skip_processed'] or '无'}")
    if stats["citation_errors"]:
        print(f"  引用编号错误: {stats['citation_errors']}")
    else:
        print("  引用编号全部命中 expected_evidence")
    fps = [(c["id"], c["claim"]) for c, r in zip(cases, results)
           if r["predicted"] == POSITIVE and c.get("expected") not in (POSITIVE, "skip")]
    fns = [(c["id"], c["claim"]) for c, r in zip(cases, results)
           if r["predicted"] != POSITIVE and c.get("expected") == POSITIVE]
    print(f"\n  False Positive（误挂引用）: {fps or '无'}")
    print(f"  False Negative（漏挂引用）: {fns or '无'}")
    if grid:
        print("\n=== 阈值网格标定（F1）===")
        for row in sorted(grid, key=lambda r: -r["f1"])[:12]:
            flag = " ★当前" if (row["min_dice"] == g.MIN_DICE and row["min_cos"] == g.MIN_COS) else ""
            print(f"  min_dice={row['min_dice']:.2f} min_cos={row['min_cos']:.2f} "
                  f"→ P={row['precision']:.3f} R={row['recall']:.3f} F1={row['f1']:.3f}{flag}")


def apply_thresholds(best: dict) -> None:
    """把网格最优阈值回写 core/grounding.py 的 MIN_DICE/MIN_COS 常量。"""
    src = GROUNDING_PY.read_text(encoding="utf-8")
    src = re.sub(r"^MIN_DICE = .*$", f"MIN_DICE = {best['min_dice']:.2f}",
                 src, count=1, flags=re.M)
    src = re.sub(r"^MIN_COS = .*$", f"MIN_COS = {best['min_cos']:.2f}",
                 src, count=1, flags=re.M)
    GROUNDING_PY.write_text(src, encoding="utf-8")
    print(f"\n已回写 grounding.py：MIN_DICE={best['min_dice']:.2f} MIN_COS={best['min_cos']:.2f}（请人工 review 后提交）")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Grounding 评测与阈值标定")
    parser.add_argument("--no-embed", action="store_true", help="纯词面模式（不加载 embedder）")
    parser.add_argument("--grid", action="store_true", help="阈值网格标定")
    parser.add_argument("--apply", action="store_true", help="网格最优阈值回写 grounding.py（需 --grid）")
    args = parser.parse_args()

    cases = load_cases()
    use_embed = not args.no_embed

    results = await run_default(cases, use_embed)
    stats = evaluate(cases, results)
    grid = None
    if args.grid:
        pre = await run_grid(cases, use_embed)
        grid = []
        for md in (0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22):
            for mc in (0.44, 0.48, 0.52, 0.56, 0.60):
                grid.append(grid_metrics(cases, pre, md, mc))
        print_report(cases, results, stats, grid)
        # F1 相同时取更保守（更严格）的阈值；当前常量如果在最优平台则无需回写
        best = max(grid, key=lambda r: (r["f1"], r["min_dice"], r["min_cos"]))
        current_f1 = next(r["f1"] for r in grid
                          if r["min_dice"] == g.MIN_DICE and r["min_cos"] == g.MIN_COS)
        print(f"\n最优：min_dice={best['min_dice']:.2f} min_cos={best['min_cos']:.2f} "
              f"(F1={best['f1']:.3f})；当前常量 F1={current_f1:.3f}，"
              f"{'已处于最优平台，无需调整' if current_f1 == best['f1'] else '建议 --apply 回写'}")
        if args.apply and current_f1 < best["f1"]:
            apply_thresholds(best)
    else:
        print_report(cases, results, stats, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
