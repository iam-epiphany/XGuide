"""
答案 Grounding 层：Hybrid Claim-aware Grounding（引用标注与证据支持度判定）。

设计动机（可信 Agent / Grounded RAG 定位）：
  - 引用由执行层后置生成（与工具调用后置引用同一哲学）：回答生成后剥离
    模型自觉的 [n] → 按句拆分 → 原子事实拆分 → 事实性过滤 → 全证据候选
    匹配（Dice + bge 批量余弦）→ Hard Consistency Guards → 高置信直接
    判定 / 模糊 Claim 轻量蕴含判定 → 支持的 Claim 追加 [i]。索引永远落在
    证据范围内，无证据支持的 Claim 不加引用（暴露给 Verifier 做
    unsupported 标注）。

旧链路（sentence-level，v6）：
  逐句 → 先按 Dice 选唯一证据 → 只对该证据算 cosine → dice>=0.16 或
  cos>=0.52 即整句 supported。问题：Dice 最大的证据未必语义最相关；文本
  相似 ≠ 逻辑蕴含（"周日开放" vs "周日不开放"、"20 元" vs "200 元"）；整句
  一票制（一个多事实句子部分无证据仍整句被标记）。

新链路（claim-aware hybrid，v7）：
  Final Answer → Sentence Split → Atomic Claim Extraction → Factuality
  Filter → All-Evidence Candidate Matching（Dice + BGE 批量组合分，保留
  Top-K）→ Hard Consistency Guards（数字/金额/百分比/日期/时间/星期/否定
  反义）→ High-confidence direct decision / Low-confidence Entailment
  Judge（批量、结构化、fail-open）→ Citation Assignment → ResponseVerifier
  （职责不变，仍为出口两层）。

匹配阈值（bge 同构嵌入标定）：
  - min_dice=0.16 / min_cos=0.52：高置信直接支持（词面依据或语义等价）；
  - 模糊带 [fuzzy_min_dice, min_dice) / [fuzzy_min_cos, min_cos) 或
    多候选接近 / 政策类高风险事实 → 交给 Entailment Judge；无 Judge 时按
    insufficient 兜底（宁缺勿错，优先 Citation Precision）。
  - 权重与阈值均为模块级常量，供 evaluation/grounding_eval.py 网格标定。

Hard Guard 语义：与 embedding 相似度正交的确定性一致性检查，冲突时该
Claim 不得由该 Evidence 支持（相似度不能覆盖冲突）。

Entailment Judge：只处理模糊区间 Claim，一次回答中的多个模糊 Claim 合并
为一次请求（MAX_JUDGE_CLAIMS 条/批），结构化输出
  {"decisions": [{"claim": "...", "verdict": "supported|contradicted|insufficient",
                  "evidence_ids": [1, ...]}]}
evidence_ids 必须落在真实证据范围内。开关：
ECHOGUIDE_GROUNDING_ENTAILMENT=1（默认关闭，避免无配置时引入额外 LLM 成本）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from core.tracing import span

logger = logging.getLogger(__name__)

# ── 可标定常量（evaluation/grounding_eval.py --grid --apply 可回写）───────────

MIN_DICE = 0.16  # 高置信直接支持：词面（Dice）
MIN_COS = 0.52  # 高置信直接支持：语义（bge 余弦）
FUZZY_MIN_DICE = 0.10  # 模糊带下界：低于此直接 insufficient
FUZZY_MIN_COS = 0.40  # 模糊带下界：低于此直接 insufficient
CANDIDATE_TOP_K = 3  # 匹配后保留的候选 Evidence 数（通常 3~5 条全保留）
COMBINE_WEIGHT_DICE = 0.5  # 组合分权重（Dice）
COMBINE_WEIGHT_COS = 0.5  # 组合分权重（cosine）
TIE_EPS = 0.03  # Top-2 组合分差小于此值 → 多候选冲突，走 Judge
MAX_JUDGE_CLAIMS = 8  # 单次 Judge 请求的 Claim 批量上限
_POLICY_RISK_RE = re.compile(r"政策|规定|资格|条件|限制|截止|必须|要求|流程|办法|标准|条款")


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── 句子 / 原子事实拆分 ──────────────────────────────────────────────────────


def split_sentences(text: str) -> List[str]:
    """中文/英文混合句子拆分：按句末标点与换行切分，标点保留在句尾。"""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    return [p.strip() for p in parts if p.strip()]


def split_sentences_raw(text: str) -> List[str]:
    """保留原始分隔（换行/缩进）的句子拆分。

    与 split_sentences 的区别：不 strip、不丢空白部分——引用标注后按原样
    拼回，保证不破坏 Markdown 列表结构（"1. 甲。乙。" 这类列表项内部的
    句号不会把一项拆成两行，否则前端会把每个列表项渲染成独立 <ol>，
    序号全部从 1 重新开始）。
    """
    if not text:
        return []
    return [p for p in re.split(r"(?<=[。！？!?；;\n])", text) if p]


def split_claims(sentence: str) -> List[Tuple[str, str]]:
    """原子事实（Atomic Claim）拆分：按逗号/分号切分 clause。

    返回 [(claim 原文, 其后分隔符)]，拼接后与原句完全一致（标注后无损重建）：
      - 中文逗号/分号/ASCII 逗号是拆分点；顿号不拆——"周一、周三开放" 是
        同一个事实（星期列表），拆开会让 "周一" 失去谓语；
      - ASCII 逗号位于数字之间（"1,200"）不拆分（千分位）；
      - 引号（“”『』「」‘’""）内的逗号不拆分（引用语是整体）；
      - 引号不配对时保护不生效，回退为原行为。
    """
    if not sentence:
        return []
    protected, quotes = _protect_quotes(sentence)
    protected = re.sub(r"(?<=\d),(?=\d)", "\x00", protected)
    pieces = re.split(r"([，,；;])", protected)
    out: List[Tuple[str, str]] = []
    for i in range(0, len(pieces) - 1, 2):
        raw = pieces[i].replace("\x00", ",")
        out.append((_restore_quotes(raw, quotes), pieces[i + 1]))
    if pieces:
        last = pieces[-1].replace("\x00", ",")
        out.append((_restore_quotes(last, quotes), ""))
    return out


_QUOTE_PATTERNS = (r"“.*?”", r"『.*?』", r"「.*?」", r"‘.*?’", r'".*?"')


def _protect_quotes(text: str) -> Tuple[str, List[str]]:
    """把引号片段替换为占位符（\x01i\x01），拆分期间逗号不生效。"""
    spans: List[str] = []

    def _save(m: re.Match) -> str:
        spans.append(m.group(0))
        return f"\x01{len(spans) - 1}\x01"

    for pat in _QUOTE_PATTERNS:
        text = re.sub(pat, _save, text)
    return text, spans


def _restore_quotes(text: str, spans: List[str]) -> str:
    for i, s in enumerate(spans):
        text = text.replace(f"\x01{i}\x01", s)
    return text


# ── Factuality Filter（哪些 Claim 需要外部证据验证）──────────────────────────

# 礼貌语 / 建议语 / 过渡语 / 交互语 / 免责 / 模糊限定：命中即 skip。
# 注意：不把 "同时/另外/此外" 等过渡词放进黑名单——它们常与事实内容同句
# （"同时，补办需携带身份证"），误杀会丢引用。
_NON_FACTUAL_RE = re.compile(
    r"建议|如需|如果需要|如需要|欢迎|随时|祝|感谢|不客气|再见|您好|"
    r"我帮|我来|我可以|总结一下|以上就是|以上是|以上信息|以上内容|总而言之|总的来说|继续查询|进一步|"
    r"以下|如下|有任何问题|如有疑问|请咨询|请告知|以(官方|学校|最新|实际).{0,6}为准|"
    r"请以|仅供参考|可能|大概|也许|或许"
)
# 事实提示词：数字/金额/百分比/日期时间/星期/政策流程资格/地点材料等。
_FACTUAL_HINT_RE = re.compile(
    r"\d|元|块钱|人民币|rmb|%|百分之|折|年|月|日|号|点|"
    r"星期|周[一二三四五六日天]|工作日|周末|上午|下午|中午|晚上|"
    r"需要|须|必须|费用|收费|免费|办理|挂失|补办|申请|流程|条件|资格|"
    r"要求|开放|关闭|营业|上班|休息|闭馆|时间|地点|地址|材料|证件|携带|"
    r"规定|允许|禁止|预约|咨询|查询|截止|电话|邮箱|窗口|位于|提供|支持|"
    r"价格|号码|校区|教学楼|图书馆|食堂|宿舍"
)


def is_factual_claim(claim: str) -> bool:
    """判断 Claim 是否属于需要外部证据验证的事实性陈述。

    非事实性内容（礼貌/建议/过渡/交互语）直接跳过，不参与 Citation
    Matching，也不会计入 unsupported。默认 True：无黑名单命中且无事实
    提示词时按事实处理（宁可多验证，不因过滤漏掉真实陈述）。
    """
    text = claim or ""
    if _NON_FACTUAL_RE.search(text):
        return False
    return True


# ── 词面相似度（Dice，确定性，无外部依赖）───────────────────────────────────


def _bigrams(s: str) -> set:
    s = re.sub(r"[\s，。！？、,.!?：:；;\"'“”‘’（）()\[\]【】*#\-]", "", s)
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def dice_coef(a: str, b: str) -> float:
    """字符 2-gram Dice 系数（确定性词面重叠，无外部依赖）。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return 2.0 * len(ga & gb) / (len(ga) + len(gb))


