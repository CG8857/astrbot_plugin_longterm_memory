"""可选的 LLM 函数工具，方便模型或其他插件按需读写记忆。"""

from __future__ import annotations

from dataclasses import field
from typing import Any

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent
from pydantic.dataclasses import dataclass

from .identity import resolve_identity


@dataclass
class RecallMemoryTool(FunctionTool):
    name: str = "recall_user_memory"
    description: str = (
        "检索当前用户的长期记忆和画像。当需要回忆用户之前说过的偏好、经历、"
        "关系或长期信息时使用。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要检索的内容或关键词，例如“咖啡偏好”。",
                },
            },
            "required": ["query"],
        }
    )
    engine: Any = None

    async def run(self, event: AstrMessageEvent, query: str) -> str:
        if self.engine is None:
            return "长期记忆功能不可用。"
        ident = resolve_identity(event)
        context = await self.engine.build_context(ident.user_key, query or "")
        if not context:
            return "没有找到与该用户相关的长期记忆。"
        return context


@dataclass
class SaveMemoryTool(FunctionTool):
    name: str = "save_user_memory"
    description: str = (
        "把用户明确要求记住的信息写入长期记忆。仅当用户表达“记住……”或"
        "提供了值得长期保存的重要信息时使用。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记住的一条完整信息。",
                },
                "keywords": {
                    "type": "string",
                    "description": "2~5 个检索关键词，用逗号分隔。",
                },
                "category": {
                    "type": "string",
                    "description": "分类：偏好、生活、工作、关系、计划、事实、其他。",
                },
                "importance": {
                    "type": "number",
                    "description": "重要程度 1~5，默认 3。",
                },
            },
            "required": ["content"],
        }
    )
    store: Any = None

    async def run(
        self,
        event: AstrMessageEvent,
        content: str,
        keywords: str = "",
        category: str = "其他",
        importance: float = 3,
    ) -> str:
        if self.store is None:
            return "长期记忆功能不可用。"
        content = (content or "").strip()
        if not content:
            return "没有可保存的内容。"
        ident = resolve_identity(event)
        self.store.touch_user(
            ident.user_key,
            ident.platform,
            ident.platform_id,
            ident.platform_user_id,
            nickname=ident.nickname,
            qq=ident.qq,
            increment=False,
        )
        memory_id = self.store.add_memory(
            ident.user_key,
            content,
            keywords,
            category=category,
            importance=int(importance or 3),
            partition_key="longterm",
            source="manual",
            group_id=ident.group_id,
        )
        return f"已写入长期记忆（#{memory_id}）。" if memory_id else "写入失败。"
