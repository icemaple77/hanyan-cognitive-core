"""Health check endpoint."""

from fastapi import APIRouter
from pydantic import BaseModel

from gateway.core.vector_guard import get_last_report

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    service: str
    # 向量维度自检:不一致时 status 变 degraded,并在此列出走岔的列。既有的
    # hcc_health_probe.py 探针据此邮件告警,不必等人肉发现(2026-08-29 事故里
    # documents 静默死了 5 天)。
    vector_dims: dict | None = None


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    report = get_last_report()
    degraded = report.get("checked") and not report.get("ok")
    return HealthResponse(
        status="degraded" if degraded else "ok",
        version="0.1.0",
        service="hanyan-cognitive-core",
        vector_dims=report if report.get("checked") else None,
    )
