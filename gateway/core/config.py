"""兼容垫片 —— 配置已统一到 :mod:`core.config`(2026-09-03 配置单一化)。

此处原本是第二个独立的 ``BaseSettings``(database_url / api_host / api_port /
debug),与 :class:`core.config.CoreSettings` 并存。两套配置各读各的 env,是
"同一个知识有两个真相源"的典型——其直接后果就是嵌入维度分裂(建表 1024、运行
时 768),把 documents 的语义检索整条打死。

现在 ``settings`` 就是 ``core_settings`` 本身。保留这个名字只为不惊动既有
import(如 gateway.core.database);新代码请直接 ``from core.config import
core_settings``。
"""

from core.config import core_settings

# 既有调用方(gateway.core.database 等)按此名 import。
settings = core_settings

__all__ = ["settings", "core_settings"]