# ── 语义相似度（bge 批量嵌入）────────────────────────────────────────────────


async def _batch_cosines(claim: str, evidences: List[Dict[str, Any]]) -> List[float]:
    """单 Claim × 全部 Evidence 的批量余弦（一次嵌入调用）。失败返回全 0。"""
    if not evidences:
        return []
    try:
        from mcp.embeddings import get_embedder

        embedder = get_embedder()
        if embedder is None:
            return [0.0] * len(evidences)
        texts = [claim[:500]] + [str(ev.get("content") or "")[:500] for ev in evidences]
        vecs = await asyncio.to_thread(embedder.embed_documents, texts)
        x, rest = vecs[0], vecs[1:]
        nx = sum(float(i) * float(i) for i in x) ** 0.5
        if not nx:
            return [0.0] * len(evidences)
        out = []
        for y in rest:
            ny = sum(float(i) * float(i) for i in y) ** 0.5
            if not ny:
                out.append(0.0)
                continue
            dot = sum(float(i) * float(j) for i, j in zip(x, y, strict=False))
            out.append(float(dot / (nx * ny)))
        return out
    except Exception:
        return [0.0] * len(evidences)


async def _batch_cosines_multi(
    claims: List[str],
    evidences: List[Dict[str, Any]],
) -> Optional[Dict[str, List[float]]]:
    """一次 batch 嵌入计算全部 claim × evidence 余弦（避免每对单独推理）。

    返回 {claim: [cos per evidence]}；embedder 不可用返回 None（调用方降级
    dice-only，此时组合分退化为 Dice 加权）。
    """
    if not claims or not evidences:
        return None
    try:
        from mcp.embeddings import get_embedder

        embedder = get_embedder()
        if embedder is None:
            return None
        texts = [c[:500] for c in claims] + [str(ev.get("content") or "")[:500] for ev in evidences]
        vecs = await asyncio.to_thread(embedder.embed_documents, texts)
        n = len(claims)
        claim_vecs, ev_vecs = vecs[:n], vecs[n:]
        ev_norms = [sum(float(i) * float(i) for i in v) ** 0.5 for v in ev_vecs]
        out: Dict[str, List[float]] = {}
        for c, cv in zip(claims, claim_vecs, strict=False):
            nc = sum(float(i) * float(i) for i in cv) ** 0.5
            if not nc:
                out[c] = [0.0] * len(evidences)
                continue
            row = []
            for v, nv in zip(ev_vecs, ev_norms, strict=False):
                if not nv:
                    row.append(0.0)
                    continue
                dot = sum(float(i) * float(j) for i, j in zip(cv, v, strict=False))
                row.append(float(dot / (nc * nv)))
            out[c] = row
        return out
    except Exception:
        return None


async def cosine_sim(a: str, b: str) -> float:
    """bge 同构嵌入余弦（向后兼容单对接口），embedder 不可用返回 0。"""
    try:
        from mcp.embeddings import get_embedder

        embedder = get_embedder()
        if embedder is None:
            return 0.0
        vecs = await asyncio.to_thread(embedder.embed_documents, [a, b[:500]])
        x, y = vecs[0], vecs[1]
        dot = sum(float(i) * float(j) for i, j in zip(x, y, strict=False))
        nx = sum(float(i) * float(i) for i in x) ** 0.5
        ny = sum(float(i) * float(i) for i in y) ** 0.5
        return float(dot / (nx * ny)) if nx and ny else 0.0
    except Exception:
        return 0.0


# ── Hard Consistency Guards（确定性事实一致性检查）────────────────────────────

_WEEKDAY_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_DAY_PERIODS = ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "白天", "深夜", "夜晚")

