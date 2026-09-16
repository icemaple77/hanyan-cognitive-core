"""Model Router — assigns the right model to each cognitive module.

Each module (Memory, Emotion, Dream, Planner, etc.) can be configured
to use a different model provider/model. Falls back gracefully.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default model assignments (can all be overridden via env vars)
# 兜底(profile 里没配这个 module 时才用到)。2026-09-16 起只写本机真实存在的
# tag —— 旧默认值 qwen3:8b / qwen3:14b / gpt-4o 一个都拉不起来,等于让
# core/local_llm.py 必定失败、模块静默退回模板,排查时非常费解。
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "memory": {
        "provider": os.getenv("HCC_MODEL_MEMORY", "local"),
        "model": os.getenv("HCC_MODEL_MEMORY_MODEL", "nf-filter:0.8b-r2"),
        "priority": "fast",
    },
    "emotion": {
        "provider": os.getenv("HCC_MODEL_EMOTION", "local"),
        "model": os.getenv("HCC_MODEL_EMOTION_MODEL", "qwen3.5:2b"),
        "priority": "fast",
    },
    "dream": {
        "provider": os.getenv("HCC_MODEL_DREAM", "local"),
        "model": os.getenv("HCC_MODEL_DREAM_MODEL", "qwen3.5:4b"),
        "priority": "quality",
    },
    "planner": {
        "provider": os.getenv("HCC_MODEL_PLANNER", "local"),
        "model": os.getenv("HCC_MODEL_PLANNER_MODEL", "qwen3.5:4b"),
        "priority": "quality_first",
    },
    "ocr": {
        "provider": os.getenv("HCC_MODEL_OCR", "local"),
        "model": os.getenv("HCC_MODEL_OCR_MODEL", "qwen2.5vl:7b"),
        "priority": "specialized",
    },
    "summary": {
        "provider": os.getenv("HCC_MODEL_SUMMARY", "local"),
        "model": os.getenv("HCC_MODEL_SUMMARY_MODEL", "qwen3.5:4b"),
        "priority": "balanced",
    },
    "embedding": {
        "provider": os.getenv("HCC_MODEL_EMBEDDING", "local"),
        "model": os.getenv("HCC_MODEL_EMBEDDING_MODEL", "BAAI/bge-base-zh-v1.5"),
        "priority": "fast",
    },
}

# Hardware profiles for automatic model selection
HARDWARE_PROFILES: dict[str, dict[str, Any]] = {
    # 2026-09-16 对齐现实:旧表里的 qwen3:8b / qwen3:14b / bge-m3 本机一个都没有
    # (`ollama list` 实际是 qwen3.5:* 系列),而 16GB 的 Mac mini 要同时供网关、
    # 嵌入、降噪,8B 常驻扛不住。现在每个条目都是本机真实存在、且实测过的 tag。
    "macmini_m4": {
        # 降噪:蒸馏出的 0.8B,常驻 0.83GB,单条 0.47s(实验见
        # ~/workspace/experiments/nf-distill-20260915/report.html)
        "memory": "nf-filter:0.8b-r2", "emotion": "qwen3.5:2b",
        # 做梦写日记:实测只有 4b 真的在用当晚的记忆片段写,0.8b/2b 会编
        # (~/workspace/experiments/dream-model-bench/)。一天只在 03:00 调一次,
        # 2.86GB 是瞬时占用,写完即卸。
        "dream": "qwen3.5:4b", "summary": "qwen3.5:4b",
        "planner": "qwen3.5:4b", "embedding": "BAAI/bge-base-zh-v1.5",
        "max_parallel": 2, "dream_enabled": True,
    },
    # Umbrella(RTX 5080 16GB):只在它醒着时用,夜间 dreaming 不依赖它 ——
    # 为写一篇日记去 WOL 唤醒一台台式机不划算。
    "rtx5080": {
        "memory": "qwen35b-a3b:latest", "emotion": "qwen3.5:4b",
        "dream": "qwen27b:latest", "summary": "qwen35b-a3b:latest",
        "planner": "qwen35b-a3b:latest", "embedding": "BAAI/bge-base-zh-v1.5",
        "max_parallel": 4, "dream_enabled": True,
    },
    "cloud": {
        "memory": "gpt-4o-mini", "emotion": "gpt-4o-mini", "dream": "gpt-4o",
        "planner": "gpt-4o", "embedding": "text-embedding-3-small",
        "max_parallel": 8, "dream_enabled": True,
    },
}


@dataclass
class ModelAssignment:
    """Which model to use for a specific module."""
    module: str
    provider: str
    model: str
    priority: str
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.7


class ModelRouter:
    """Routes tasks to the appropriate model based on module and config.

    Supports:
    - Per-module model configuration via env vars
    - Hardware profiles for automatic scaling
    - Graceful fallback when models are unavailable
    """

    def __init__(self, profile: str | None = None):
        self._profile = profile or os.getenv("HCC_HARDWARE_PROFILE", "macmini_m4")
        self._overrides: dict[str, dict[str, str]] = {}

    def get_model(self, module: str) -> ModelAssignment:
        """Get the model assignment for a specific module.

        Resolution order:
        1. Runtime overrides (set by user/admin)
        2. Environment-specific model config
        3. Hardware profile default
        4. Global default
        """
        # 1. Check runtime overrides
        if module in self._overrides:
            cfg = self._overrides[module]
            return ModelAssignment(
                module=module,
                provider=cfg.get("provider", "local"),
                model=cfg.get("model", "qwen3:8b"),
                priority=cfg.get("priority", "balanced"),
            )

        # 2. Check env vars
        env_model = os.getenv(f"HCC_MODEL_{module.upper()}")
        env_provider = os.getenv(f"HCC_MODEL_{module.upper()}_PROVIDER")
        if env_model or env_provider:
            return ModelAssignment(
                module=module,
                provider=env_provider or "local",
                model=env_model or "qwen3:8b",
                priority="custom",
            )

        # 3. Check hardware profile
        profile = HARDWARE_PROFILES.get(self._profile, HARDWARE_PROFILES["macmini_m4"])
        profile_model = profile.get(module)
        if profile_model:
            return ModelAssignment(
                module=module,
                provider="local",
                model=profile_model,
                priority="profile",
            )

        # 4. Fall back to default
        defaults = DEFAULT_MODELS.get(module, DEFAULT_MODELS["memory"])
        return ModelAssignment(
            module=module,
            provider=defaults.get("provider", "local"),
            model=defaults.get("model", "qwen3:8b"),
            priority=defaults.get("priority", "balanced"),
        )

    def set_override(self, module: str, provider: str | None = None,
                     model: str | None = None) -> None:
        """Set a runtime override for a module."""
        if module not in self._overrides:
            self._overrides[module] = {}
        if provider:
            self._overrides[module]["provider"] = provider
        if model:
            self._overrides[module]["model"] = model
        logger.info("model_router: override %s -> %s/%s", module,
                    provider or "default", model or "default")

    def clear_overrides(self) -> None:
        """Clear all runtime overrides."""
        self._overrides.clear()

    def set_profile(self, profile: str) -> None:
        """Switch hardware profile at runtime."""
        if profile in HARDWARE_PROFILES:
            self._profile = profile
            logger.info("model_router: switched to profile %s", profile)

    def get_profile_summary(self) -> dict[str, Any]:
        """Get summary of all module assignments."""
        profile = HARDWARE_PROFILES.get(self._profile, {})
        return {
            "active_profile": self._profile,
            "modules": {
                module: {
                    "provider": self.get_model(module).provider,
                    "model": self.get_model(module).model,
                    "priority": self.get_model(module).priority,
                }
                for module in DEFAULT_MODELS
            },
            "profile_config": {
                "dream_enabled": profile.get("dream_enabled", True),
                "max_parallel": profile.get("max_parallel", 2),
            },
        }


# Singleton
_router: ModelRouter | None = None


def get_model_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router
