"""把 Commbitz 已分配套餐同步成本地商品目录。

开发方案 7.3：目录可见不等于可售。同步只维护名称、SKU、上游套餐 ID；
本店售价、币种与上架状态由管理员本地维护。新同步的商品 0 价且下架，
需管理员定价并上架后才对用户可见。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..db import Database
from ..logging_config import get_logger
from .commbitz_api import CommbitzError, parse_plan

logger = get_logger(__name__)


class PlanSource(Protocol):
    """目录来源最小接口；CommbitzClient 结构化满足，测试可注入 fake。"""

    async def get_all_plans(self, **filters: Any) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class SyncResult:
    total: int
    created: int
    updated: int
    skipped: int


def _request_type_for(sim_category: str) -> str | None:
    # planIsFor 枚举未完全定义（开发方案 7.3），先用 simCategory 推导可确定的两类；
    # 其他类型等业务确认后再启用。
    if sim_category == "esim":
        return "esim"
    if sim_category == "physicalsim":
        return "physical"
    return None


async def sync_catalog(db: Database, client: PlanSource) -> SyncResult:
    result = SyncResult(total=0, created=0, updated=0, skipped=0)
    raw_plans = await client.get_all_plans()
    result.total = len(raw_plans)
    for raw in raw_plans:
        try:
            plan = parse_plan(raw)
        except CommbitzError as exc:
            logger.warning("catalog sync: malformed plan skipped: %s", exc)
            result.skipped += 1
            continue
        if not plan.sku:
            logger.warning("catalog sync: plan without sku skipped (plan_id=%s)", plan.plan_id)
            result.skipped += 1
            continue
        # 上游报价（USD）不写进本地价格；币种与扣款规则未经采购对账验证
        description = f"{plan.sim_category or 'unknown'} · planIsFor={plan.plan_is_for}"
        created = await db.upsert_product_from_upstream(
            sku=plan.sku,
            name=plan.name,
            description=description,
            upstream_plan_id=plan.plan_id,
            request_type=_request_type_for(plan.sim_category),
        )
        if created:
            result.created += 1
        else:
            result.updated += 1
    logger.info(
        "catalog synced: total=%s created=%s updated=%s skipped=%s",
        result.total,
        result.created,
        result.updated,
        result.skipped,
    )
    return result
