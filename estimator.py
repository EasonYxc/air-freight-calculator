#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空运重量段成本预估器 (Air Freight Tier Cost Estimator)  v2.0
=============================================================
严格按照业务流程图实现，支持三种计算模式、计费重取整、靠级分析、可信度评估。

计算模式:
  dimension_based      精确尺寸模式（有 pieces + 长宽高）
  cbm_based            总体积模式（有 total_volume_cbm）
  weight_only_estimate 仅实重预估模式（无体积数据）

计费重取整:
  ceil_1kg    向上取整到 1kg
  ceil_0.5kg  向上取整到 0.5kg
  keep_decimal保留小数

作者: WorkBuddy
版本: 2.0.0（按流程图重构）
"""

from __future__ import annotations
import math
import json as _json
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum


# ============================================================
# 常量 & 枚举
# ============================================================

DEFAULT_BILLING_FACTOR = 6000   # 默认计费系数（cm³ → kg）


class CalcMode(Enum):
    """计算模式"""
    DIMENSION_BASED = "dimension_based"
    CBM_BASED = "cbm_based"
    WEIGHT_ONLY = "weight_only_estimate"


class RoundingRule(Enum):
    """计费重取整规则"""
    CEIL_1KG = "ceil_1kg"
    CEIL_05KG = "ceil_0.5kg"
    KEEP_DECIMAL = "keep_decimal"


class Confidence(Enum):
    """可信度（按计算模式判定）"""
    HIGH = "高"      # dimension_based
    MEDIUM = "中"    # cbm_based
    LOW = "低"       # weight_only_estimate


@dataclass
class RateTable:
    """服务商重量段价格表"""
    min: Optional[float] = None      # 固定最低收费（元/票）
    n: Optional[float] = None        # N 段单价（元/kg），<45kg
    t45: Optional[float] = None      # +45 段单价（元/kg）
    t100: Optional[float] = None     # +100 段单价（元/kg）
    t300: Optional[float] = None     # +300 段单价（元/kg）
    t500: Optional[float] = None     # +500 段单价（元/kg）
    t1000: Optional[float] = None    # +1000 段单价（元/kg）

    def get_available_tiers(self) -> List[Tuple[str, Optional[float], float]]:
        """返回所有有报价的可用重量段: [(段名, 段阈值kg, 单价), ...]"""
        tiers = []
        mapping = [
            ("Min", None, self.min),
            ("N", None, self.n),
            ("+45", 45.0, self.t45),
            ("+100", 100.0, self.t100),
            ("+300", 300.0, self.t300),
            ("+500", 500.0, self.t500),
            ("+1000", 1000.0, self.t1000),
        ]
        for name, threshold, price in mapping:
            if price is not None and price > 0:
                tiers.append((name, threshold, price))
        return tiers

    def is_empty(self) -> bool:
        return len(self.get_available_tiers()) == 0


@dataclass
class TierCandidate:
    """单个重量段的成本方案"""
    tier_name: str                    # 段名 (Min / N / +45 / +100 / +300 / +500 / +1000)
    tier_threshold: Optional[float]   # 段阈值 (kg)
    settlement_weight: float          # 结算计费重 (kg)
    unit_price: float                 # 单价 (元/kg 或 元/票)
    cost: float                       # 空运成本 (元)
    is_pivot: bool                    # 是否靠级（非自然落入段）
    is_recommended: bool              # 是否为推荐方案


@dataclass
class EstimateResult:
    """完整预估结果（字段名严格按流程图 T1-T16）"""
    # 元信息
    provider_name: str
    origin: str
    destination: str
    currency: str                     # 币种（如 CNY / USD）
    valid_until: Optional[str]        # 报价有效期

    # 计算元数据
    calculation_mode: CalcMode
    billing_factor: float
    rounding_rule: RoundingRule

    # 货物信息
    gross_weight_kg: float            # T3  总实重 (kg)
    total_volume_cbm: Optional[float] # T4  总体积 (m³)
    volumetric_weight_kg: Optional[float]  # T5  体积重 (kg)
    bubble_ratio: Optional[float]     # T6  泡比

    # 计费重
    raw_chargeable_weight_kg: float   # T7  原始计费重 (kg)
    rounded_chargeable_weight_kg: float  # T8  取整后计费重 (kg)

    # 推荐结果
    recommended_tier: str             # T9  推荐重量段
    settlement_weight_kg: float       # T10 结算计费重 (kg)
    rate: float                       # T11 适用单价
    air_freight_cost: float           # T13 空运成本 (元)
    is_pivot: bool                    # T14 是否靠级

    # 辅助
    confidence: Confidence            # T2  可信度
    remark: List[str]                 # T15 备注
    all_candidates: List[TierCandidate]  # 所有候选方案

    # 输出兼容
    @property
    def raw_chargeable_weight(self) -> float:
        """兼容旧版字段名"""
        return self.raw_chargeable_weight_kg

    @property
    def chargeable_weight(self) -> float:
        """兼容旧版字段名 → rounded_chargeable_weight_kg"""
        return self.rounded_chargeable_weight_kg


# ============================================================
# 多服务商比价 (模式二)
# ============================================================

class RecLabel(Enum):
    """推荐标签"""
    LOWEST_COST = "最低成本方案"
    NATURAL_LOWEST = "自然段最低方案"
    PIVOT_SAVING = "靠级节省方案"
    FASTER_PREMIUM = "时效较快但成本略高"
    NONE = ""


@dataclass
class ProviderConfig:
    """单个服务商的比价配置"""
    provider_name: str                   # 服务商名称
    airline: str = ""                    # 航司 / 渠道
    transit_time_days: Optional[int] = None  # 时效 (天)
    origin: str = ""                     # 起运地 (可覆盖默认)
    destination: str = ""                # 目的地 (可覆盖默认)
    currency: str = "CNY"               # 币种 (可覆盖默认)
    valid_until: Optional[str] = None   # 报价有效期

    # 独立价格表
    rate_min: Optional[float] = None
    rate_n: Optional[float] = None
    rate_45: Optional[float] = None
    rate_100: Optional[float] = None
    rate_300: Optional[float] = None
    rate_500: Optional[float] = None
    rate_1000: Optional[float] = None

    def to_rates(self) -> RateTable:
        return RateTable(
            min=self.rate_min, n=self.rate_n,
            t45=self.rate_45, t100=self.rate_100,
            t300=self.rate_300, t500=self.rate_500, t1000=self.rate_1000,
        )


@dataclass
class ProviderCompareItem:
    """单个服务商的比价结果项"""
    # 服务商信息
    provider_name: str
    airline: str
    transit_time_days: Optional[int]

    # 单个预估结果
    result: EstimateResult

    # 排名信息
    rank: int = 0                          # 成本排名 (1-based)
    rec_label: RecLabel = RecLabel.NONE    # 推荐标签

    @property
    def cost(self) -> float:
        return self.result.air_freight_cost

    @property
    def tier(self) -> str:
        return self.result.recommended_tier

    @property
    def is_pivot(self) -> bool:
        return self.result.is_pivot


@dataclass
class CompareResult:
    """多服务商比价汇总结果"""
    # 共享的货物信息
    gross_weight_kg: float
    total_volume_cbm: Optional[float]
    volumetric_weight_kg: Optional[float]
    bubble_ratio: Optional[float]
    raw_chargeable_weight_kg: float
    rounded_chargeable_weight_kg: float
    calculation_mode: CalcMode

    # 共享的计算规则
    billing_factor: float
    rounding_rule: RoundingRule
    allow_pivot: bool

    # 比价结果
    items: List[ProviderCompareItem]       # 已排序

    # 汇总信息
    currency_consistent: bool              # 币种是否一致
    remark: List[str] = field(default_factory=list)


def compare_providers(
    # ---- 共享货物信息 ----
    gross_weight_kg: float,
    pieces: Optional[int] = None,
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    total_volume_cbm: Optional[float] = None,

    # ---- 共享计算规则 ----
    billing_factor: float = DEFAULT_BILLING_FACTOR,
    rounding_rule: RoundingRule = RoundingRule.CEIL_1KG,
    allow_pivot: bool = True,

    # ---- 服务商列表 ----
    providers: Optional[List[ProviderConfig]] = None,
) -> CompareResult:
    """
    多服务商比价主函数（严格按流程图模式二实现）。

    1. 用共享货物信息 + 规则，对每个服务商执行单服务商计算
    2. 汇总所有服务商的最优方案
    3. 币种一致性检查
    4. 按成本升序排序（同币种时）
    5. 生成推荐标签

    返回 CompareResult。
    """
    if not providers:
        raise ValueError("至少需要提供一个服务商")

    remark: List[str] = []

    # ---- 1. 逐个服务商计算 ----
    items: List[ProviderCompareItem] = []
    currencies: set = set()

    for pvd in providers:
        # 每个服务商独立用单服务商逻辑计算
        result = estimate(
            provider_name=pvd.provider_name,
            origin=pvd.origin,
            destination=pvd.destination,
            currency=pvd.currency,
            valid_until=pvd.valid_until,
            gross_weight_kg=gross_weight_kg,
            billing_factor=billing_factor,
            pieces=pieces,
            length_cm=length_cm,
            width_cm=width_cm,
            height_cm=height_cm,
            total_volume_cbm=total_volume_cbm,
            rounding_rule=rounding_rule,
            allow_pivot=allow_pivot,
            rates=pvd.to_rates(),
        )

        currencies.add(pvd.currency)
        items.append(ProviderCompareItem(
            provider_name=pvd.provider_name,
            airline=pvd.airline,
            transit_time_days=pvd.transit_time_days,
            result=result,
        ))

    # ---- 2. 币种一致性检查 ----
    currency_consistent = len(currencies) == 1

    if not currency_consistent:
        remark.append(
            f"⚠️ 服务商币种不一致 ({', '.join(sorted(currencies))})，不自动排序排名，"
            f"请人工比对汇率后确认"
        )
        # 同币种分组排序，不同币种各自排序后交错展示
        _sort_by_currency_groups(items)
    else:
        # 同币种，按空运成本升序
        items.sort(key=lambda x: x.cost)

    # ---- 3. 排名 ----
    for i, item in enumerate(items):
        item.rank = i + 1

    # ---- 4. 生成推荐标签 ----
    if items:
        sorted_by_cost = sorted(items, key=lambda x: x.cost)
        sorted_by_nat = sorted(
            [it for it in items if not it.is_pivot],
            key=lambda x: x.cost,
        )
        sorted_by_time = sorted(
            [it for it in items if it.transit_time_days is not None],
            key=lambda x: (x.transit_time_days or 999, x.cost),
        )

        # 最低成本方案
        best = sorted_by_cost[0]
        best.rec_label = RecLabel.LOWEST_COST

        # 自然段最低方案 (不同于最低成本方案时)
        if sorted_by_nat and sorted_by_nat[0] is not best:
            nat_best = sorted_by_nat[0]
            nat_best.rec_label = RecLabel.NATURAL_LOWEST

        # 靠级节省方案 (有靠级且节省>10%)
        for item in items:
            if item.is_pivot and item.rec_label == RecLabel.NONE:
                nat_opts = [c for c in item.result.all_candidates if not c.is_pivot]
                if nat_opts and nat_opts[0].cost > 0:
                    saving_pct = (nat_opts[0].cost - item.cost) / nat_opts[0].cost * 100
                    if saving_pct > 10:
                        item.rec_label = RecLabel.PIVOT_SAVING
                        break

        # 时效较快但成本略高方案
        if sorted_by_time and currency_consistent:
            fastest = sorted_by_time[0]
            if fastest.rec_label == RecLabel.NONE and fastest.cost > best.cost:
                fastest.rec_label = RecLabel.FASTER_PREMIUM

    # ---- 5. 组装结果 ----
    # 从第一个结果提取共享的货物信息
    first = items[0].result
    result = CompareResult(
        gross_weight_kg=gross_weight_kg,
        total_volume_cbm=first.total_volume_cbm,
        volumetric_weight_kg=first.volumetric_weight_kg,
        bubble_ratio=first.bubble_ratio,
        raw_chargeable_weight_kg=first.raw_chargeable_weight_kg,
        rounded_chargeable_weight_kg=first.rounded_chargeable_weight_kg,
        calculation_mode=first.calculation_mode,
        billing_factor=billing_factor,
        rounding_rule=rounding_rule,
        allow_pivot=allow_pivot,
        items=items,
        currency_consistent=currency_consistent,
        remark=remark,
    )
    return result


def _sort_by_currency_groups(items: List[ProviderCompareItem]) -> None:
    """币种不一致时：按币种分组，组内按成本排序，然后按组名交错排列"""
    from collections import defaultdict
    groups: Dict[str, list] = defaultdict(list)
    for item in items:
        groups[item.result.currency].append(item)
    for g in groups.values():
        g.sort(key=lambda x: x.cost)

    # 交错排列: 取每组第一个 → 每组第二个 → ...
    result: List[ProviderCompareItem] = []
    max_len = max(len(g) for g in groups.values())
    sorted_currencies = sorted(groups.keys())
    for i in range(max_len):
        for cur in sorted_currencies:
            if i < len(groups[cur]):
                result.append(groups[cur][i])
    items.clear()
    items.extend(result)


# ============================================================
# 体积 & 计费重计算
# ============================================================

def _calc_dims_mode(
    pieces: int,
    length_cm: float,
    width_cm: float,
    height_cm: float,
    gross_weight_kg: float,
    billing_factor: float,
) -> Tuple[float, float, float]:
    """
    dimension_based 模式：
      CBM = pieces × L × W × H / 1000000
      体积重 = pieces × L × W × H / billing_factor
      原始计费重 = max(总实重, 体积重)
    返回: (total_volume_cbm, volumetric_weight_kg, raw_chargeable_weight_kg)
    """
    total_cm3 = pieces * length_cm * width_cm * height_cm
    cbm = round(total_cm3 / 1_000_000, 6)
    vol_wt = round(total_cm3 / billing_factor, 2)
    raw_cw = max(gross_weight_kg, vol_wt)
    return cbm, vol_wt, raw_cw


def _calc_cbm_mode(
    total_volume_cbm: float,
    gross_weight_kg: float,
    billing_factor: float,
) -> Tuple[float, float]:
    """
    cbm_based 模式：
      体积重 = total_volume_cbm × 1000000 / billing_factor
      原始计费重 = max(总实重, 体积重)
    返回: (volumetric_weight_kg, raw_chargeable_weight_kg)
    """
    vol_wt = round(total_volume_cbm * 1_000_000 / billing_factor, 2)
    raw_cw = max(gross_weight_kg, vol_wt)
    return vol_wt, raw_cw


def _apply_rounding(weight: float, rule: RoundingRule) -> float:
    """按取整规则处理计费重"""
    if rule == RoundingRule.CEIL_1KG:
        return math.ceil(weight)
    elif rule == RoundingRule.CEIL_05KG:
        return math.ceil(weight * 2) / 2
    else:  # keep_decimal
        return weight


# ============================================================
# 重量段匹配 & 靠级
# ============================================================

def _get_natural_tier(
    rounded_cw: float,
    rates: RateTable,
) -> Tuple[str, Optional[float], float]:
    """确定取整后计费重自然落入的重量段"""
    checks = [
        ("+1000", 1000.0, rates.t1000),
        ("+500", 500.0, rates.t500),
        ("+300", 300.0, rates.t300),
        ("+100", 100.0, rates.t100),
        ("+45", 45.0, rates.t45),
    ]
    for name, threshold, price in checks:
        if price is not None and rounded_cw >= threshold:
            return name, threshold, price

    if rates.n is not None:
        return "N", None, rates.n
    if rates.t45 is not None:
        return "+45", 45.0, rates.t45

    raise ValueError("价格表中无可用重量段")


def _generate_candidates(
    rounded_cw: float,
    rates: RateTable,
    natural_tier_name: str,
    allow_pivot: bool,
) -> List[TierCandidate]:
    """生成所有候选计费方案"""
    candidates: List[TierCandidate] = []

    # N 段（仅当计费重 < 45kg 时纳入候选）
    if rates.n is not None and rounded_cw < 45.0:
        raw_cost = rounded_cw * rates.n
        cost = max(raw_cost, rates.min) if rates.min is not None else raw_cost
        candidates.append(TierCandidate(
            tier_name="N",
            tier_threshold=None,
            settlement_weight=rounded_cw,
            unit_price=rates.n,
            cost=round(cost, 2),
            is_pivot=False,  # N 段无阈值概念，不算靠级
            is_recommended=False,
        ))

    # +45 / +100 / +300 / +500 / +1000
    tier_defs = [
        ("+45", 45.0, rates.t45),
        ("+100", 100.0, rates.t100),
        ("+300", 300.0, rates.t300),
        ("+500", 500.0, rates.t500),
        ("+1000", 1000.0, rates.t1000),
    ]

    for t_name, t_threshold, t_price in tier_defs:
        if t_price is None:
            continue

        is_natural = (natural_tier_name == t_name)

        if is_natural:
            # 自然落入：按取整后计费重计费
            settlement = rounded_cw
            is_pivot = False
        elif allow_pivot and rounded_cw < t_threshold:
            # 靠级：按段阈值计费
            settlement = t_threshold
            is_pivot = True
        else:
            # 计费重 ≥ 阈值且非自然段 → 跳过（不靠级到更低段）
            continue

        raw_cost = settlement * t_price
        cost = max(raw_cost, rates.min) if rates.min is not None else raw_cost

        candidates.append(TierCandidate(
            tier_name=t_name,
            tier_threshold=t_threshold,
            settlement_weight=settlement,
            unit_price=t_price,
            cost=round(cost, 2),
            is_pivot=is_pivot,
            is_recommended=False,
        ))

    return candidates


# ============================================================
# 主入口
# ============================================================

def estimate(
    # ---- 基础信息 ----
    provider_name: str = "",
    origin: str = "",
    destination: str = "",
    currency: str = "CNY",
    valid_until: Optional[str] = None,

    # ---- 货物信息 ----
    gross_weight_kg: float = 0.0,
    billing_factor: float = DEFAULT_BILLING_FACTOR,
    pieces: Optional[int] = None,
    length_cm: Optional[float] = None,
    width_cm: Optional[float] = None,
    height_cm: Optional[float] = None,
    total_volume_cbm: Optional[float] = None,

    # ---- 计算规则 ----
    rounding_rule: RoundingRule = RoundingRule.CEIL_1KG,
    allow_pivot: bool = True,

    # ---- 价格表 ----
    rates: Optional[RateTable] = None,
    rate_min: Optional[float] = None,
    rate_n: Optional[float] = None,
    rate_45: Optional[float] = None,
    rate_100: Optional[float] = None,
    rate_300: Optional[float] = None,
    rate_500: Optional[float] = None,
    rate_1000: Optional[float] = None,
) -> EstimateResult:
    """
    空运重量段成本预估主函数（严格按流程图实现）。

    返回 EstimateResult，包含 T1-T16 所有输出字段。
    """
    # ---- 0. 组装价格表 ----
    if rates is None:
        rates = RateTable(
            min=rate_min, n=rate_n,
            t45=rate_45, t100=rate_100,
            t300=rate_300, t500=rate_500, t1000=rate_1000,
        )

    if rates.is_empty():
        raise ValueError("当前价格表无可用重量段")

    remark: List[str] = []

    # ---- 1. 判定计算模式 ----
    has_dims = (
        pieces is not None and pieces > 0
        and length_cm is not None and length_cm > 0
        and width_cm is not None and width_cm > 0
        and height_cm is not None and height_cm > 0
    )
    has_cbm = total_volume_cbm is not None and total_volume_cbm > 0

    if has_dims:
        calc_mode = CalcMode.DIMENSION_BASED
    elif has_cbm:
        calc_mode = CalcMode.CBM_BASED
    else:
        calc_mode = CalcMode.WEIGHT_ONLY

    # ---- 2. 计算体积信息 ----
    cbm_val: Optional[float] = None
    vol_wt: Optional[float] = None

    if calc_mode == CalcMode.DIMENSION_BASED:
        cbm_val, vol_wt, raw_cw = _calc_dims_mode(
            pieces, length_cm, width_cm, height_cm,
            gross_weight_kg, billing_factor,
        )
    elif calc_mode == CalcMode.CBM_BASED:
        cbm_val = total_volume_cbm
        vol_wt, raw_cw = _calc_cbm_mode(
            total_volume_cbm, gross_weight_kg, billing_factor,
        )
    else:  # weight_only_estimate
        raw_cw = gross_weight_kg

    # ---- 3. 计算泡比 ----
    bubble_ratio: Optional[float] = None
    if vol_wt is not None and gross_weight_kg > 0:
        bubble_ratio = round(vol_wt / gross_weight_kg, 2)

    # ---- 4. 计费重取整 ----
    rounded_cw = _apply_rounding(raw_cw, rounding_rule)

    # ---- 5. 确定自然段 & 生成候选方案 ----
    natural_tier_name, _, _ = _get_natural_tier(rounded_cw, rates)
    candidates = _generate_candidates(rounded_cw, rates, natural_tier_name, allow_pivot)

    if not candidates:
        raise ValueError("无法生成有效的计费方案")

    # ---- 6. 确定推荐方案（最低成本）----
    candidates.sort(key=lambda c: (c.cost, 0 if not c.is_pivot else 1))
    recommended = candidates[0]
    for c in candidates:
        c.is_recommended = (c is recommended)

    # ---- 7. 计算可信度（按计算模式判定）----
    if calc_mode == CalcMode.DIMENSION_BASED:
        confidence = Confidence.HIGH
    elif calc_mode == CalcMode.CBM_BASED:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW

    # ---- 8. 生成备注 ----
    # 8a. 体积信息备注
    if calc_mode == CalcMode.WEIGHT_ONLY:
        remark.append("仅按实重预估，缺少尺寸和CBM数据，最终计费重可能变化")
    else:
        remark.append("已根据体积信息计算")

    # 8b. 靠级备注
    if recommended.is_pivot:
        remark.append(f"靠级至 {recommended.tier_name} 段后成本更低，自然段为 {natural_tier_name}")
    else:
        remark.append("未发生靠级，按自然段计费")

    # 8c. 泡货提醒
    if bubble_ratio is not None and bubble_ratio > 1.0:
        remark.append(f"货物为泡货（泡比 {bubble_ratio:.2f}），体积重主导计费")

    # 8d. 靠级节省提示
    natural_candidates = [c for c in candidates if not c.is_pivot]
    if recommended.is_pivot and natural_candidates:
        nat_cost = natural_candidates[0].cost
        saved = nat_cost - recommended.cost
        if saved > 0:
            remark.append(f"靠级节省 {saved:.0f} 元，建议与航司确认靠级可行性")

    # 8e. 备选方案接近提醒
    if len(candidates) >= 2:
        diff = candidates[1].cost - candidates[0].cost
        if 0 < diff < 10:
            remark.append(
                f"备选方案 {candidates[1].tier_name} 段成本仅差 {diff:.1f} 元，可灵活选择"
            )

    # 8f. 报价有效期提醒
    if valid_until:
        remark.append(f"报价有效期至 {valid_until}，请在此日期前确认")

    # ---- 9. 组装结果 ----
    return EstimateResult(
        provider_name=provider_name,
        origin=origin,
        destination=destination,
        currency=currency,
        valid_until=valid_until,
        calculation_mode=calc_mode,
        billing_factor=billing_factor,
        rounding_rule=rounding_rule,
        gross_weight_kg=gross_weight_kg,
        total_volume_cbm=cbm_val,
        volumetric_weight_kg=vol_wt,
        bubble_ratio=bubble_ratio,
        raw_chargeable_weight_kg=round(raw_cw, 2),
        rounded_chargeable_weight_kg=rounded_cw,
        recommended_tier=recommended.tier_name,
        settlement_weight_kg=recommended.settlement_weight,
        rate=recommended.unit_price,
        air_freight_cost=recommended.cost,
        is_pivot=recommended.is_pivot,
        confidence=confidence,
        remark=remark,
        all_candidates=candidates,
    )


# ============================================================
# JSON 序列化（按流程图 T1-T16 输出）
# ============================================================

def result_to_dict(result: EstimateResult) -> dict:
    """将预估结果转换为字典（字段名严格按流程图 T1-T16）"""
    return {
        "calculation_mode": result.calculation_mode.value,          # T1
        "confidence": result.confidence.value,                       # T2
        "gross_weight_kg": result.gross_weight_kg,                   # T3
        "total_volume_cbm": result.total_volume_cbm,                 # T4
        "volumetric_weight_kg": result.volumetric_weight_kg,         # T5
        "bubble_ratio": result.bubble_ratio,                         # T6
        "raw_chargeable_weight_kg": result.raw_chargeable_weight_kg, # T7
        "rounded_chargeable_weight_kg": result.rounded_chargeable_weight_kg,  # T8
        "recommended_tier": result.recommended_tier,                 # T9
        "settlement_weight_kg": result.settlement_weight_kg,         # T10
        "rate": result.rate,                                         # T11
        "currency": result.currency,                                 # T12
        "air_freight_cost": result.air_freight_cost,                 # T13
        "is_pivot": result.is_pivot,                                 # T14
        "remark": result.remark,                                     # T15
        "valid_until": result.valid_until,                           # T16
        # 扩展输出
        "provider_name": result.provider_name,
        "origin": result.origin,
        "destination": result.destination,
        "billing_factor": result.billing_factor,
        "rounding_rule": result.rounding_rule.value,
        "all_candidates": [
            {
                "tier": c.tier_name,
                "settlement_weight_kg": c.settlement_weight,
                "unit_price": c.unit_price,
                "cost": c.cost,
                "is_pivot": c.is_pivot,
                "is_recommended": c.is_recommended,
            }
            for c in result.all_candidates
        ],
    }


def export_json(result: EstimateResult) -> str:
    """导出为 JSON 字符串"""
    return _json.dumps(result_to_dict(result), ensure_ascii=False, indent=2)


def compare_result_to_dict(cr: CompareResult) -> dict:
    """将多服务商比价结果转换为字典"""
    return {
        "shared_cargo": {
            "gross_weight_kg": cr.gross_weight_kg,
            "total_volume_cbm": cr.total_volume_cbm,
            "volumetric_weight_kg": cr.volumetric_weight_kg,
            "bubble_ratio": cr.bubble_ratio,
            "raw_chargeable_weight_kg": cr.raw_chargeable_weight_kg,
            "rounded_chargeable_weight_kg": cr.rounded_chargeable_weight_kg,
            "calculation_mode": cr.calculation_mode.value,
        },
        "shared_rules": {
            "billing_factor": cr.billing_factor,
            "rounding_rule": cr.rounding_rule.value,
            "allow_pivot": cr.allow_pivot,
        },
        "currency_consistent": cr.currency_consistent,
        "remark": cr.remark,
        "rankings": [
            {
                "rank": item.rank,
                "provider_name": item.provider_name,
                "airline": item.airline,
                "transit_time_days": item.transit_time_days,
                "currency": item.result.currency,
                "recommended_tier": item.tier,
                "settlement_weight_kg": item.result.settlement_weight_kg,
                "rate": item.result.rate,
                "air_freight_cost": item.cost,
                "is_pivot": item.is_pivot,
                "confidence": item.result.confidence.value,
                "rec_label": item.rec_label.value if item.rec_label != RecLabel.NONE else "",
                "remark": item.result.remark,
            }
            for item in cr.items
        ],
    }


def export_compare_json(cr: CompareResult) -> str:
    """导出比价结果为 JSON 字符串"""
    return _json.dumps(compare_result_to_dict(cr), ensure_ascii=False, indent=2)


def format_compare_result(cr: CompareResult) -> str:
    """将多服务商比价结果格式化为可读文本"""
    lines = []
    lines.append("=" * 70)
    lines.append("  空运多服务商比价结果")
    lines.append("=" * 70)
    lines.append("")

    # 货物信息
    lines.append("【统一货物信息】")
    lines.append(f"  总实重:       {cr.gross_weight_kg:.1f} kg")
    if cr.total_volume_cbm is not None:
        lines.append(f"  总体积:       {cr.total_volume_cbm:.4f} m³")
    if cr.volumetric_weight_kg is not None:
        lines.append(f"  体积重:       {cr.volumetric_weight_kg:.1f} kg")
    lines.append(f"  取整后计费重: {cr.rounded_chargeable_weight_kg:.1f} kg")
    lines.append(f"  计算模式:     {cr.calculation_mode.value}")
    lines.append("")

    # 比价表格
    if cr.currency_consistent:
        lines.append("【比价排名】(按空运成本升序)")
    else:
        lines.append("【比价排名】(币种不一致，按币种分组排列)")

    header = (
        f"  {'排名':<4} {'服务商':<12} {'航司/渠道':<12} "
        f"{'重量段':<6} {'结算重':>7} {'单价':>8} {'成本':>12} "
        f"{'靠级':<4} {'时效':<6} {'标签':<16}"
    )
    lines.append(header)
    lines.append("  " + "-" * 90)
    for item in cr.items:
        r = item.result
        cost_str = f"{r.currency} {item.cost:>9.2f}"
        pivot_str = "是" if item.is_pivot else "否"
        time_str = f"{item.transit_time_days}天" if item.transit_time_days else "-"
        label_str = item.rec_label.value if item.rec_label != RecLabel.NONE else ""
        lines.append(
            f"  {item.rank:<4} "
            f"{item.provider_name:<12} "
            f"{item.airline:<12} "
            f"{item.tier:<6} "
            f"{r.settlement_weight_kg:>7.1f} "
            f"{r.rate:>8.2f} "
            f"{cost_str:>12} "
            f"{pivot_str:<4} "
            f"{time_str:<6} "
            f"{label_str:<16}"
        )
    lines.append("")

    # 备注
    if cr.remark:
        lines.append("【比价备注】")
        for note in cr.remark:
            lines.append(f"  · {note}")
        lines.append("")

    lines.append("=" * 70)
    return "\n".join(lines)


# ============================================================
# 格式化输出
# ============================================================

def format_result(result: EstimateResult) -> str:
    """将预估结果格式化为可读文本"""
    lines = []
    lines.append("=" * 60)
    lines.append("  空运重量段成本预估结果")
    lines.append("=" * 60)
    lines.append("")

    # 元信息
    if result.provider_name:
        lines.append(f"  服务商: {result.provider_name}")
    if result.origin or result.destination:
        lines.append(f"  航线:    {result.origin} → {result.destination}")
    lines.append(f"  币种:    {result.currency}")
    if result.valid_until:
        lines.append(f"  有效期:  {result.valid_until}")
    lines.append(f"  模式:    {result.calculation_mode.value}  取整: {result.rounding_rule.value}")
    lines.append("")

    # 货物信息
    lines.append("【货物信息】")
    lines.append(f"  总实重:             {result.gross_weight_kg:.1f} kg")
    if result.total_volume_cbm is not None:
        lines.append(f"  总体积:             {result.total_volume_cbm:.4f} m³")
    if result.volumetric_weight_kg is not None:
        lines.append(f"  体积重:             {result.volumetric_weight_kg:.1f} kg")
    if result.bubble_ratio is not None:
        lines.append(f"  泡比:               {result.bubble_ratio:.2f}")
    lines.append(f"  原始计费重(T7):     {result.raw_chargeable_weight_kg:.1f} kg")
    lines.append(f"  取整后计费重(T8):   {result.rounded_chargeable_weight_kg:.1f} kg")
    lines.append("")

    # 候选方案对比
    lines.append("【方案对比】(按流程图 T9-T14)")
    header = f"  {'段名':<8} {'结算重(kg)':>10} {'单价':>10} {'成本':>12} {'靠级':>6} {'推荐':>6}"
    lines.append(header)
    lines.append("  " + "-" * 55)
    for c in result.all_candidates:
        unit_str = f"{c.unit_price:.2f}/票" if c.tier_name == "Min" else f"{c.unit_price:.2f}/kg"
        pivot_mark = "✓" if c.is_pivot else "-"
        rec_mark = "★" if c.is_recommended else ""
        lines.append(
            f"  {c.tier_name:<8} "
            f"{c.settlement_weight:>10.1f} "
            f"{unit_str:>10} "
            f"{result.currency} {c.cost:>10.2f} "
            f"{pivot_mark:>6} "
            f"{rec_mark:>6}"
        )
    lines.append("")

    # 推荐结果
    lines.append("【推荐方案】")
    lines.append(f"  推荐重量段(T9):   {result.recommended_tier}")
    lines.append(f"  结算计费重(T10):  {result.settlement_weight_kg:.1f} kg")
    price_desc = f"{result.rate:.2f} 元/票" if result.recommended_tier == "Min" else f"{result.rate:.2f} 元/kg"
    lines.append(f"  适用单价(T11):    {price_desc}")
    lines.append(f"  空运成本(T13):    {result.currency} {result.air_freight_cost:.2f}")
    lines.append(f"  是否靠级(T14):    {'是' if result.is_pivot else '否'}")
    lines.append(f"  可信度(T2):       {result.confidence.value}")
    lines.append("")

    # 备注
    if result.remark:
        lines.append("【备注】(T15)")
        for i, note in enumerate(result.remark, 1):
            lines.append(f"  {i}. {note}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


# ============================================================
# CLI 入口
# ============================================================

def main():
    """命令行入口：支持交互模式和 JSON 参数模式"""
    import sys

    args = sys.argv[1:]

    # JSON 管道模式 — 多服务商比价
    if ("--compare" in args or "-c" in args):
        try:
            data = _json.loads(sys.stdin.read())
        except _json.JSONDecodeError:
            print(_json.dumps({"error": "需要 JSON 输入"}, ensure_ascii=False))
            return 1

        try:
            # 共享货物信息
            gross_weight_kg = float(data["gross_weight_kg"])
            pieces = int(data["pieces"]) if "pieces" in data else None
            length_cm = float(data["length_cm"]) if "length_cm" in data else None
            width_cm = float(data["width_cm"]) if "width_cm" in data else None
            height_cm = float(data["height_cm"]) if "height_cm" in data else None
            total_volume_cbm = float(data["total_volume_cbm"]) if "total_volume_cbm" in data else None

            # 共享计算规则
            billing_factor = float(data.get("billing_factor", DEFAULT_BILLING_FACTOR))
            rr_str = data.get("rounding_rule", "ceil_1kg")
            rr_map = {
                "ceil_1kg": RoundingRule.CEIL_1KG,
                "ceil_0.5kg": RoundingRule.CEIL_05KG,
                "keep_decimal": RoundingRule.KEEP_DECIMAL,
            }
            rounding_rule = rr_map.get(rr_str, RoundingRule.CEIL_1KG)
            allow_pivot = str(data.get("allow_pivot", "true")).lower() != "false"

            # 服务商列表
            providers_raw = data.get("providers", [])
            if not providers_raw:
                raise ValueError("providers 数组不能为空")

            providers = []
            for p in providers_raw:
                providers.append(ProviderConfig(
                    provider_name=p.get("provider_name", ""),
                    airline=p.get("airline", ""),
                    transit_time_days=int(p["transit_time_days"]) if "transit_time_days" in p and p["transit_time_days"] else None,
                    origin=p.get("origin", ""),
                    destination=p.get("destination", ""),
                    currency=p.get("currency", "CNY"),
                    valid_until=p.get("valid_until"),
                    rate_min=float(p["min"]) if "min" in p and p["min"] else None,
                    rate_n=float(p["n"]) if "n" in p and p["n"] else None,
                    rate_45=float(p["t45"]) if "t45" in p and p["t45"] else None,
                    rate_100=float(p["t100"]) if "t100" in p and p["t100"] else None,
                    rate_300=float(p["t300"]) if "t300" in p and p["t300"] else None,
                    rate_500=float(p["t500"]) if "t500" in p and p["t500"] else None,
                    rate_1000=float(p["t1000"]) if "t1000" in p and p["t1000"] else None,
                ))

            result = compare_providers(
                gross_weight_kg=gross_weight_kg,
                pieces=pieces, length_cm=length_cm, width_cm=width_cm, height_cm=height_cm,
                total_volume_cbm=total_volume_cbm,
                billing_factor=billing_factor, rounding_rule=rounding_rule, allow_pivot=allow_pivot,
                providers=providers,
            )
            print(export_compare_json(result))
            return 0
        except Exception as e:
            print(_json.dumps({"error": str(e)}, ensure_ascii=False))
            return 1

    # JSON 管道模式 — 单服务商
    if "--json" in args or "-j" in args:
        try:
            data = _json.loads(sys.stdin.read())
        except _json.JSONDecodeError:
            data = {}
            for arg in args:
                if "=" in arg:
                    k, v = arg.split("=", 1)
                    data[k] = v

        try:
            gross_weight_kg = float(data.get("gross_weight_kg", 0))
            if gross_weight_kg <= 0:
                raise ValueError("gross_weight_kg 必须大于 0")

            # 计算模式参数
            pieces = int(data["pieces"]) if "pieces" in data else None
            length_cm = float(data["length_cm"]) if "length_cm" in data else None
            width_cm = float(data["width_cm"]) if "width_cm" in data else None
            height_cm = float(data["height_cm"]) if "height_cm" in data else None
            total_volume_cbm = float(data["total_volume_cbm"]) if "total_volume_cbm" in data else None
            billing_factor = float(data.get("billing_factor", DEFAULT_BILLING_FACTOR))

            # 取整规则
            rr_str = data.get("rounding_rule", "ceil_1kg")
            rr_map = {
                "ceil_1kg": RoundingRule.CEIL_1KG,
                "ceil_0.5kg": RoundingRule.CEIL_05KG,
                "keep_decimal": RoundingRule.KEEP_DECIMAL,
            }
            rounding_rule = rr_map.get(rr_str, RoundingRule.CEIL_1KG)

            allow_pivot = str(data.get("allow_pivot", "true")).lower() != "false"

            result = estimate(
                provider_name=data.get("provider_name", ""),
                origin=data.get("origin", ""),
                destination=data.get("destination", ""),
                currency=data.get("currency", "CNY"),
                valid_until=data.get("valid_until"),
                gross_weight_kg=gross_weight_kg,
                billing_factor=billing_factor,
                pieces=pieces,
                length_cm=length_cm,
                width_cm=width_cm,
                height_cm=height_cm,
                total_volume_cbm=total_volume_cbm,
                rounding_rule=rounding_rule,
                allow_pivot=allow_pivot,
                rate_min=float(data["min"]) if "min" in data and data["min"] else None,
                rate_n=float(data["n"]) if "n" in data and data["n"] else None,
                rate_45=float(data["t45"]) if "t45" in data and data["t45"] else None,
                rate_100=float(data["t100"]) if "t100" in data and data["t100"] else None,
                rate_300=float(data["t300"]) if "t300" in data and data["t300"] else None,
                rate_500=float(data["t500"]) if "t500" in data and data["t500"] else None,
                rate_1000=float(data["t1000"]) if "t1000" in data and data["t1000"] else None,
            )
            print(export_json(result))
            return 0
        except Exception as e:
            print(_json.dumps({"error": str(e)}, ensure_ascii=False))
            return 1

    # ---- 交互模式 ----
    print("=" * 60)
    print("  空运重量段成本预估器 v2.0 - CLI")
    print("=" * 60)
    print()

    try:
        provider_name = input("服务商名称 (可选): ").strip()
        origin = input("起运地 (可选): ").strip()
        destination = input("目的地 (可选): ").strip()
        currency = input("币种 (默认 CNY): ").strip() or "CNY"
        valid_until = input("报价有效期 (可选, 格式 YYYY-MM-DD): ").strip() or None

        gross_weight_kg = float(input("\n总实重 (kg): ").strip())

        use_cbm = input("使用 CBM 计算体积重? (y/n, 默认 n=使用尺寸): ").strip().lower()
        pieces = None
        length_cm = width_cm = height_cm = None
        total_volume_cbm = None

        if use_cbm == 'y':
            total_volume_cbm = float(input("总体积 CBM (m³): ").strip())
        else:
            use_dims = input("提供件数+长宽高? (y/n, 默认 n=仅实重): ").strip().lower()
            if use_dims == 'y':
                pieces = int(input("件数: ").strip())
                length_cm = float(input("单件长 (cm): ").strip())
                width_cm = float(input("单件宽 (cm): ").strip())
                height_cm = float(input("单件高 (cm): ").strip())

        billing_factor_str = input(f"计费系数 (默认 {DEFAULT_BILLING_FACTOR}): ").strip()
        billing_factor = float(billing_factor_str) if billing_factor_str else DEFAULT_BILLING_FACTOR

        print("\n取整规则:")
        print("  1 - ceil_1kg (向上取整到1kg, 默认)")
        print("  2 - ceil_0.5kg (向上取整到0.5kg)")
        print("  3 - keep_decimal (保留小数)")
        rr_choice = input("选择 (1/2/3): ").strip()
        rr_map = {"1": RoundingRule.CEIL_1KG, "2": RoundingRule.CEIL_05KG, "3": RoundingRule.KEEP_DECIMAL}
        rounding_rule = rr_map.get(rr_choice, RoundingRule.CEIL_1KG)

        bump_str = input("\n允许靠级? (y/n, 默认 y): ").strip().lower()
        allow_pivot = bump_str != 'n'

        print("\n请输入重量段报价（留空表示该段无报价）:")
        min_str = input("  Min  最低消费 (元/票): ").strip()
        n_str = input("  N    <45kg 单价 (元/kg): ").strip()
        t45_str = input("  +45  ≥45kg 单价 (元/kg): ").strip()
        t100_str = input("  +100 ≥100kg 单价 (元/kg): ").strip()
        t300_str = input("  +300 ≥300kg 单价 (元/kg): ").strip()
        t500_str = input("  +500 ≥500kg 单价 (元/kg): ").strip()
        t1000_str = input("  +1000 ≥1000kg 单价 (元/kg): ").strip()

        result = estimate(
            provider_name=provider_name,
            origin=origin,
            destination=destination,
            currency=currency,
            valid_until=valid_until,
            gross_weight_kg=gross_weight_kg,
            billing_factor=billing_factor,
            pieces=pieces,
            length_cm=length_cm,
            width_cm=width_cm,
            height_cm=height_cm,
            total_volume_cbm=total_volume_cbm,
            rounding_rule=rounding_rule,
            allow_pivot=allow_pivot,
            rate_min=float(min_str) if min_str else None,
            rate_n=float(n_str) if n_str else None,
            rate_45=float(t45_str) if t45_str else None,
            rate_100=float(t100_str) if t100_str else None,
            rate_300=float(t300_str) if t300_str else None,
            rate_500=float(t500_str) if t500_str else None,
            rate_1000=float(t1000_str) if t1000_str else None,
        )

        print()
        print(format_result(result))

    except ValueError as e:
        print(f"\n错误: {e}")
        return 1
    except KeyboardInterrupt:
        print("\n\n已取消")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