# 高风险实体（数字/金额/百分比/年份/月份/日号/日期/时间），按序优先消费，
# 被前面类别消费的数字不会落入裸数字 num。
_ENTITY_RE = re.compile(
    r"(?P<date>\d{1,2}\s*月\s*\d{1,2}\s*日)"
    r"|(?P<year>\d{4}\s*年)"
    r"|(?P<month>\d{1,2}\s*月)"
    r"|(?P<day>\d{1,2}\s*日)"
    r"|(?P<hm>\d{1,2}\s*[:：]\s*\d{2})"
    r"|(?P<hh>\d{1,2}\s*点(?:\s*半|\s*\d{1,2}\s*分)?)"
    r"|(?P<money>(?:￥|¥|RMB|rmb)?\s*\d+(?:\.\d+)?\s*万\s*(?:元|块钱|块)"
    r"|(?:￥|¥|RMB|rmb)?\s*\d+(?:\.\d+)?\s*(?:元|块钱|块))"
    r"|(?P<pct>百分之\s*[零一二两三四五六七八九十半]+|\d+(?:\.\d+)?\s*%|"
    r"[零一二两三四五六七八九十半]+\s*折|\d+(?:\.\d+)?\s*折)"
    r"|(?P<num>\d+(?:\.\d+)?)"
)

_DATE_RANGE_RE = re.compile(
    r"(?P<a>\d{1,2}\s*月\s*\d{1,2}\s*日)\s*(?:至|到|—|－|~|～|-)\s*(?P<b>\d{1,2}\s*月\s*\d{1,2}\s*日)"
)
_TIME_RANGE_RE = re.compile(
    r"(?P<a>(?:\d{1,2}\s*[:：]\s*\d{2})|(?:\d{1,2}\s*点(?:\s*半|\s*\d{1,2}\s*分)?))"
    r"\s*(?:至|到|—|－|~|～|-)\s*"
    r"(?P<b>(?:\d{1,2}\s*[:：]\s*\d{2})|(?:\d{1,2}\s*点(?:\s*半|\s*\d{1,2}\s*分)?))"
)
_WEEKDAY_WORD = r"(?:周|星期|礼拜)([一二三四五六日天])"
_WEEKDAY_RANGE_RE = re.compile(rf"{_WEEKDAY_WORD}\s*(?:至|到|—|－|~|～)\s*{_WEEKDAY_WORD}")
_WEEKDAY_SINGLE_RE = re.compile(_WEEKDAY_WORD)

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT_VALUES = {"十": 10, "百": 100, "千": 1000}


def _cn_num(s: str) -> Optional[float]:
    """中文数字转数值（支持 十/百/千 组合与折扣风格两位组合 "九五"=95）。"""
    s = (s or "").strip()
    if s == "半":
        return 0.5
    if s in _CN_DIGITS:
        return float(_CN_DIGITS[s])
    total, cur, has_unit = 0.0, None, False
    for ch in s:
        if ch in _CN_DIGITS:
            cur = float(_CN_DIGITS[ch])
        elif ch in _CN_UNIT_VALUES:
            has_unit = True
            total += (cur if cur is not None else 1.0) * _CN_UNIT_VALUES[ch]
            cur = None
        else:
            return None
    if cur is not None:
        total += cur
    if not has_unit and len(s) == 2:  # "九五"→95（折扣/百分比的两位写法）
        a, b = _CN_DIGITS.get(s[0]), _CN_DIGITS.get(s[1])
        if a is not None and b is not None:
            return float(a * 10 + b)
    return total


