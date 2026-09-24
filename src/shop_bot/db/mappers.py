"""模型层：sqlite 行到领域模型（``models.py`` dataclass）的映射。"""

from __future__ import annotations

import aiosqlite

from ..models import (
    BalanceTransaction,
    Order,
    OrderStatus,
    Product,
    Purchase,
    PurchaseState,
    Topup,
    TopupState,
    User,
)


def row_to_user(row: aiosqlite.Row) -> User:
    keys = row.keys()
    return User(
        id=row["id"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        display_name=row["display_name"] if "display_name" in keys else None,
        balance_cents=row["balance_cents"],
        created_at=row["created_at"],
    )


def row_to_product(row: aiosqlite.Row) -> Product:
    return Product(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        price_cents=row["price_cents"],
        currency=row["currency"],
        active=bool(row["active"]),
        sku=row["sku"],
        upstream_plan_id=row["upstream_plan_id"],
        request_type=row["request_type"],
    )


def row_to_purchase(row: aiosqlite.Row) -> Purchase:
    return Purchase(
        id=row["id"],
        order_id=row["order_id"],
        state=PurchaseState(row["state"]),
        request_type=row["request_type"],
        sku=row["sku"],
        quantity=row["quantity"],
        upstream_request_id=row["upstream_request_id"],
        upstream_order_no=row["upstream_order_no"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        kyc_documents=row["kyc_documents"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_topup(row: aiosqlite.Row) -> Topup:
    return Topup(
        id=row["id"],
        user_id=row["user_id"],
        currency=row["currency"],
        amount_cents=row["amount_cents"],
        status=TopupState(row["status"]),
        trade_no=row["trade_no"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def row_to_balance_tx(row: aiosqlite.Row) -> BalanceTransaction:
    return BalanceTransaction(
        id=row["id"],
        user_id=row["user_id"],
        currency=row["currency"],
        amount_cents=row["amount_cents"],
        balance_after=row["balance_after"],
        kind=row["kind"],
        order_id=row["order_id"],
        topup_id=row["topup_id"],
        note=row["note"],
        created_at=row["created_at"],
    )


def row_to_order(row: aiosqlite.Row) -> Order:
    return Order(
        id=row["id"],
        user_id=row["user_id"],
        product_id=row["product_id"],
        quantity=row["quantity"],
        amount_cents=row["amount_cents"],
        currency=row["currency"],
        status=OrderStatus(row["status"]),
        upstream_ref=row["upstream_ref"],
        trade_no=row["trade_no"],
        payload=row["payload"],
        notified_at=row["notified_at"],
        notification_pending=bool(row["notification_pending"]),
        input_iccid=row["input_iccid"],
        input_msisdn=row["input_msisdn"],
        input_days=row["input_days"],
        input_sku=row["input_sku"],
        input_request_type=row["input_request_type"],
        input_plan_id=row["input_plan_id"],
        payment_method=row["payment_method"],
        delivery_esims=row["delivery_esims"],
        notification_cursor=row["notification_cursor"],
        notification_retry_at=row["notification_retry_at"],
        notification_plan_version=row["notification_plan_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
