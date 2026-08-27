"""时间上下文测试：教学周计算、当前节次判断、夏季/标准作息切换、上下文文本。

time_context 的常量在 import 时从环境变量读取，测试只验证函数对
显式传入的 datetime 的确定性计算（不依赖真实时钟）。
"""
from __future__ import annotations

from datetime import UTC, datetime

from personal.time_context import (
    WEEKDAY_CN,
    build_time_context,
    current_period,
    periods_for,
    week_num,
)


def test_week_num_before_semester():
    # 2026-08-05：开学（2026-09-07）之前 → 0
    assert week_num(datetime(2026, 8, 5, 10, 0, tzinfo=UTC)) == 0


def test_week_num_first_week():
    assert week_num(datetime(2026, 9, 7, 8, 0, tzinfo=UTC)) == 1   # 开学当天（周一）
    assert week_num(datetime(2026, 9, 13, 8, 0, tzinfo=UTC)) == 1  # 第一周周日


def test_week_num_caps_at_semester_weeks():
    # 超出学期末：封顶为总周数（19）
    assert week_num(datetime(2027, 3, 1, 8, 0, tzinfo=UTC)) == 19


def test_current_period_in_class_morning():
    name, in_class = current_period(datetime(2026, 9, 7, 8, 30, tzinfo=UTC))   # 第 1 节开始
    assert in_class is True
    assert name.startswith("第1节")
    name, in_class = current_period(datetime(2026, 9, 7, 11, 15, tzinfo=UTC))  # 第 4 节
    assert in_class is True
    assert name.startswith("第4节")


def test_current_period_break():
    name, in_class = current_period(datetime(2026, 9, 7, 12, 30, tzinfo=UTC))  # 午休
    assert in_class is False
    assert "非上课时间" in name


def test_current_period_evening():
    name, in_class = current_period(datetime(2026, 9, 7, 19, 30, tzinfo=UTC))  # 第 9 节
    assert in_class is True
    assert name.startswith("第9节")


def test_summer_vs_standard_afternoon():
    """夏季（5 月）下午 14:00 是课间；秋冬春季（11 月）14:00 是第 5 节上课。"""
    summer = periods_for(datetime(2026, 5, 10, 14, 0, tzinfo=UTC))
    standard = periods_for(datetime(2026, 11, 10, 14, 0, tzinfo=UTC))
    assert summer[4][1] == "14:30"      # 夏季第 5 节 14:30 开始
    assert standard[4][1] == "14:00"    # 标准第 5 节 14:00 开始

    name, in_class = current_period(datetime(2026, 5, 10, 14, 10, tzinfo=UTC))
    assert in_class is False            # 夏季 14:10 尚未上课
    name, in_class = current_period(datetime(2026, 11, 10, 14, 10, tzinfo=UTC))
    assert in_class is True             # 标准 14:10 已是第 5 节


def test_build_time_context_contains_key_info():
    ctx = build_time_context(datetime(2026, 9, 7, 10, 30, tzinfo=UTC))
    assert "2026-09-07" in ctx
    assert "周一" in ctx
    assert "第 1 周" in ctx
    assert WEEKDAY_CN[0] == "周一"
