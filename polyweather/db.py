"""SQLAlchemy models. Every pricing output is persisted with its full input
snapshot so any historical price can be reproduced and scored."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, Float, Index, Integer,
                        String, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import DATA_DIR, DB_URL


class Base(DeclarativeBase):
    pass


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(16))        # metar | knmi | amedas
    station: Mapped[str] = mapped_column(String(32))       # ICAO / knmi code / amedas id
    ts_obs: Mapped[dt.datetime] = mapped_column(DateTime)  # UTC observation time
    ts_fetch: Mapped[dt.datetime] = mapped_column(DateTime)
    temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    dewp: Mapped[float | None] = mapped_column(Float, nullable=True)
    wdir: Mapped[float | None] = mapped_column(Float, nullable=True)
    wspd: Mapped[float | None] = mapped_column(Float, nullable=True)   # m/s
    rh: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[str | None] = mapped_column(String(512), nullable=True)
    __table_args__ = (
        Index("ix_obs_city_station_ts", "city", "station", "ts_obs", unique=True),
    )


class ModelForecast(Base):
    __tablename__ = "model_forecasts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(16), index=True)
    model: Mapped[str] = mapped_column(String(48))
    ts_fetch: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    climate_date: Mapped[str] = mapped_column(String(10))  # local date the forecast targets
    today_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    hourly: Mapped[dict] = mapped_column(JSON)             # {time:[iso..], temp:[..], ...}
    upper: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 925/850 data


class PricingVersion(Base):
    __tablename__ = "pricing_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(16), index=True)
    climate_date: Mapped[str] = mapped_column(String(10), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    phase: Mapped[str] = mapped_column(String(12))
    run_max_int: Mapped[int | None] = mapped_column(Integer, nullable=True)
    int_pmf: Mapped[dict] = mapped_column(JSON)            # {"24": 0.03, "25": 0.4, ...}
    inputs: Mapped[dict] = mapped_column(JSON)             # full snapshot for reproducibility
    signals: Mapped[dict] = mapped_column(JSON)
    explain: Mapped[list] = mapped_column(JSON)            # human-readable change log lines


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(16), index=True)
    climate_date: Mapped[str] = mapped_column(String(10))
    ts: Mapped[dt.datetime] = mapped_column(DateTime)
    event_slug: Mapped[str] = mapped_column(String(128))
    buckets: Mapped[list] = mapped_column(JSON)  # [{title, yes_price, lo, hi}] top-N by price


class SignalEvent(Base):
    __tablename__ = "signal_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(24))          # breeze_front | ceiling | residual | ...
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    payload: Mapped[dict] = mapped_column(JSON)


class LatencyLog(Base):
    __tablename__ = "latency_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(24), index=True)
    ts_fetch: Mapped[dt.datetime] = mapped_column(DateTime)
    ts_source: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    lag_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)


class DailyOutcome(Base):
    __tablename__ = "daily_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(16), index=True)
    climate_date: Mapped[str] = mapped_column(String(10))
    settle_int: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peak_time_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    scores: Mapped[dict] = mapped_column(JSON)  # {checkpoint_hour: {brier, logloss, pmf_top}}
    __table_args__ = (Index("ix_outcome_city_date", "city", "climate_date", unique=True),)


_engine = None
_Session: sessionmaker | None = None


def init_db(url: str = DB_URL):
    global _engine, _Session
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session() -> Session:
    if _Session is None:
        init_db()
    return _Session()


def upsert_observation(s: Session, **kw: Any) -> bool:
    """Insert observation if (city, station, ts_obs) unseen. Returns True if new."""
    q = s.query(Observation).filter_by(
        city=kw["city"], station=kw["station"], ts_obs=kw["ts_obs"]
    )
    if q.first() is not None:
        return False
    s.add(Observation(**kw))
    return True


def to_dict(obj) -> dict:
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    for k, v in d.items():
        if isinstance(v, dt.datetime):
            d[k] = v.replace(tzinfo=dt.timezone.utc).isoformat()
    return d
