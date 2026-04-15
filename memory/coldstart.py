"""冷启动：批量回放历史对话，复用 _process_round 逻辑构建初始记忆。"""

import json
from datetime import datetime, timedelta, timezone

from astrbot.api import logger

from .store import MemoryStore


class ColdStarter:
    def __init__(self, store: MemoryStore):
        """
        Args:
            store: MemoryStore 实例
        """
        self.store = store

    @staticmethod
    def _parse_datetime(ts) -> datetime | None:
        """将各种时间格式统一解析为 aware UTC datetime。

        支持 int/float Unix 时间戳、str ISO 格式（含 Z 后缀）和 datetime 对象。
        """
        try:
            if isinstance(ts, (int, float)):
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            if isinstance(ts, str):
                ts = ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_text(content) -> str:
        """从消息 content 中提取纯文本。

        AstrBot 的消息 content 有两种格式：
        - str: 纯文本消息
        - list: 多部分消息，如 [{"type": "think", ...}, {"type": "text", "text": "..."}]
        """
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        return ""

    @staticmethod
    def _is_command_message(message: str) -> bool:
        """判断消息是否为指令型消息（以 / 开头的命令）。"""
        stripped = message.strip()
        return bool(stripped and stripped[0] == "/")

    async def run(
        self,
        umo: str,
        conversation_manager,
        process_round_fn,
        days: int = 7,
        progress_callback=None,
    ) -> dict:
        """执行冷启动，从历史对话中提取轮次，逐轮调用 process_round_fn 处理。

        Args:
            umo: 用户标识
            conversation_manager: AstrBot 的 ConversationManager 实例
            process_round_fn: 异步函数，签名为 async (umo, user_message, assistant_response) -> None
            days: 扫描过去多少天的对话
            progress_callback: 进度回调

        Returns:
            统计信息 dict
        """
        stats = {
            "conversations_scanned": 0,
            "rounds_processed": 0,
            "errors": [],
        }

        try:
            # 仅用 umo 作为 user_id 查询，不传 platform_id，避免 platform_id
            # 不匹配导致过滤掉有效对话（例如数据库中存储的 platform_id 与
            # 从 umo 解析出的不一致）
            conversations = await conversation_manager.get_conversations(umo)
        except Exception as e:
            stats["errors"].append(f"获取历史对话失败: {e}")
            logger.error(f"[ColdStart] 获取历史对话失败: {e}")
            return stats

        # 过滤时间范围
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered_convs = []
        for conv in conversations:
            try:
                updated = getattr(conv, "updated_at", None)
                created = getattr(conv, "created_at", None)
                ts = self._parse_datetime(updated or created)
                if ts and ts >= cutoff:
                    filtered_convs.append(conv)
            except Exception as e:
                logger.warning(f"[ColdStart] 解析对话时间失败: {e}")
                continue

        stats["conversations_scanned"] = len(filtered_convs)
        logger.info(f"[ColdStart] 过滤后得到 {len(filtered_convs)} 个会话")

        # 提取所有轮次
        all_rounds = []
        for i, conv in enumerate(filtered_convs):
            if progress_callback:
                await progress_callback(
                    i + 1,
                    len(filtered_convs),
                    f"正在扫描第 {i + 1}/{len(filtered_convs)} 个会话...",
                )

            try:
                history = getattr(conv, "history", None)
                if history is None:
                    history = getattr(conv, "content", None)
                if history is None:
                    continue

                if isinstance(history, str):
                    history = json.loads(history)

                if not isinstance(history, list):
                    continue

                # 提取 user+assistant 对（跳过 tool 消息，处理 list 格式 content）
                text_messages = []
                for entry in history:
                    if not isinstance(entry, dict):
                        continue
                    role = entry.get("role", "")
                    if role not in ("user", "assistant"):
                        continue
                    text = self._extract_text(entry.get("content", ""))
                    if text:
                        text_messages.append({"role": role, "text": text})

                # 提取对话级时间戳（history 消息无单独时间戳，用对话的 created_at）
                conv_ts = None
                for ts_field in ("created_at", "updated_at"):
                    ts_val = getattr(conv, ts_field, None)
                    if ts_val is not None:
                        dt = self._parse_datetime(ts_val)
                        if dt:
                            conv_ts = dt.isoformat()
                            break

                # 相邻 user+assistant 配对（跳过指令型消息）
                j = 0
                while j < len(text_messages) - 1:
                    msg = text_messages[j]
                    resp = text_messages[j + 1]
                    if msg["role"] == "user" and resp["role"] == "assistant":
                        # 跳过指令型消息
                        if self._is_command_message(msg["text"]):
                            j += 2
                            continue
                        all_rounds.append(
                            {
                                "user_message": msg["text"],
                                "assistant_response": resp["text"],
                                "timestamp": conv_ts,
                            }
                        )
                        j += 2
                    else:
                        j += 1
            except Exception as e:
                stats["errors"].append(f"解析会话 {i} 失败: {e}")
                logger.warning(f"[ColdStart] 解析会话 {i} 失败: {e}")
                continue

        stats["rounds_processed"] = len(all_rounds)
        logger.info(f"[ColdStart] 提取了 {len(all_rounds)} 轮对话，开始逐轮处理...")

        # 按时间正序处理（从旧到新），模拟真实对话顺序
        all_rounds.reverse()

        # 逐轮调用 process_round_fn（与正常运行的逻辑完全一致）
        for i, rnd in enumerate(all_rounds):
            if progress_callback:
                await progress_callback(
                    i + 1,
                    len(all_rounds),
                    f"正在处理第 {i + 1}/{len(all_rounds)} 轮对话...",
                )

            try:
                await process_round_fn(
                    umo,
                    rnd["user_message"],
                    rnd["assistant_response"],
                    rnd.get("timestamp"),
                )

            except Exception as e:
                stats["errors"].append(f"处理第 {i} 轮失败: {e}")
                logger.warning(f"[ColdStart] 处理第 {i} 轮失败: {e}")
                continue

        return stats
