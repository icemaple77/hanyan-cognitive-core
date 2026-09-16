"""本地模型文本生成的唯一入口(Ollama /api/generate)。

在此之前 HCC 没有通用的大模型调用路径:``core/local_filter.py`` 自己拿 httpx
写了一份降噪专用的调用,``core/model_router.py`` 只返回配置、没有任何消费者,
而它配的 ``qwen3:8b`` / ``qwen3:14b`` 在本机根本不存在。本模块把"调哪个模型"
交给 router,把"怎么调"收在一处,让 dreaming 这类模块能真的用上本地模型。

契约与 local_filter 一致,包括那条硬前提:qwen3.5 是混合思考模型,不传
``think: false`` 的话整个 num_predict 预算会烧在思考链上,``response`` 是空的。

**永不抛异常**:任何失败返回 ``None``,调用方退回自己的模板/规则路径。
dreaming 不能因为模型没起来就整晚失败。
"""

from __future__ import annotations

import logging

import httpx

from core.config import core_settings
from core.model_router import get_model_router

logger = logging.getLogger(__name__)

__all__ = ["generate"]


async def generate(
    module: str,
    prompt: str,
    *,
    max_tokens: int = 400,
    temperature: float = 0.7,
    timeout: float | None = None,
) -> str | None:
    """用 ``module``(dream/summary/...)配的本地模型生成一段文本。

    失败返回 None —— 连接不上、超时、模型不存在、输出为空,调用方一律
    按"没有模型"处理。
    """
    assignment = get_model_router().get_model(module)
    if assignment.provider not in ("local", "ollama"):
        logger.warning("local_llm: module=%s 配的是非本地 provider=%s,跳过", module, assignment.provider)
        return None

    payload = {
        "model": assignment.model,
        "prompt": prompt,
        "think": False,  # 同 local_filter:不关思考,response 会是空的
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout or core_settings.local_llm_timeout) as client:
            resp = await client.post(f"{core_settings.noise_filter_ollama_url}/api/generate", json=payload)
            resp.raise_for_status()
            text = (resp.json().get("response") or "").strip()
    except Exception:
        logger.warning("local_llm: module=%s model=%s 调用失败,调用方退回模板", module, assignment.model, exc_info=True)
        return None

    if not text:
        logger.warning("local_llm: module=%s model=%s 返回空文本", module, assignment.model)
        return None
    return text
