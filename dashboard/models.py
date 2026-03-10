from sqlalchemy import Column, Date, Index, String, DateTime, Text, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase
import uuid
from datetime import datetime


class Base(DeclarativeBase):
    pass


class CuratedAggregate(Base):
    __tablename__ = "curated_aggregates"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String)
    dataset_key = Column(String)
    grain = Column(String)
    metrics = Column(JSONB)
    fingerprint = Column(Text)
    computed_at = Column(DateTime, default=datetime.utcnow)


class SalesOrderSnapshot(Base):
    __tablename__ = "sales_order_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sales_order", name="uq_tenant_sales_order"),
        # BTREE on last_changed_at for date-range pre-filtering in _query_filtered_snapshots
        Index("ix_so_snap_tenant_changed", "tenant_id", "last_changed_at"),
        # BTREE on creation_date for fast date-range filtering (replaces regex on JSONB)
        Index("ix_so_snap_tenant_cdate", "tenant_id", "creation_date"),
        # GIN on payload for JSONB operator queries (payload @> ...)
        Index("ix_so_snap_payload_gin", "payload", postgresql_using="gin"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False)
    sales_order = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)
    creation_date = Column(Date, nullable=True)  # materialized from payload CreationDate
    last_changed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class BillingSnapshot(Base):
    __tablename__ = "billing_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "billing_document", name="uq_tenant_billing_doc"),
        Index("ix_bill_snap_tenant_bdate", "tenant_id", "billing_date"),
        Index("ix_bill_snap_payload_gin", "payload", postgresql_using="gin"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False)
    billing_document = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)
    billing_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class JobRun(Base):
    __tablename__ = "job_runs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String)
    job_type = Column(String)
    status = Column(String)
    error_message = Column(Text, nullable=True)
    row_count = Column(Integer, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class DashboardWidget(Base):
    __tablename__ = "dashboard_widgets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    widget_type = Column(String, nullable=False)  # chart | table
    payload = Column(JSONB, nullable=False)
    source = Column(String, default="chatbot", nullable=False)
    is_active = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