def _parse_date_point(s: str) -> Optional[float]:
    m = re.match(r"\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s or "")
    if not m:
        return None
    return float(int(m.group(1)) * 31 + int(m.group(2)))  # 绝对日号（月内近似）


def _parse_time_point(s: str) -> Optional[float]:
    s = (s or "").strip()
    m = re.match(r"(\d{1,2})\s*[:：]\s*(\d{2})", s)
    if m:
        return round(float(int(m.group(1))) + float(int(m.group(2))) / 60, 2)
    m = re.match(r"(\d{1,2})\s*点(?:\s*半|\s*(\d{1,2})\s*分)?", s)
    if m:
        h = float(int(m.group(1)))
        if "半" in s:
            return round(h + 0.5, 2)
        if m.group(2):
            return round(h + float(int(m.group(2))) / 60, 2)
        return round(h, 2)
    return None


def _extract_weekdays(text: str) -> Optional[set]:
    """提取星期集合：单点（周六）、范围（周一至周五）、工作日/周末。"""
    days: set = set()
    masked = _WEEKDAY_RANGE_RE.sub("  ", text)
    for m in _WEEKDAY_RANGE_RE.finditer(text):
        a, b = _WEEKDAY_CN[m.group(1)], _WEEKDAY_CN[m.group(2)]
        lo, hi = min(a, b), max(a, b)
        days.update(range(lo, hi + 1))
    for m in _WEEKDAY_SINGLE_RE.finditer(masked):
        days.add(_WEEKDAY_CN[m.group(1)])
    if "工作日" in text:
        days.update(range(1, 6))
    if "周末" in text:
        days.update({6, 7})
    return days or None


def _extract_entities(text: str) -> Dict[str, Any]:
    """抽取高风险事实实体（确定性，无外部依赖）。

    返回 {money/percent/num/year/month/day/date/time/weekday/period: [...],
          interval: {"date": [(s,e)...], "time": [(s,e)...]}}
    date 为「月*31+日」绝对日号；time 为 24h 小时小数（下午/晚上自动 +12）；
    interval 为 "5月1日至5月3日" / "9:00-17:00" 这类区间（先于单点消费）。
    """
    empty: Dict[str, List[Any]] = {
        "money": [],
        "percent": [],
        "num": [],
        "year": [],
        "month": [],
        "day": [],
        "date": [],
        "time": [],
        "weekday": [],
        "period": [],
    }
    text = text or ""
    if not text.strip():
        return {**empty, "interval": {"date_ranges": [], "time_ranges": []}}

    intervals: Dict[str, List[Tuple[float, float]]] = {"date_ranges": [], "time_ranges": []}
    masked = text

    def _mask_ranges(pattern, unit, parser):
        nonlocal masked
        for m in pattern.finditer(masked):
            a, b = parser(m.group("a")), parser(m.group("b"))
            if a is None or b is None:
                continue
            intervals[unit].append((min(a, b), max(a, b)))
        masked = pattern.sub(lambda m: " " * (m.end() - m.start()), masked)

    _mask_ranges(_DATE_RANGE_RE, "date_ranges", _parse_date_point)
    _mask_ranges(_TIME_RANGE_RE, "time_ranges", _parse_time_point)

    entities: Dict[str, List[Any]] = {k: [] for k in empty}
    for m in _ENTITY_RE.finditer(masked):
        if m.group("date"):
            d = _parse_date_point(m.group("date"))
            if d is not None:
                entities["date"].append(d)
        elif m.group("year"):
            entities["year"].append(float(re.sub(r"\s*年$", "", m.group("year"))))
        elif m.group("month"):
            entities["month"].append(float(re.sub(r"\s*月$", "", m.group("month"))))
        elif m.group("day"):
            entities["day"].append(float(re.sub(r"\s*日$", "", m.group("day"))))
        elif m.group("hm"):
            entities["time"].append(_parse_time_point(m.group("hm")))
        elif m.group("hh"):
            entities["time"].append(_parse_time_point(m.group("hh")))
        elif m.group("money"):
            raw = m.group("money")
            v = float(re.sub(r"[^\d.]", "", raw))
            if "万" in raw:
                v *= 10000  # "2 万元" → 20000，与 "20000 元" 可比较
            entities["money"].append(v)
        elif m.group("pct"):
            s = m.group("pct")
            if "百分之" in s:
                v = _cn_num(re.sub(r"^百分之", "", s))
            elif "%" in s:
                v = float(re.sub(r"[^\d.]", "", s))
            elif "折" in s:
                # 折扣转百分比："9折"=90%（单字/阿拉伯数字是折扣数 ×10）；
                # "九五折"=95%（两位中文直接是百分比写法）
                token = re.sub(r"\s*折$", "", s)
                cn = _cn_num(token)
                if cn is not None:
                    v = cn * 10 if len(token) == 1 else cn
                else:
                    v = float(re.sub(r"[^\d.]", "", s)) * 10
            else:
                v = None
            if v is not None:
                entities["percent"].append(round(v, 2))
        elif m.group("num"):
            entities["num"].append(float(m.group("num")))
    entities["weekday"] = sorted(_extract_weekdays(text) or [])
    entities["period"] = [p for p in _DAY_PERIODS if p in text]

    # 下午/晚上/傍晚的 12h 制时间 → 24h（"下午 3:00" == "15:00"）
    if any(p in text for p in ("下午", "晚上", "傍晚")):
        entities["time"] = [round(t + 12, 2) if t < 12 else t for t in entities["time"]]
    rounded = {k: list(values) if k == "period" else [round(v, 2) for v in values] for k, values in entities.items()}
    return rounded | {"interval": intervals}


def _eq(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < 1e-6


def _covered(v: float, points: List[float], ranges: List[Tuple[float, float]]) -> bool:
    if any(_eq(v, p) for p in points):
        return True
    return any(s <= v <= e for s, e in ranges)


def _interval_covered(
    a: float,
    b: float,
    points: List[float],
    ranges: List[Tuple[float, float]],
) -> bool:
    """Claim 区间被证据覆盖：与某证据区间完全相等，或严格包含于其内部。

    严格包含（s < a 且 b < e）避免 "9:00-17:00" vs "9:00-18:00" 这类
    端点不同的区间被误判为支持；退化为单点时走点覆盖规则。
    """
    if _eq(a, b):
        return _covered(a, points, ranges)
    return any((_eq(a, s) and _eq(b, e)) or (s < a and b < e) for s, e in ranges)


# 否定前缀与极性动词组：claim 与 evidence 对同一动词（或反义词）极性相反
# 且作用域（星期/时段/日期/时间）重叠时 → 冲突。
_NEG_PREFIX_AT_END = re.compile(r"(?:不|未|没|无|非|勿|别|不能|无法|禁止|不得|不予|无需|无须)\s*$")
_POLARITY_GROUPS: List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = [
    ("开放", ("开放",), ("休息", "闭馆", "关闭", "停开", "暂停")),
    ("营业", ("营业", "开门"), ("歇业", "停业", "关门")),
    ("上班", ("上班",), ("下班",)),
    ("通行", ("通行",), ("封闭", "禁行", "限行")),
    ("收费", ("收费",), ("免费",)),
    ("需要", ("需要",), ("无需", "不必")),
    ("允许", ("允许", "可以"), ("禁止", "严禁")),
    ("办理", ("办理",), ()),
    ("补办", ("补办",), ()),
    ("提供", ("提供",), ()),
    ("支持", ("支持",), ()),
    ("受理", ("受理",), ()),
    ("供应", ("供应",), ()),
    ("携带", ("携带",), ()),
    ("预约", ("预约",), ()),
]
_UNIT_LABELS = {
    "money": "金额",
    "percent": "百分比",
    "year": "年份",
    "month": "月份",
    "day": "日号",
    "weekday": "星期",
    "period": "时段",
}


def _polarity_mentions(text: str) -> List[Dict[str, Any]]:
    """按 clause 提取 (动词组, 极性, 作用域) 提及，作用域 = 该 clause 的
    星期/时段/日期/时间（无信息为 None/[]，即"不设防"）。"""
    mentions: List[Dict[str, Any]] = []
    for clause in re.split(r"[，,；;。！？!?]", text or ""):
        ent = _extract_entities(clause)
        scope = {
            "weekdays": _extract_weekdays(clause),
            "periods": set(ent["period"]) or None,
            "dates": ent["date"],
            "times": ent["time"],
            "date_ranges": ent["interval"]["date_ranges"],
            "time_ranges": ent["interval"]["time_ranges"],
        }
        for group, pos_verbs, neg_verbs in _POLARITY_GROUPS:
            for v in pos_verbs:
                for m in re.finditer(re.escape(v), clause):
                    negated = _NEG_PREFIX_AT_END.search(clause[: m.start()]) is not None
                    mentions.append(
                        {
                            "group": group,
                            "polarity": -1 if negated else 1,
                            "scope": scope,
                        }
                    )
            for v in neg_verbs:
                if re.search(re.escape(v), clause):
                    mentions.append(
                        {
                            "group": group,
                            "polarity": -1,
                            "scope": scope,
                        }
                    )
    return mentions


def _scope_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    """两提及作用域关系（a = claim 侧，b = evidence 侧），三态：

    - "overlap"：双方未限定该维，或双方都限定且相交，或 claim 限定而
      evidence 未限定（"周日开放" 被笼统的 "不开放" 直接否定 → 硬冲突）；
    - "soft"：claim 未限定该维而 evidence 限定了（"图书馆开放" 是笼统
      陈述，evidence "周末休息" 不构成直接矛盾，但需要 Judge 复核）；
    - "disjoint"：双方都限定且不相交（周六 vs 周日是不同事实，不冲突）。
    """
    soft = False
    for key in ("weekdays", "periods"):
        sa, sb = a[key], b[key]
        if sa is None and sb is None:
            continue
        if sa is None:
            soft = True
            continue
        if sb is None:
            continue
        if not (sa & sb):
            return "disjoint"
    for key, rkey in (("dates", "date_ranges"), ("times", "time_ranges")):
        pa, pb = a[key], b[key]
        ra, rb = a[rkey], b[rkey]
        a_any, b_any = bool(pa or ra), bool(pb or rb)
        if not a_any and not b_any:
            continue
        if not a_any:
            soft = True
            continue
        if not b_any:
            continue
        if not any(_covered(p, pb, rb) for p in pa):
            return "disjoint"
        for s, e in ra:
            if not _interval_covered(s, e, pb, rb):
                return "disjoint"
    return "soft" if soft else "overlap"


def _polarity_conflict(claim: str, evidence: str) -> Tuple[str, Optional[str]]:
    """否定/反义极性检查，返回 (level, reason)：

    - "hard"：作用域重叠（或双方未限定）的相反表述 → 直接拦截；
    - "soft"：claim 未限定作用域而 evidence 限定否定（"图书馆开放" vs
      "周末休息"）→ 不直接拦截，但标记为需要 Judge 复核；
    - "none"：作用域不相交（不同事实）或无共同动词。
    """
    cm, em = _polarity_mentions(claim), _polarity_mentions(evidence)
    saw_soft = False
    for c in cm:
        for e in em:
            if c["group"] != e["group"] or c["polarity"] != -e["polarity"]:
                continue
            rel = _scope_overlap(c["scope"], e["scope"])
            if rel == "overlap":
                return "hard", f"[polarity] 否定/反义冲突：claim 对「{c['group']}」表述与 evidence 相反"
            if rel == "soft":
                saw_soft = True
    if saw_soft:
        return "soft", "[polarity_soft] claim 未限定时间/星期等作用域，evidence 对该作用域否定——需 Judge 复核"
    return "none", None


def check_hard_consistency(claim: str, evidence: str) -> Dict[str, Any]:
    """确定性事实一致性检查（Hard / Soft Consistency Guards）。

    覆盖：金额/百分比/裸数字、年份/月份/日号、日期（月日，含区间）、时间
    （含区间与 12h/24h 归一）、星期（含范围）、时段、否定/反义表达。

    返回 {"conflict", "level", "reasons"}：
      - level = "hard"：确定性冲突（数字/日期/时间等事实不符，或作用域
        重叠的相反表述）→ 该 Claim 不得由该 Evidence 支持，embedding
        相似度不能覆盖；
      - level = "soft"：claim 笼统 vs evidence 限定否定的模糊信号 → 不
        直接拦截，但高置信相似度也会被路由到 Entailment Judge 复核；
      - level = "none"：无冲突。
    reasons 每条带 [kind] 前缀（money/percent/num/date/time/…/polarity/
    polarity_soft），供 trace 聚合统计。
    """
    hard_reasons: List[str] = []
    soft_reasons: List[str] = []
    ce, ee = _extract_entities(claim), _extract_entities(evidence)

    # 等值子集规则：claim 的每个值必须出现在证据中（如 "费用 20 元" 对
    # "费用 20 元，押金 200 元" 通过；对 "费用 200 元" 冲突）
    for unit in ("money", "percent", "year", "month", "day", "weekday", "period"):
        if not ce[unit] or not ee[unit]:
            continue
        if unit == "period":
            missing = [v for v in ce[unit] if v not in ee[unit]]
        else:
            missing = [v for v in ce[unit] if not any(_eq(v, x) for x in ee[unit])]
        if missing:
            hard_reasons.append(f"[{unit}] {_UNIT_LABELS[unit]}不一致：claim {ce[unit]} vs evidence {ee[unit]}")

    # 裸数字：双方都只出现一个数字时严格比较（避免多数字场景的噪声）
    if len(ce["num"]) == 1 and len(ee["num"]) == 1 and not _eq(ce["num"][0], ee["num"][0]):
        hard_reasons.append(f"[num] 数字不一致：claim {ce['num'][0]} vs evidence {ee['num'][0]}")

    # 日期/时间：claim 的点必须被证据的点或区间覆盖；claim 区间必须与某
    # 证据区间相等或严格包含于其内
    for unit, rkey in (("date", "date_ranges"), ("time", "time_ranges")):
        if not ce[unit] and not ce["interval"][rkey]:
            continue
        points, ranges = ee[unit], ee["interval"][rkey]
        missing = [v for v in ce[unit] if not _covered(v, points, ranges)]
        not_covered_iv = [iv for iv in ce["interval"][rkey] if not _interval_covered(iv[0], iv[1], points, ranges)]
        if missing or not_covered_iv:
            label = "日期" if unit == "date" else "时间"
            hard_reasons.append(f"[{unit}] {label}不一致：claim 超出 evidence 覆盖范围")

    pol_level, pol_reason = _polarity_conflict(claim, evidence)
    if pol_level == "hard":
        hard_reasons.append(pol_reason)
    elif pol_level == "soft":
        soft_reasons.append(pol_reason)

    level = "hard" if hard_reasons else ("soft" if soft_reasons else "none")
    return {"conflict": level == "hard", "level": level, "reasons": hard_reasons + soft_reasons}


# ── 全证据候选匹配（P0-1）────────────────────────────────────────────────────


def combined_score(dice: float, cos: float) -> float:
    """组合分：Dice 与 cosine 的加权和（权重为模块常量，可标定）。"""
    return COMBINE_WEIGHT_DICE * dice + COMBINE_WEIGHT_COS * cos


async def match_evidence_candidates(
    claim: str,
    evidences: List[Dict[str, Any]],
    cos_scores: Optional[List[float]] = None,
    top_k: int = CANDIDATE_TOP_K,
) -> List[Dict[str, Any]]:
    """全证据候选匹配：对每条 Evidence 同时计算 Dice + 余弦 + Hard Guard。

    cos_scores：与 evidences 等长的预计算余弦（回答级批量嵌入复用）；
    None 时内部一次 batch 嵌入补齐。返回按组合分降序的候选（保留 Top-K），
    每条含 evidence_idx / title / dice / cos / combined / guard。
    避免旧链路"先按 Dice 选唯一证据、再只对该证据算 cosine"的错选问题。
    """
    if not evidences:
        return []
    if cos_scores is None:
        cos_scores = await _batch_cosines(claim, evidences)
    cands: List[Dict[str, Any]] = []
    for i, ev in enumerate(evidences):
        content = str(ev.get("content") or "")
        dice = dice_coef(claim, content)
        cos = cos_scores[i] if i < len(cos_scores) else 0.0
        cands.append(
            {
                "evidence_idx": i,
                "title": str(ev.get("title") or ""),
                "dice": round(dice, 4),
                "cos": round(cos, 4),
                "combined": round(combined_score(dice, cos), 4),
                "guard": check_hard_consistency(claim, content),
            }
        )
    cands.sort(key=lambda c: (c["combined"], c["dice"], -c["evidence_idx"]), reverse=True)
    return cands[: max(1, top_k)]


# ── 决策：高置信直接判定 / 模糊 Claim Entailment Judge（P1-3）────────────────


def supported(dice: float, cos: float) -> bool:
    """高置信直接支持：词面依据或语义等价（任一达标）。"""
    return dice >= MIN_DICE or cos >= MIN_COS


def decide_by_scores(
    best: Optional[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    claim: str,
    *,
    min_dice: float = MIN_DICE,
    min_cos: float = MIN_COS,
    fuzzy_min_dice: float = FUZZY_MIN_DICE,
    fuzzy_min_cos: float = FUZZY_MIN_COS,
    tie_eps: float = TIE_EPS,
) -> str:
    """纯阈值决策（无 LLM，评测网格标定复用）：supported / needs_judge / insufficient。

    best = 未与证据冲突的最优候选；candidates = 全部未冲突候选。
    高置信直接 supported；模糊带 / 政策类高风险 / Top-2 组合分接近
    （多候选冲突）→ needs_judge；否则 insufficient。
    """
    if best is None:
        return "insufficient"
    if best["dice"] >= min_dice or best["cos"] >= min_cos:
        return "supported"
    if best["dice"] >= fuzzy_min_dice or best["cos"] >= fuzzy_min_cos:
        return "needs_judge"
    if _POLICY_RISK_RE.search(claim or "") and best["combined"] >= 0.05:
        return "needs_judge"
    if len(candidates) >= 2:
        top2 = sorted((c["combined"] for c in candidates), reverse=True)[:2]
        if top2[0] > 0.0 and abs(top2[0] - top2[1]) < tie_eps:
            return "needs_judge"
    return "insufficient"


def _base_record(claim: str) -> Dict[str, Any]:
    return {
        "claim": claim,
        "factual": True,
        "candidates": [],
        "selected_evidence_idx": -1,
        "selected_title": "",
        "best_dice": 0.0,
        "best_cos": 0.0,
        "hard_guard": {"conflict": False, "level": "none", "reasons": []},
        "decision_source": "deterministic",
        "status": "insufficient",
        "citation": None,
        "fuzzy": False,
        "soft_guard": False,
    }


def _skip_record(claim: str) -> Dict[str, Any]:
    rec = _base_record(claim)
    rec.update({"factual": False, "decision_source": "skip", "status": "skipped"})
    return rec


def _decide_from_candidates(
    claim: str,
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """候选 → 决策记录：Guard 过滤 → 高置信直接判定 / 软信号复核 / needs_judge。

    - level="hard" 的候选被过滤（相似度不能覆盖确定性冲突）；
    - 高置信（dice/cos 达阈值）但最优候选带 level="soft"（claim 笼统 vs
      evidence 限定否定）→ 不直接 supported，路由到 Entailment Judge；
    - 模糊区间 / 政策高风险 / 多候选接近 → needs_judge（无 Judge 时兜底）。
    """
    rec = _base_record(claim)
    rec["candidates"] = candidates
    active = [c for c in candidates if c["guard"]["level"] != "hard"]
    if not active:
        if candidates:
            # 全部候选被 Hard Guard 拦截 → contradicted（相似度不能覆盖）
            rec["hard_guard"] = candidates[0]["guard"]
            rec["best_dice"] = candidates[0]["dice"]
            rec["best_cos"] = candidates[0]["cos"]
            rec["status"] = "contradicted"
            rec["decision_source"] = "hard_guard"
        return rec
    best = active[0]
    rec.update(
        {
            "selected_evidence_idx": best["evidence_idx"],
            "selected_title": best["title"],
            "best_dice": best["dice"],
            "best_cos": best["cos"],
            "hard_guard": best["guard"],
        }
    )
    verdict = decide_by_scores(best, active, claim)
    if verdict == "supported":
        if best["guard"]["level"] == "soft":
            # 相似度通过但存在软信号（笼统 vs 限定否定）→ 交给 Judge 复核
            rec["status"] = "needs_judge"
            rec["fuzzy"] = True
            rec["soft_guard"] = True
        else:
            rec["status"] = "supported"
            rec["citation"] = best["evidence_idx"] + 1
    elif verdict == "needs_judge":
        rec["status"] = "needs_judge"
        rec["fuzzy"] = True
    return rec


def _apply_judge(
    record: Dict[str, Any],
    verdict_info: Optional[Dict[str, Any]],
    evidences: List[Dict[str, Any]],
) -> None:
    """把 Judge 结果落到记录上。verdict 缺失/非法 → insufficient 兜底
    （fail-open，不产生 Citation；supported 但无合法 evidence_ids 同样兜底）。"""
    record["decision_source"] = "entailment"
    record["fuzzy"] = False
    if not verdict_info:
        record["status"] = "insufficient"
        return
    verdict = str(verdict_info.get("verdict", "")).strip().lower()
    ids = verdict_info.get("evidence_ids") or []
    valid = [
        int(i)
        for i in ids
        if isinstance(i, int | float | str) and str(i).lstrip("-").isdigit() and 1 <= int(i) <= len(evidences)
    ]
    if verdict == "supported" and valid:
        idx = valid[0] - 1
        record["status"] = "supported"
        record["selected_evidence_idx"] = idx
        record["selected_title"] = str(evidences[idx].get("title") or "")
        record["citation"] = idx + 1
    elif verdict == "contradicted":
        record["status"] = "contradicted"
    else:
        record["status"] = "insufficient"


async def decide_claim(
    claim: str,
    evidences: List[Dict[str, Any]],
    entailment_judge: Optional[LLMEntailmentJudge] = None,
) -> Dict[str, Any]:
    """单个 Claim 的完整判定（评测/脚本/外部复用入口）。

    先做事实性过滤（非事实句 → skipped，不参与匹配、不计入 unsupported），
    再匹配 → Guard → 直接判定；fuzzy 且有 Judge 时走 Judge，否则 insufficient。
    """
    claim = (claim or "").strip()
    if not claim or not is_factual_claim(claim):
        return _skip_record(claim)
    candidates = await match_evidence_candidates(claim, evidences)
    rec = _decide_from_candidates(claim, candidates)
    if rec["status"] == "needs_judge":
        if entailment_judge is not None:
            verdicts = await entailment_judge.judge([claim], evidences)
            _apply_judge(rec, verdicts.get(claim), evidences)
        else:
            rec["status"] = "insufficient"
    return rec


# ── Entailment Judge（轻量蕴含判定，批量、结构化、fail-open）─────────────────


class LLMEntailmentJudge:
    """轻量蕴含判定器：只处理模糊区间 / 政策高风险 Claim。

    Judge 的任务是判断「Evidence 是否能蕴含 Claim」（supported /
    contradicted / insufficient），不是"文本像不像"。一次回答中的多个模糊
    Claim 合并为一次请求（最多 MAX_JUDGE_CLAIMS 条/批），evidence_ids
    必须落在真实证据范围内（1-based），越界一律丢弃。

    开关：ECHOGUIDE_GROUNDING_ENTAILMENT=1（默认关闭）。关闭 / client 不可
    用 / 调用或解析失败 → 返回 {}，调用方按 insufficient 兜底（fail-open，
    绝不因 Judge 故障产生 Citation）。
    """

    def __init__(self, client: Any = None, model: str = "", gateway: Any = None, enabled: Optional[bool] = None):
        self._client = client
        self._model = model
        self._gateway = gateway
        self._enabled = _env_bool("ECHOGUIDE_GROUNDING_ENTAILMENT", "0") if enabled is None else bool(enabled)

    async def judge(self, claims: List[str], evidences: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        if not self._enabled or self._client is None or not self._model or not claims:
            return {}
        results: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(claims), MAX_JUDGE_CLAIMS):
            chunk = claims[i : i + MAX_JUDGE_CLAIMS]
            try:
                results.update(await self._judge_chunk(chunk, evidences))
            except Exception as ex:
                logger.warning("Entailment Judge 调用失败，按 insufficient 兜底: %s", ex)
        return results

    async def _judge_chunk(
        self,
        claims: List[str],
        evidences: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        evidence_lines = "\n".join(
            f"{i + 1}. {ev.get('title', '')!s}：{str(ev.get('content', ''))[:400]}" for i, ev in enumerate(evidences)
        )[:4000]
        claim_lines = "\n".join(f"- {c}" for c in claims)
        system = (
            "你是事实蕴含判定器（Entailment Judge）。给定工具证据，逐条判定每个 claim：\n"
            "supported = 证据能直接蕴含该事实；\n"
            "contradicted = 证据与 claim 直接矛盾（数字、日期、时间、否定表述等不一致）；\n"
            "insufficient = 证据不足、无关或仅部分提及。\n"
            "规则：未被证据提及的细节一律 insufficient；宁可 insufficient 也不要强行 supported。\n"
            '只输出 JSON：{"decisions": [{"claim": "原样复述claim", '
            '"verdict": "supported|contradicted|insufficient", "evidence_ids": [1,2]}]}'
            "。evidence_ids 为证据编号（1-based），仅 supported 时填写。"
        )
        user = f"工具证据：\n{evidence_lines or '（无）'}\n\n待判定 claims：\n{claim_lines}"

        async with span("entailment_judge", n_claims=len(claims), ok="") as s:
            text = ""
            for attempt in range(2):  # 输出解析失败重试一次（多为格式抖动）
                try:
                    if self._gateway is not None:
                        result = await self._gateway.call(
                            client=self._client,
                            model=self._model,
                            messages=[{"role": "user", "content": user}],
                            state=None,
                            span_name="entailment_judge",
                            max_tokens=512,
                            system=system,
                        )
                        resp = result.response
                    else:
                        resp = await self._client.messages.create(
                            model=self._model,
                            max_tokens=512,
                            system=system,
                            messages=[{"role": "user", "content": user}],
                        )
                    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip().strip("`")
                    if text.startswith("json"):
                        text = text[4:]
                    data = json.loads(text)
                    break
                except Exception as ex:
                    if attempt == 0:
                        logger.warning("Entailment Judge 输出解析失败，重试一次: %s", ex)
                        continue
                    raise
            known = set(claims)
            out: Dict[str, Dict[str, Any]] = {}
            for d in data.get("decisions", []):
                claim = str(d.get("claim", "")).strip()
                verdict = str(d.get("verdict", "")).strip().lower()
                if claim not in known or verdict not in ("supported", "contradicted", "insufficient"):
                    continue
                ids = [
                    int(x)
                    for x in (d.get("evidence_ids") or [])
                    if str(x).lstrip("-").isdigit() and 1 <= int(x) <= len(evidences)
                ]
                out[claim] = {"verdict": verdict, "evidence_ids": ids}
            if s is not None:  # 观测：原始输出截断进 trace，供离线评估 Judge 输出质量
                s.meta["ok"] = "1" if out else "0"
                s.meta["raw"] = text[:120]
            return out


# ── 引用标注（执行层后置生成）────────────────────────────────────────────────


def _strip_markdown_links(text: str) -> str:
    """剔除 [text](url) 形式的 Markdown 链接（其 [n] 是链接标签不是引用）。"""
    return re.sub(r"\[[^\]]*\]\([^)]*\)", "", text or "")


def existing_citations(answer: str) -> List[int]:
    """解析回答中已有的 [n] 引用索引（模型自觉输出的，供合并/校验）。"""
    stripped = _strip_markdown_links(answer or "")
    return sorted({int(m) for m in re.findall(r"\[(\d+)\]", stripped)})


def strip_citation_markers(answer: str) -> str:
    """移除正文中独立的 [n] 引用标记（保留 Markdown 链接）。

    引用由 Grounding 层统一重标，避免"模型标错 + 执行层再标"造成
    重复/越界引用。
    """
    text = answer or ""
    # 先保护 Markdown 链接（占位符替换），再删独立 [n]
    links: List[str] = []

    def _save(m: re.Match) -> str:
        links.append(m.group(0))
        return f"\x00{len(links) - 1}\x00"

    text = re.sub(r"\[[^\]]*\]\([^)]*\)", _save, text)
    text = re.sub(r"\[\d+\]", "", text)
    for i, link in enumerate(links):
        text = text.replace(f"\x00{i}\x00", link)
    return text


async def annotate_citations(
    answer: str,
    evidences: List[Dict[str, Any]],
    entailment_judge: Optional[LLMEntailmentJudge] = None,
) -> Dict[str, Any]:
    """Claim-aware 引用标注（确定性后置保证）：

      1. 剥离模型自觉输出的 [n]（防重复/越界）
      2. 按句拆分 → 原子事实拆分 → 事实性过滤
      3. 全证据候选匹配（Dice + 批量 BGE 余弦 + Hard Guard）
      4. 高置信直接判定；模糊 Claim 批量交给 Entailment Judge
      5. 支持的 Claim 在原位追加 [i]（i = 证据 1-based 序号）；整句多
         个 Claim 可分别标注（"补办校园卡需要身份证[1]，费用为 20 元。"）
      6. 无支持的 Claim 不加引用（保留原句，交给 Verifier 标注 unsupported）

    返回 {text, sentences, citation_indices, unsupported_sentences, claims}
    citation_indices 恒在 [1, len(evidences)] 内——引用正确性由执行层保证；
    claims 为逐 Claim 的完整决策记录（Trace / 错误分析用）。
    """
    if not evidences:
        return {
            "text": answer or "",
            "sentences": [],
            "citation_indices": [],
            "unsupported_sentences": [],
            "claims": [],
        }

    body = strip_citation_markers(answer or "")
    parts = split_sentences_raw(body)

    # 1) 原子 Claim 拆分 + 事实性过滤（保留原文以便无损重建）
    parts_claims: List[List[Tuple[str, str]]] = []
    factual_claims: List[str] = []
    for part in parts:
        clauses = split_claims(part.strip()) if part.strip() else []
        parts_claims.append(clauses)
        for c, _sep in clauses:
            c = c.strip()
            if c and is_factual_claim(c):
                factual_claims.append(c)

    # 2) 一次 batch 嵌入：全部 Claim × 全部证据（避免每对单独推理）
    cos_by_claim = await _batch_cosines_multi(factual_claims, evidences)

    # 3) 逐 Claim 决策（Guard 过滤 + 高置信直接判定；模糊 Claim 收集）
    records: List[Optional[Dict[str, Any]]] = []
    pending: List[Dict[str, Any]] = []
    for clauses in parts_claims:
        for c, _sep in clauses:
            c = c.strip()
            if not c:
                records.append(None)
                continue
            if not is_factual_claim(c):
                records.append(_skip_record(c))
                continue
            cos_scores = cos_by_claim.get(c) if cos_by_claim else [0.0] * len(evidences)
            candidates = await match_evidence_candidates(c, evidences, cos_scores)
            rec = _decide_from_candidates(c, candidates)
            if rec["status"] == "needs_judge":
                pending.append(rec)
            records.append(rec)

    # 4) 模糊 Claim 批量 Judge（一次请求；失败 fail-open）
    if pending and entailment_judge is not None:
        unique = list(dict.fromkeys(r["claim"] for r in pending))
        try:
            verdicts = await entailment_judge.judge(unique, evidences)
        except Exception as ex:
            logger.warning("Entailment Judge 调用失败，模糊 Claim 按 insufficient 兜底: %s", ex)
            verdicts = {}
        for rec in pending:
            _apply_judge(rec, verdicts.get(rec["claim"]), evidences)
    for rec in records:
        if rec is not None and rec["status"] == "needs_judge":
            rec["status"] = "insufficient"  # 无 Judge 时模糊区间兜底（宁缺勿错）

    # 5) 标注：支持句/Claim 在原位追加 [i]，其余原样保留（含换行/缩进）
    cited_sents: List[Dict[str, Any]] = []
    unsupported: List[str] = []
    indices: set = set()
    rec_pos = 0
    out_parts: List[str] = []
    for part, clauses in zip(parts, parts_claims, strict=False):
        if not clauses:
            out_parts.append(part)
            continue
        leading = part[: len(part) - len(part.lstrip())]
        tail = part[len(part.rstrip()) :]
        body_parts: List[str] = []
        for c, sep in clauses:
            rec = records[rec_pos]
            rec_pos += 1
            mark = ""
            if rec is not None and rec["status"] == "supported" and rec["citation"]:
                mark = f"[{rec['citation']}]"
                cited_sents.append(
                    {
                        "sentence": c.strip(),
                        "evidence_idx": rec["selected_evidence_idx"],
                        "dice": rec["best_dice"],
                        "cos": rec["best_cos"],
                    }
                )
                indices.add(rec["citation"])
            elif rec is not None and rec["status"] in ("insufficient", "contradicted"):
                unsupported.append(rec["claim"])
            # 标注插在 Claim 行尾空白之前（与句级标注同一风格）
            body_parts.append(f"{c.rstrip()}{mark}{c[len(c.rstrip()) :]}{sep}")
        out_parts.append(leading + "".join(body_parts) + tail)
    annotated = "".join(out_parts)

    return {
        "text": annotated,
        "sentences": cited_sents,
        "citation_indices": sorted(indices),
        "unsupported_sentences": unsupported,
        "claims": [r for r in records if r is not None],
    }


async def grounding_trace(
    answer: str,
    evidences: List[Dict[str, Any]],
    entailment_judge: Optional[LLMEntailmentJudge] = None,
) -> Dict[str, Any]:
    """完整 Grounding trace（可观测性/评测用，不修改回答）。

    对每个 factual claim 记录：claim、factual、候选（evidence_idx/title/
    dice/cos/combined/guard）、selected evidence、hard guard、decision
    source（deterministic / entailment / hard_guard / skip）、最终状态
    （supported / contradicted / insufficient / skipped）以及 Citation。
    后续 Evaluation 发现 Faithfulness / Citation 异常时，可直接通过 Trace
    定位是 Claim 拆分、Evidence 匹配、Hard Guard 还是 Entailment 判断出错。
    """
    if not evidences:
        return {
            "sentence_count": 0,
            "claim_count": 0,
            "supported_count": 0,
            "supported_ratio": 0.0,
            "sentences": [],
            "claims": [],
        }
    parts = split_sentences_raw(answer or "")
    per_sentence = []
    factual_claims: List[str] = []
    for part in parts:
        sent = part.strip()
        clauses = split_claims(sent) if sent else []
        per_sentence.append(
            {
                "sentence": sent,
                "claims": [c.strip() for c, _ in clauses if c.strip()],
            }
        )
        for c, _sep in clauses:
            c = c.strip()
            if c and is_factual_claim(c):
                factual_claims.append(c)

    cos_by_claim = await _batch_cosines_multi(factual_claims, evidences) if factual_claims else None
    records: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for c in factual_claims:
        cos_scores = cos_by_claim.get(c) if cos_by_claim else [0.0] * len(evidences)
        rec = _decide_from_candidates(c, await match_evidence_candidates(c, evidences, cos_scores))
        if rec["status"] == "needs_judge":
            pending.append(rec)
        records.append(rec)
    if pending and entailment_judge is not None:
        unique = list(dict.fromkeys(r["claim"] for r in pending))
        try:
            verdicts = await entailment_judge.judge(unique, evidences)
        except Exception as ex:
            logger.warning("Entailment Judge 调用失败，模糊 Claim 按 insufficient 兜底: %s", ex)
            verdicts = {}
        for rec in pending:
            _apply_judge(rec, verdicts.get(rec["claim"]), evidences)
    for rec in records:
        if rec["status"] == "needs_judge":
            rec["status"] = "insufficient"

    supported_n = sum(1 for r in records if r["status"] == "supported")
    return {
        "sentence_count": len(per_sentence),
        "claim_count": len(records),
        "supported_count": supported_n,
        "supported_ratio": round(supported_n / len(records), 4) if records else 0.0,
        "sentences": per_sentence,
        "claims": records,
    }


def build_source_section(evidences: List[Dict[str, Any]], cited_indices: List[int]) -> str:
    """末尾来源区：按 [i] 编号列出被引用的证据（title + url）。"""
    lines = []
    for i, ev in enumerate(evidences):
        if (i + 1) not in cited_indices:
            continue
        title = str(ev.get("title") or "公开来源").strip()
        url = str(ev.get("source_url") or "").strip()
        updated = str(ev.get("updated_at") or "").strip()
        if url:
            line = f"[{i + 1}] [{title}]({url})"
        else:
            line = f"[{i + 1}] {title}"
        if updated:
            line = f"{line}（更新：{updated}）"
        lines.append(line)
    if not lines:
        return ""
    return "\n### 可核验来源\n" + "\n".join(lines)


async def match_evidence(sentence: str, evidences: List[Dict[str, Any]]) -> Tuple[int, float, float]:
    """向后兼容包装：句子 → 最佳证据匹配，返回 (evidence_idx, dice, cos)。

    注意：最佳 = 组合分最高且未与 Hard Guard 冲突的候选；evidence_idx = -1
    表示无任何证据可匹配。新代码请使用 match_evidence_candidates / decide_claim。
    """
    candidates = await match_evidence_candidates(sentence, evidences)
    for c in candidates:
        if not c["guard"]["conflict"]:
            return c["evidence_idx"], c["dice"], c["cos"]
    if candidates:
        return candidates[0]["evidence_idx"], candidates[0]["dice"], candidates[0]["cos"]
    return -1, 0.0, 0.0
