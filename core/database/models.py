"""ORM-модели (расширяемый слой поверх legacy SQLite)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database.engine import Base


class PluginStateRow(Base):
    __tablename__ = "orm_plugin_state"

    uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DeliveryLogRow(Base):
    __tablename__ = "orm_delivery_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    product_name: Mapped[str] = mapped_column(String(256))
    buyer_username: Mapped[str] = mapped_column(String(128), default="")
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
