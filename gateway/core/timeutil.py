"""统一的时区口径：库里一律 UTC-naive，**展示时**转成「公子当前时区」。

诊断（2026-09-29）：
    ``created_at``/``updated_at``/``last_access`` 三列都是 ``timestamp without time
    zone``，写入的一律是 **UTC naive**（``datetime.now(timezone.utc).replace(tzinfo=None)``，
    models 默认值 + 各 ``_now()`` 全一致）。也就是说**存储本来就是对的**，不用动。
    真正的两个毛病是：
    1. SQL 会话时区是本机（Australia/Brisbane），``now()`` 返回本地 —— 与 naive UTC
       的列直接比较会差 10 小时（2026-09-29 08:15 那次假阴性就出在这）；
    2. 展示层把 naive UTC 当本地渲染 —— 给人看时差 10 小时。
    本模块治的是第 2 个。

「公子当前时区」怎么定（按优先级）：
    1. 环境变量 ``HCC_USER_TIMEZONE``（显式钉死，需重启）
    2. 文件 ``~/.hanyanos/user_timezone``（**改完即时生效、不用重启** —— 公子出行时
       说一句「我到中国了」，改这个文件即可：``echo Asia/Shanghai > ~/.hanyanos/user_timezone``）
    3. 宿主时区（``/etc/localtime``，当前是 Australia/Brisbane）

为什么不用「自动检测位置」：消息经由本机投递，信封时间戳带的是**宿主**偏移，
拿不到发送方所在地；WeChat 也不给位置。所以做成人可切换的显式状态，最诚实。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

TZ_FILE = Path.home() / ".hanyanos" / "user_timezone"
ENV_VAR = "HCC_USER_TIMEZONE"
DEFAULT_TZ = "Australia/Brisbane"


def host_tz_name() -> str:
    """宿主时区的 IANA 名（从 /etc/localtime 反解）。"""
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except Exception:  # noqa: BLE001 - 纯尽カ而为，失败就退回默认
        logger.exception("resolve /etc/localtime failed")
    return DEFAULT_TZ


def user_tz_name() -> str:
    """解析顺序：环境变量 → 文件 → 宿主时区。"""
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        return env
    try:
        name = TZ_FILE.read_text(encoding="utf-8").strip()
        if name:
            return name
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("read %s failed", TZ_FILE)
    return host_tz_name()


def user_tz() -> ZoneInfo:
    name = user_tz_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("unknown timezone %r — falling back to %s", name, DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)


def to_local(dt: datetime | None) -> datetime | None:
    """naive(UTC) → 公子当前时区（带 tzinfo）。``None`` 原样返回。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(user_tz())


def to_local_iso(dt: datetime | None) -> str | None:
    """转成带偏移量的 ISO-8601（如 ``2026-09-29T08:20:00+10:00``）。

    带 offset 比裸 naive 更不容易被消费端误读——解析方拿到的是**绝对时刻**。
    """
    d = to_local(dt)
    return d.isoformat() if d is not None else None


def utcnow_naive() -> datetime:
    """库里统一用的「现在」（UTC naive）。新代码别再手写 ``datetime.now()``。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
