"""上下文构建器：从主题对话日志中构建 LLM 请求的 contexts，完全替代 AstrBot 原始 contexts。"""

from .store import MemoryStore


class ContextBuilder:
    def __init__(self, store: MemoryStore):
        self.store = store

    async def build_contexts(
        self,
        umo: str,
        topic_id: str,
        max_rounds: int = 30,
    ) -> list[dict]:
        """从某主题的 conversation_log.json 中构建 contexts。

        Args:
            umo: 用户标识
            topic_id: 主题 ID
            max_rounds: 最多包含多少轮对话（一轮 = 一对 user + assistant）

        Returns:
            OpenAI 格式的消息列表，如 [{"role": "user", "content": ...}, {"role": "assistant", "content": ...}, ...]
        """
        rounds = await self.store.load_conversation_log(umo, topic_id)

        if not rounds:
            return []

        # 只保留最近 N 轮
        if max_rounds and len(rounds) > max_rounds:
            rounds = rounds[-max_rounds:]

        # 转为 OpenAI 格式
        contexts: list[dict] = []
        for r in rounds:
            user_msg = r.get("user_message", "").strip()
            asst_msg = r.get("assistant_response", "").strip()
            if user_msg:
                contexts.append({"role": "user", "content": user_msg})
            if asst_msg:
                contexts.append({"role": "assistant", "content": asst_msg})

        return contexts
