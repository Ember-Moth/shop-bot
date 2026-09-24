"""仓储层：按聚合划分的数据访问对象，通过 ``Database`` 的属性使用。"""

from .base import Repository
from .deliveries import DeliveryRepository
from .operations import OperationsRepository
from .orders import OrderRepository
from .payments import PaymentRepository
from .products import ProductRepository
from .purchases import PurchaseRepository
from .users import UserRepository
from .wallet import WalletRepository
from .work import WorkRepository

__all__ = [
    "DeliveryRepository",
    "OperationsRepository",
    "OrderRepository",
    "PaymentRepository",
    "ProductRepository",
    "PurchaseRepository",
    "Repository",
    "UserRepository",
    "WalletRepository",
    "WorkRepository",
]
