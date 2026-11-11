"""AstrBot 长期记忆插件。

功能概览：
- 按用户独立存储对话，兼容 OneBot（aiocqhttp）与 QQ 官方机器人；
- 使用 LLM 抽取记忆、关键词和用户画像，SQLite 持久化；
- 对话前按关键词检索并注入提示词；
- 支持图片理解、群聊按用户记忆、每日定时整理与分区存储；
- 提供适配手机端的 WebUI，可管理记忆、画像和关键字。

插件不拦截消息、不修改其他插件的处理结果，所有能力通过事件钩子、
插件公开方法和可选的 LLM 工具提供。
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

from .identity import resolve_identity
from .memory_engine import MemoryEngine
from .memory_store import MemoryStore, today_str
from .tools import RecallMemoryTool, SaveMemoryTool

try:  # AstrBot v4.30+
    from astrbot.api.agents import TextPart
except ImportError:  # pragma: no cover - 兼容旧版本
    from astrbot.core.agent.message import TextPart  # type: ignore

try:  # AstrBot v4.x 新版插件 Web API
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover - 极旧版本没有可视化视图
    error_response = None  # type: ignore
    json_response = None  # type: ignore
    request = None  # type: ignore

try:
    from astrbot.api.util import get_astrbot_plugin_data_path
except ImportError:  # pragma: no cover
    from astrbot.core.utils.astrbot_path import (  # type: ignore
        get_astrbot_plugin_data_path,
    )


PLUGIN_NAME = "astrbot_plugin_longterm_memory"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SETTING_SPECS: dict[str, tuple[str, str, str]] = {
    # key: (分组, 配置名, 类型)
    "enabled": ("basic", "enabled", "bool"),
    "capture_group_all": ("basic", "capture_group_all", "bool"),
    "enable_image_understanding": ("basic", "enable_image_understanding", "bool"),
    "max_images_per_message": ("basic", "max_images_per_message", "int"),
    "ignore_commands": ("basic", "ignore_commands", "bool"),
    "enable_auto_extract": ("memory", "enable_auto_extract", "bool"),
    "extract_every_n_messages": ("memory", "extract_every_n_messages", "int"),
    "consolidation_time": ("memory", "consolidation_time", "str"),
    "catch_up_on_start": ("memory", "catch_up_on_start", "bool"),
    "profile_refresh_days": ("memory", "profile_refresh_days", "int"),
    "profile_min_messages": ("memory", "profile_min_messages", "int"),
    "retention_days": ("memory", "retention_days", "int"),
    "inject_mode": ("injection", "inject_mode", "str"),
    "max_inject_memories": ("injection", "max_inject_memories", "int"),
    "max_inject_chars": ("injection", "max_inject_chars", "int"),
    "max_profile_chars": ("injection", "max_profile_chars", "int"),
    "enable_llm_tools": ("advanced", "enable_llm_tools", "bool"),
    "llm_provider_id": ("advanced", "llm_provider_id", "str"),
    "vision_provider_id": ("advanced", "vision_provider_id", "str"),
    "debug": ("advanced", "debug", "bool"),
}

_SETTING_LIMITS = {
    "max_images_per_message": (0, 4),
    "extract_every_n_messages": (1, 100),
    "profile_refresh_days": (1, 365),
    "profile_min_messages": (1, 1000),
    "retention_days": (0, 3650),
    "max_inject_memories": (1, 20),
    "max_inject_chars": (200, 8000),
    "max_profile_chars": (200, 4000),
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开启", "是"}
    return default


def _normalize_image_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if ref.startswith("file://"):
        try:
            from urllib.parse import unquote, urlparse

            return unquote(urlparse(ref).path)
        except Exception:  # noqa: BLE001
            return ref
    return ref


class LongTermMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.store = MemoryStore(data_dir / "memory.db")
        self.engine = MemoryEngine(
            self.store,
            self._provider_getter,
            self._cfg,
            self.logger,
        )
        self._scheduler_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._daily_lock = asyncio.Lock()
        self._last_daily_run = ""
        self._warned_time = False
        self._register_web_apis()
        self._register_tools()

    # ------------------------------------------------------------------
    # 配置与基础设施
    # ------------------------------------------------------------------
    def _cfg(self, path: str, default: Any = None) -> Any:
        value: Any = self.config
        for part in path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value if value is not None else default

    def _flag(self, path: str, default: bool) -> bool:
        return _as_bool(self._cfg(path, default), default)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _provider_getter(
        self, umo: str | None = None, provider_id: str | None = None
    ) -> Any:
        pid = (provider_id or str(self._cfg("advanced.llm_provider_id", "") or "")).strip()
        if pid:
            provider = self.context.get_provider_by_id(pid)
            if provider is not None:
                return provider
        provider = None
        try:
            provider = await self.context.get_using_provider_async(umo=umo)
        except TypeError:  # pragma: no cover - 旧版本同步接口
            provider = self.context.get_using_provider(umo=umo)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"长期记忆：获取当前 Provider 失败：{exc}")
        if provider is not None:
            return provider
        get_all = getattr(self.context, "get_all_providers", None)
        providers = get_all() if callable(get_all) else []
        return providers[0] if providers else None

    def _register_tools(self) -> None:
        if not self._flag("advanced.enable_llm_tools", True):
            return
        add_tools = getattr(self.context, "add_llm_tools", None)
        if not add_tools:
            return
        try:
            add_tools(
                RecallMemoryTool(engine=self.engine),
                SaveMemoryTool(store=self.store),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"长期记忆：注册 LLM 工具失败：{exc}")

    # ------------------------------------------------------------------
    # 消息捕获与提示词注入
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent) -> None:
        """可选的群聊全量记录：只在开启 capture_group_all 时工作。"""
        if not self._flag("basic.enabled", True):
            return
        if not self._flag("basic.capture_group_all", False):
            return
        if not event.get_group_id() or event.get_extra("ltm_captured"):
            return
        text = (event.message_str or "").strip()
        images = self._collect_images(event)
        if not text and not images:
            return
        if self._flag("basic.ignore_commands", True) and text.startswith("/"):
            return
        ident = resolve_identity(event)
        event.set_extra("ltm_captured", True)
        await self._capture_user_message(event, ident, text, images)
        await self._maybe_trigger_extraction(ident)

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._flag("basic.enabled", True):
            return
        await self._ensure_scheduler()

        text = (event.message_str or "").strip()
        images = self._collect_images(event)
        if not text and not images:
            return

        ident = resolve_identity(event)
        if not event.get_extra("ltm_captured"):
            event.set_extra("ltm_captured", True)
            await self._capture_user_message(event, ident, text, images)

        if not event.get_extra("ltm_injected"):
            event.set_extra("ltm_injected", True)
            if not (
                self._flag("basic.ignore_commands", True)
                and text.startswith("/")
            ):
                await self._inject_context(event, req, ident, text)

        if self._flag("memory.enable_auto_extract", True):
            await self._maybe_trigger_extraction(ident)

    async def _maybe_trigger_extraction(self, ident: Any) -> None:
        if not self._flag("memory.enable_auto_extract", True):
            return
        threshold = max(1, int(self._cfg("memory.extract_every_n_messages", 8)))
        pending = await asyncio.to_thread(self.store.count_unprocessed, ident.user_key)
        if pending >= threshold:
            self._spawn(self.engine.process_pending(ident.user_key))

    async def _capture_user_message(
        self,
        event: AstrMessageEvent,
        ident: Any,
        text: str,
        images: list[str],
    ) -> None:
        if not self._should_capture(event):
            return
        ignore_commands = self._flag("basic.ignore_commands", True)
        if ignore_commands and text.startswith("/"):
            return
        await asyncio.to_thread(
            self.store.touch_user,
            ident.user_key,
            ident.platform,
            ident.platform_id,
            ident.platform_user_id,
            ident.nickname,
            ident.qq,
            True,
        )
        message_id = getattr(event.message_obj, "message_id", "") or ""
        row_id = await asyncio.to_thread(
            self.store.add_message,
            ident.user_key,
            ident.platform,
            "user",
            text,
            platform_msg_id=str(message_id),
            group_id=ident.group_id,
            session_id=ident.session_id,
            has_image=bool(images),
        )
        event.set_extra("ltm_message_row", row_id)
        event.set_extra("ltm_user_key", ident.user_key)
        if images and self._flag("basic.enable_image_understanding", True):
            self._spawn(self._describe_images(row_id, images, event.unified_msg_origin))

    def _should_capture(self, event: AstrMessageEvent) -> bool:
        if not event.get_group_id():
            return True
        if self._flag("basic.capture_group_all", False):
            return True
        return bool(event.is_at_or_wake_command or event.is_wake_up())

    async def _describe_images(
        self, row_id: int, images: list[str], umo: str
    ) -> None:
        try:
            refs = [_normalize_image_ref(ref) for ref in images if ref]
            desc = await self.engine.describe_image(refs, umo=umo)
            if desc:
                await asyncio.to_thread(self.store.set_message_image_desc, row_id, desc)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"长期记忆：图片理解失败：{exc}")

    async def _inject_context(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        ident: Any,
        text: str,
    ) -> None:
        mode = str(self._cfg("injection.inject_mode", "extra_user_part") or "").strip()
        if mode == "off":
            return
        try:
            block = await self.engine.build_context(ident.user_key, text)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"长期记忆：检索记忆失败：{exc}")
            return
        if not block:
            return
        if mode == "system_prompt":
            req.system_prompt = f"{req.system_prompt}\n\n{block}".strip()
            return
        try:
            part = TextPart(text=block)
            mark = getattr(part, "mark_as_temp", None)
            if callable(mark):
                mark()
            req.extra_user_content_parts.append(part)
        except Exception:  # pragma: no cover - 极端兼容场景
            req.system_prompt = f"{req.system_prompt}\n\n{block}".strip()

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        user_key = event.get_extra("ltm_user_key")
        if not user_key:
            return
        try:
            text = (resp.completion_text or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        if not text:
            return
        ident = resolve_identity(event)
        await asyncio.to_thread(
            self.store.add_message,
            user_key,
            ident.platform,
            "assistant",
            text,
            group_id=ident.group_id,
            session_id=ident.session_id,
        )

    def _collect_images(self, event: AstrMessageEvent) -> list[str]:
        images: list[str] = []
        try:
            messages = event.get_messages()
        except Exception:  # noqa: BLE001
            messages = []
        for component in messages:
            if not isinstance(component, Comp.Image):
                continue
            ref = (
                getattr(component, "url", "")
                or getattr(component, "path", "")
                or getattr(component, "file", "")
            )
            if ref:
                images.append(str(ref))
        return images

    # ------------------------------------------------------------------
    # 定时整理
    # ------------------------------------------------------------------
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self._ensure_scheduler()

    async def _ensure_scheduler(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(5)
        if self._flag("memory.catch_up_on_start", True):
            self._spawn(self._catch_up())
        while True:
            try:
                target_time = str(self._cfg("memory.consolidation_time", "00:00") or "00:00")
                if not TIME_RE.match(target_time):
                    if not self._warned_time:
                        self.logger.warning(
                            f"长期记忆：整理时间 {target_time!r} 无效，已回退到 00:00。"
                        )
                        self._warned_time = True
                    target_time = "00:00"
                now = datetime.now()
                today = now.date().isoformat()
                if now.strftime("%H:%M") == target_time and self._last_daily_run != today:
                    self._last_daily_run = today
                    self._spawn(self._run_daily(today))
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"长期记忆：定时任务异常：{exc}")
            await asyncio.sleep(20)

    async def _catch_up(self) -> None:
        await asyncio.sleep(15)
        provider = await self._provider_getter()
        if provider is None:
            self.logger.info("长期记忆：尚无可用模型，跳过启动补整理。")
            return
        if self._daily_lock.locked():
            return
        async with self._daily_lock:
            days = await asyncio.to_thread(self.store.unconsolidated_days, 30)
            today = today_str()
            targets = [item["day"] for item in days if item["day"] < today][:7]
            for day in reversed(targets):
                await self.engine.consolidate_day(day)

    async def _run_daily(self, today: str) -> None:
        if self._daily_lock.locked():
            return
        async with self._daily_lock:
            days = await asyncio.to_thread(self.store.unconsolidated_days, 30)
            targets = [item["day"] for item in days if item["day"] < today][:7]
            for day in reversed(targets):
                result = await self.engine.consolidate_day(day)
                self.logger.info(f"长期记忆：{day} 整理完成：{result}")
                # 清理过期数据只在每日任务中做一次
                await asyncio.to_thread(
                    self.store.purge_old,
                    int(self._cfg("memory.retention_days", 0)),
                )

    # ------------------------------------------------------------------
    # Web API
    # ------------------------------------------------------------------
    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not register or json_response is None:
            self.logger.warning(
                "长期记忆：当前 AstrBot 版本不支持插件 Web API，可视化页面不可用。"
            )
            return
        routes = [
            (f"/{PLUGIN_NAME}/overview", self.api_overview, ["GET"], "记忆概览"),
            (f"/{PLUGIN_NAME}/users", self.api_users, ["GET"], "用户列表"),
            (f"/{PLUGIN_NAME}/user", self.api_user_detail, ["GET"], "用户详情"),
            (f"/{PLUGIN_NAME}/memories", self.api_memories, ["GET"], "记忆列表"),
            (f"/{PLUGIN_NAME}/memory/delete", self.api_delete_memory, ["POST"], "删除记忆"),
            (f"/{PLUGIN_NAME}/keyword/delete", self.api_delete_keyword, ["POST"], "删除关键字"),
            (f"/{PLUGIN_NAME}/profile/delete", self.api_delete_profile, ["POST"], "删除画像"),
            (f"/{PLUGIN_NAME}/user/delete", self.api_delete_user, ["POST"], "删除用户数据"),
            (f"/{PLUGIN_NAME}/consolidate", self.api_consolidate, ["POST"], "整理记忆"),
            (f"/{PLUGIN_NAME}/settings", self.api_get_settings, ["GET"], "读取设置"),
            (f"/{PLUGIN_NAME}/settings/save", self.api_save_settings, ["POST"], "保存设置"),
        ]
        for route, handler, methods, desc in routes:
            try:
                register(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"长期记忆：注册 Web API {route} 失败：{exc}")

    async def api_overview(self) -> Any:
        stats = await asyncio.to_thread(self.store.stats)
        recent = await asyncio.to_thread(self.store.recent_memories, 8)
        recent_users, _ = await asyncio.to_thread(self.store.list_users, "", 6, 0)
        return json_response(
            {
                "stats": stats,
                "recent_memories": recent,
                "recent_users": recent_users,
                "settings": {
                    "enabled": self._flag("basic.enabled", True),
                    "consolidation_time": self._cfg("memory.consolidation_time", "00:00"),
                },
            }
        )

    async def api_users(self) -> Any:
        search = request.query.get("search", "") or ""
        limit = request.query.get("limit", 30, type=int)
        offset = request.query.get("offset", 0, type=int)
        limit = max(1, min(100, int(limit or 30)))
        offset = max(0, int(offset or 0))
        users, total = await asyncio.to_thread(
            self.store.list_users, search, limit, offset
        )
        return json_response({"items": users, "total": total, "limit": limit, "offset": offset})

    async def api_user_detail(self) -> Any:
        user_key = request.query.get("key", "") or ""
        if not user_key:
            return error_response("缺少用户 key", status_code=400)
        user = await asyncio.to_thread(self.store.get_user, user_key)
        if not user:
            return error_response("用户不存在", status_code=404)
        limit = max(1, min(100, int(request.query.get("limit", 30, type=int) or 30)))
        offset = max(0, int(request.query.get("offset", 0, type=int) or 0))
        memories, total = await asyncio.to_thread(
            self.store.list_memories,
            user_key,
            limit=limit,
            offset=offset,
        )
        keywords = await asyncio.to_thread(self.store.get_keyword_counts, user_key, 100)
        summaries = await asyncio.to_thread(self.store.get_recent_summaries, user_key, 7)
        return json_response(
            {
                "user": user,
                "memories": memories,
                "memories_total": total,
                "keywords": keywords,
                "summaries": summaries,
                "limit": limit,
                "offset": offset,
            }
        )

    async def api_memories(self) -> Any:
        query = request.query.get("q", "") or ""
        keyword = request.query.get("keyword", "") or ""
        user_key = request.query.get("key", "") or ""
        limit = max(1, min(100, int(request.query.get("limit", 30, type=int) or 30)))
        offset = max(0, int(request.query.get("offset", 0, type=int) or 0))
        memories, total = await asyncio.to_thread(
            self.store.list_memories,
            user_key or None,
            query=query,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )
        users: dict[str, dict[str, Any]] = {}
        for item in memories:
            key = item["user_key"]
            if key not in users:
                users[key] = await asyncio.to_thread(self.store.get_user, key) or {}
            item["nickname"] = users[key].get("nickname") or ""
            item["qq"] = users[key].get("qq") or ""
        return json_response({"items": memories, "total": total, "limit": limit, "offset": offset})

    async def api_delete_memory(self) -> Any:
        payload = await request.json(default={})
        try:
            memory_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            memory_id = 0
        if memory_id <= 0:
            return error_response("无效的记忆 ID", status_code=400)
        ok = await asyncio.to_thread(self.store.delete_memory, memory_id)
        return json_response({"ok": bool(ok)})

    async def api_delete_keyword(self) -> Any:
        payload = await request.json(default={})
        try:
            memory_id = int(payload.get("memory_id") or 0)
        except (TypeError, ValueError):
            memory_id = 0
        keyword = str(payload.get("keyword") or "").strip()
        if memory_id <= 0 or not keyword:
            return error_response("参数不完整", status_code=400)
        ok = await asyncio.to_thread(self.store.delete_keyword, memory_id, keyword)
        return json_response({"ok": bool(ok)})

    async def api_delete_profile(self) -> Any:
        payload = await request.json(default={})
        user_key = str(payload.get("user_key") or "").strip()
        if not user_key:
            return error_response("缺少用户 key", status_code=400)
        await asyncio.to_thread(self.store.clear_profile, user_key)
        return json_response({"ok": True})

    async def api_delete_user(self) -> Any:
        payload = await request.json(default={})
        user_key = str(payload.get("user_key") or "").strip()
        if not user_key:
            return error_response("缺少用户 key", status_code=400)
        await asyncio.to_thread(self.store.delete_user, user_key)
        return json_response({"ok": True})

    async def api_consolidate(self) -> Any:
        payload = await request.json(default={})
        day = str(payload.get("day") or "").strip()
        if not day:
            day = (date.today() - timedelta(days=1)).isoformat()
        if not DATE_RE.match(day):
            return error_response("日期格式应为 YYYY-MM-DD", status_code=400)
        try:
            date.fromisoformat(day)
        except ValueError:
            return error_response("无效日期", status_code=400)
        if self._daily_lock.locked():
            return error_response("已有整理任务在运行，请稍后再试", status_code=409)
        async with self._daily_lock:
            result = await self.engine.consolidate_day(day)
        return json_response({"ok": True, "result": result})

    async def api_get_settings(self) -> Any:
        values = {
            key: self._cfg(f"{group}.{name}", "")
            for key, (group, name, _) in _SETTING_SPECS.items()
        }
        providers = []
        get_all = getattr(self.context, "get_all_providers", None)
        for provider in (get_all() if callable(get_all) else []):
            try:
                meta = provider.meta()
                providers.append(
                    {
                        "id": str(getattr(meta, "id", "") or ""),
                        "model": str(getattr(meta, "model", "") or ""),
                        "type": str(getattr(meta, "type", "") or ""),
                    }
                )
            except Exception:  # noqa: BLE001
                continue
        return json_response({"values": values, "providers": providers})

    async def api_save_settings(self) -> Any:
        payload = await request.json(default={})
        values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
        if not isinstance(values, dict):
            return error_response("请求体格式错误", status_code=400)
        errors: list[str] = []
        for key, value in values.items():
            spec = _SETTING_SPECS.get(key)
            if not spec:
                continue
            group, name, kind = spec
            if kind == "bool":
                value = _as_bool(value, False)
            elif kind == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    errors.append(f"{key} 必须是整数")
                    continue
                low, high = _SETTING_LIMITS.get(key, (-10**9, 10**9))
                value = max(low, min(high, value))
            else:
                value = str(value or "")
                if key == "consolidation_time" and not TIME_RE.match(value):
                    errors.append("整理时间格式应为 HH:MM")
                    continue
                if key == "inject_mode" and value not in {
                    "extra_user_part",
                    "system_prompt",
                    "off",
                }:
                    errors.append("注入方式无效")
                    continue
            self._set_config(f"{group}.{name}", value)
        if errors:
            return error_response("；".join(errors), status_code=400)
        await self._save_config()
        return json_response({"ok": True})

    def _set_config(self, path: str, value: Any) -> None:
        if not isinstance(self.config, dict):
            return
        node: Any = self.config
        parts = path.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    async def _save_config(self) -> None:
        save_async = getattr(self.config, "save_config_async", None)
        if callable(save_async):
            try:
                await save_async()
                return
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"长期记忆：保存配置失败：{exc}")
                return
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                await asyncio.to_thread(save)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"长期记忆：保存配置失败：{exc}")

    # ------------------------------------------------------------------
    # 对其他插件公开的接口
    # ------------------------------------------------------------------
    async def build_memory_prompt(self, user_key: str, query: str = "") -> str:
        """返回可直接拼接进提示词的画像 + 记忆文本（供其他插件调用）。"""
        return await self.engine.build_context(user_key, query)

    async def get_user_profile(self, user_key: str) -> dict[str, Any] | None:
        """读取用户画像与标签。"""
        return await asyncio.to_thread(self.store.get_user, user_key)

    async def search_memories(
        self, user_key: str, query: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        """按关键字/文本检索某位用户的记忆。"""
        items, _ = await asyncio.to_thread(
            self.store.list_memories,
            user_key,
            query=query,
            limit=max(1, min(100, int(limit))),
            offset=0,
        )
        return items

    async def add_memory(
        self,
        user_key: str,
        content: str,
        keywords: list[str] | str | None = None,
        *,
        category: str = "其他",
        importance: int = 3,
        source: str = "plugin",
    ) -> int:
        """供其他插件写入一条记忆，返回记忆 ID。"""
        return await asyncio.to_thread(
            self.store.add_memory,
            user_key,
            content,
            keywords,
            category=category,
            importance=importance,
            partition_key="longterm",
            source=source,
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def terminate(self) -> None:
        """插件停用/卸载时清理后台任务并关闭数据库。"""
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        self.store.close()
