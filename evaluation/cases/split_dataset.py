"""数据集三向分割（train / dev / holdout = 60/20/20，固定 seed 可复现）。

计划 §6：新增评测体系，避免过拟合——训练/开发/留出三分，holdout 用例在
优化过程中不可见（本轮优化未针对任何用例做特化，holdout 用于验证泛化）。

划分策略（旧版纯随机 shuffle 的两个问题）：
  - intent：按期望标签分层——纯随机下稀有标签（other/feedback）可能整个
    缺席 dev/holdout，这两类的 per-class 指标无从谈起；
  - retrieval：按目标文档组划分——同义改写对共享同一组 relevant_titles，
    整组进同一划分，否则 holdout 的"泛化"被改写对泄漏撑高。

用法：
  python evaluation/cases/split_dataset.py            # 生成 intent + retrieval 三份文件
  python evaluation/cases/split_dataset.py --seed 42

输出（与 load_intent_cases / load_retrieval_cases 格式一致）：
  evaluation/cases/intent_cases_train/dev/holdout.json
  evaluation/cases/retrieval_cases_train/dev/holdout.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random

CASES_DIR = pathlib.Path(__file__).resolve().parent
RATIOS = (0.6, 0.2, 0.2)


def _allocate(groups: list[list], seed: int) -> tuple[list, list, list]:
    """把"不可跨划分的组"按组大小加权分配到三个桶，保持 60/20/20。

    仅用于 retrieval：同义改写对共享 relevant_titles，组不可拆。
    """
    rng = random.Random(seed)
    rng.shuffle(groups)
    total = sum(len(group) for group in groups)
    targets = [total * ratio for ratio in RATIOS]
    sizes = [0, 0, 0]
    buckets: tuple[list, list, list] = ([], [], [])
    for group in groups:
        idx = min(range(3), key=lambda i: sizes[i] / max(targets[i], 1e-9))
        buckets[idx].extend(group)
        sizes[idx] += len(group)
    for bucket in buckets:
        rng.shuffle(bucket)
    return buckets


def _split_intent(cases: list, seed: int = 42) -> tuple[list, list, list]:
    """按期望标签分层：每个标签内部按 60/20/20 比例切分。

    纯随机 shuffle 下稀有标签（other/feedback 各只有 5 条）可能整个缺席
    dev/holdout，这两类的 per-class 指标无从谈起。
    """
    rng = random.Random(seed)
    by_label: dict[str, list] = {}
    for case in cases:
        by_label.setdefault(str(case.get("expected")), []).append(case)

    buckets: tuple[list, list, list] = ([], [], [])
    for label in sorted(by_label):
        pool = by_label[label]
        rng.shuffle(pool)
        n = len(pool)
        n_train = round(n * RATIOS[0])
        n_dev = round(n * RATIOS[1])
        buckets[0].extend(pool[:n_train])
        buckets[1].extend(pool[n_train : n_train + n_dev])
        buckets[2].extend(pool[n_train + n_dev :])
    for bucket in buckets:
        rng.shuffle(bucket)
    return buckets


def _split_retrieval(cases: list, seed: int = 42) -> tuple[list, list, list]:
    """按目标文档组划分：改写对共享 relevant_titles，整组同划分防泄漏。"""
    groups: dict[frozenset, list] = {}
    for case in cases:
        groups.setdefault(frozenset(case.get("relevant_titles") or []), []).append(case)
    return _allocate(list(groups.values()), seed=seed)


def _save(name: str, payload: dict) -> None:
    path = CASES_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{path.name}: {len(payload['cases'])} 条")


def main() -> int:
    parser = argparse.ArgumentParser(description="数据集 train/dev/holdout 三分（intent 分层 / retrieval 分组）")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    intent_data = json.loads((CASES_DIR / "intent_cases.json").read_text(encoding="utf-8"))
    train, dev, holdout = _split_intent(intent_data["cases"], seed=args.seed)
    _save(
        "intent_cases_train",
        {
            "version": intent_data.get("version", "1.0"),
            "description": "intent 训练集（60%，按标签分层）",
            "cases": train,
        },
    )
    _save(
        "intent_cases_dev",
        {"version": intent_data.get("version", "1.0"), "description": "intent 开发集（20%，按标签分层）", "cases": dev},
    )
    _save(
        "intent_cases_holdout",
        {
            "version": intent_data.get("version", "1.0"),
            "description": "intent 留出集（20%，按标签分层，优化过程不可见）",
            "cases": holdout,
        },
    )

    retrieval_data = json.loads((CASES_DIR / "retrieval_cases.json").read_text(encoding="utf-8"))
    train, dev, holdout = _split_retrieval(retrieval_data["cases"], seed=args.seed)
    _save(
        "retrieval_cases_train",
        {
            "version": retrieval_data.get("version", "1.0"),
            "description": "retrieval 训练集（60%，按目标文档组划分）",
            "cases": train,
        },
    )
    _save(
        "retrieval_cases_dev",
        {
            "version": retrieval_data.get("version", "1.0"),
            "description": "retrieval 开发集（20%，按目标文档组划分）",
            "cases": dev,
        },
    )
    _save(
        "retrieval_cases_holdout",
        {
            "version": retrieval_data.get("version", "1.0"),
            "description": "retrieval 留出集（20%，按目标文档组划分，优化过程不可见）",
            "cases": holdout,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
