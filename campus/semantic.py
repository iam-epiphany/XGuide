"""Inbox 语义层：本地 bge 嵌入的相关度加成 / 事件聚类合并 / 查询检索。

设计原则（与 extractor 的规则兜底哲学一致）：语义信号是增强而不是依赖。
嵌入模型不可用（首次下载失败 / onnxruntime 缺失 / 冷却期）时所有函数
返回空结果或 None，调用方保持原有关键词行为，链路不新增失败路径。

向量缓存：通知正文以 content_hash 为键缓存在进程内，重复打开 Inbox
或同一用户多次查询时不重复推理；画像向量按画像文本哈希缓存。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from mcp.embeddings import get_embedder

logger = logging.getLogger(__name__)

# bge-small-zh 余弦相似度校准（实测）：强相关对 0.63-0.75，同域不同题 0.46-0.53。
# 阈值取 0.58 —— 只有明确的语义相关才加分；0.45-0.58 区间是噪声带，宁缺毋滥
# （加成分会直接影响 Inbox 的 ≥2 准入阈值，误放行比漏召回代价更高）。
_STRONG_SIM = 0.58
_CLUSTER_SIM = 0.82  # ≥ 视为同一事件的系列通知（关键词聚类之外的补充合并）
_RETRIEVAL_SIM = 0.42  # 查询检索的召回底线（只影响排序召回，不改关键词准入）


def _enabled() -> bool:
    """Inbox 语义层总开关（ECHOGUIDE_INBOX_SEMANTIC=0 关闭，默认开启）。

    语义信号是增强而不是依赖：关闭或模型不可用时全部退化为原有关键词行为。
    """
    return os.getenv("ECHOGUIDE_INBOX_SEMANTIC", "1") != "0"

_DOC_CACHE: Dict[str, "np.ndarray"] = {}
_DOC_CACHE_MAX = 4096
_PROFILE_CACHE: Dict[str, "np.ndarray"] = {}


def _event_text(event: Dict[str, Any]) -> str:
    parts = [str(event.get("title") or ""), str(event.get("summary") or ""), str(event.get("event_type") or "")]
    return " ".join(part for part in parts if part.strip())[:1000]


def _profile_text(profile: Dict[str, Any]) -> str:
    parts = [
        str(profile.get("education") or ""),
        str(profile.get("college") or ""),
        str(profile.get("major") or ""),
        *[str(v) for v in (profile.get("interests") or [])],
    ]
    return " ".join(part for part in parts if part.strip())[:500]


def _cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 1e-9 else 0.0


def _doc_vectors(events: List[Dict[str, Any]], embedder: Any) -> Dict[int, "np.ndarray"]:
    """按 content_hash 缓存的通知向量；返回 {event_id: 向量}。"""
    texts: List[str] = []
    ids: List[int] = []
    keys: List[str] = []
    vectors: Dict[int, "np.ndarray"] = {}
    for event in events:
        key = str(event.get("content_hash") or f"id:{event.get('id')}")
        cached = _DOC_CACHE.get(key)
        if cached is not None:
            vectors[event["id"]] = cached
            continue
        texts.append(_event_text(event))
        ids.append(event["id"])
        keys.append(key)
    if not texts:
        return vectors
    try:
        computed = embedder.embed_documents(texts)
    except Exception as ex:
        logger.warning("通知向量计算失败（本次退化为纯关键词）: %s", ex)
        return vectors
    for event_id, key, vec in zip(ids, keys, computed):
        arr = np.asarray(vec, dtype=np.float32)
        vectors[event_id] = arr
        if len(_DOC_CACHE) >= _DOC_CACHE_MAX:
            _DOC_CACHE.pop(next(iter(_DOC_CACHE)))
        _DOC_CACHE[key] = arr
    return vectors


def _profile_vector(embedder: Any, profile: Dict[str, Any]) -> Optional["np.ndarray"]:
    text = _profile_text(profile)
    if not text:
        return None
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cached = _PROFILE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        vec = np.asarray(embedder.embed_query([text])[0], dtype=np.float32)
    except Exception as ex:
        logger.warning("画像向量计算失败（本次退化为纯关键词）: %s", ex)
        return None
    _PROFILE_CACHE[key] = vec
    if len(_PROFILE_CACHE) > 8:  # 简化 LRU：只缓存最近 8 份画像向量
        _PROFILE_CACHE.pop(next(iter(_PROFILE_CACHE)))
    return vec


def relevance_boost(
    events: List[Dict[str, Any]], profile: Dict[str, Any]
) -> Dict[int, Tuple[float, int, str]]:
    """画像与通知的语义相关加成：{event_id: (相似度, 加成分, 理由)}。

    只做加法不做减法：语义不相关不扣关键词分；只有强相似（≥0.58）才加分，
    避免嵌入器的同域噪声把不该进 Inbox 的通知顶过准入阈值。
    """
    if not _enabled():
        return {}
    embedder = get_embedder()
    if embedder is None or not events:
        return {}
    profile_vec = _profile_vector(embedder, profile)
    if profile_vec is None:
        return {}
    doc_vecs = _doc_vectors(events, embedder)
    result: Dict[int, Tuple[float, int, str]] = {}
    for event in events:
        vec = doc_vecs.get(event["id"])
        if vec is None:
            continue
        sim = _cosine(profile_vec, vec)
        if sim >= _STRONG_SIM:
            result[event["id"]] = (round(sim, 4), 3, "与你关注的方向语义相关")
    return result


def merge_similar_groups(groups: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    """关键词聚类之外的补充合并：主通知语义高度相似的关键词组合并为同一事件。

    输入输出都是保序 dict；被合并组的成员追加到目标组，顺序不变。
    嵌入模型不可用时原样返回。
    """
    if not _enabled():
        return groups
    embedder = get_embedder()
    if embedder is None or len(groups) < 2:
        return groups
    primaries = [members[0] for members in groups.values()]
    try:
        vectors = embedder.embed_documents([_event_text(event) for event in primaries])
    except Exception as ex:
        logger.warning("聚类向量计算失败（本次保持关键词分组）: %s", ex)
        return groups
    keys = list(groups.keys())
    kept: Dict[str, List[Dict[str, Any]]] = {}
    assigned: Dict[int, int] = {}  # 原组下标 → 保留组下标
    vec_list = [np.asarray(vec, dtype=np.float32) for vec in vectors]
    for i, key in enumerate(keys):
        target = next(
            (j for j in assigned if _cosine(vec_list[j], vec_list[i]) >= _CLUSTER_SIM), None
        )
        if target is None:
            assigned[i] = i
            kept[key] = groups[key]
        else:
            kept[keys[target]].extend(groups[key])
    if len(kept) != len(groups):
        logger.info("语义聚类合并：%d 组关键词分组合并为 %d 个事件", len(groups), len(kept))
    return kept


def rank_by_query(events: List[Dict[str, Any]], query: str) -> Optional[List[Dict[str, Any]]]:
    """按查询语义相似度对通知排序；嵌入模型不可用时返回 None（调用方回退子串匹配）。

    子串命中的通知获得固定加成（用户点名的关键词优先于纯语义），其余按
    相似度排序并过滤掉低于召回底线的通知。
    """
    if not _enabled():
        return None
    embedder = get_embedder()
    query = (query or "").strip()
    if embedder is None or not query or not events:
        return None
    try:
        query_vec = np.asarray(embedder.embed_query([query])[0], dtype=np.float32)
        doc_vecs = _doc_vectors(events, embedder)
    except Exception as ex:
        logger.warning("查询向量计算失败（本次退化为子串匹配）: %s", ex)
        return None
    query_ws = re.sub(r"\s+", "", query.lower())
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for event in events:
        vec = doc_vecs.get(event["id"])
        sim = _cosine(query_vec, vec) if vec is not None else 0.0
        substring = query_ws in re.sub(r"\s+", "", f"{event['title']} {event['summary']}".lower())
        score = sim + (0.5 if substring else 0.0)
        if substring or sim >= _RETRIEVAL_SIM:
            scored.append((score, event["id"], event))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [event for _, _, event in scored]
