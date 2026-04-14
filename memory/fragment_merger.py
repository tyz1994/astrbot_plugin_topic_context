"""片段合并器：判断新轮次是否应与已有片段合并。"""

import json
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import logger

from .store import MemoryStore


@dataclass
class MergeResult:
    """合并判定结果。"""

    should_merge: bool
    merged_summary: str = ""  # 合并后的综合摘要（should_merge=True 时有值）
    merged_keywords: list[str] = None

    def __post_init__(self):
        if self.merged_keywords is None:
            self.merged_keywords = []


class FragmentMerger:
    def __init__(self, llm_caller, store: MemoryStore):
        """
        Args:
            llm_caller: 异步函数，签名为 async (system_prompt, prompt) -> str
            store: MemoryStore 实例
        """
        self.llm_caller = llm_caller
        self.store = store

    async def judge(
        self,
        umo: str,
        topic_id: str,
        topic_name: str,
        summary: str,
        keywords: list[str],
        round_data: dict,
    ) -> MergeResult:
        """判断新轮次是否应与同主题下最近的片段合并。

        Args:
            umo: 用户标识
            topic_id: 主题 ID
            topic_name: 主题名称
            summary: 新轮次的摘要
            keywords: 新轮次的关键词
            round_data: 新轮次的完整数据

        Returns:
            MergeResult
        """
        # 加载最近片段
        latest = await self.store.get_latest_fragment(umo, topic_id)
        if latest is None:
            # 没有已有片段，直接创建新的
            return MergeResult(should_merge=False)

        latest_summary = latest.get("summary", "")
        latest_round_count = len(latest.get("rounds", []))

        prompt = f"""请判断新的一轮对话是否应与已有记忆片段合并。

已有片段摘要: {latest_summary}（已包含 {latest_round_count} 轮对话）
新轮次摘要: {summary}

请判断新轮次是否应与已有片段合并：
- 如果新轮次是对已有话题的继续、深化、追问或补充，返回 "merge"
- 如果新轮次开启了明显不同的子话题或方向，或者已有片段已经涵盖了足够完整的一段讨论，返回 "new"

如果返回 "merge"，请同时生成合并后的综合摘要（覆盖旧摘要）。

请返回如下 JSON 格式（不要包含 markdown 代码块标记）：
{{
  "decision": "merge" 或 "new",
  "merged_summary": "合并后的综合摘要（仅 merge 时填写）"
}}

角色区分：用户消息中出现的称呼是用户在叫助手，不是用户自己的名字。merged_summary 中统一用"用户"指代使用者，禁止把用户对助手的称呼当作用户名字。

重要：merged_summary 中必须使用绝对日期（如"2025年3月15日"），禁止使用"今天"、"昨天"、"明天"、"上周"等相对时间。如果原文使用了相对时间，请保留原文中的绝对日期信息，不要自行引入相对时间。"""

        try:
            result_text = await self.llm_caller(
                system_prompt="你是一个记忆整理助手。你只返回 JSON 格式的结果，不包含其他文字。",
                prompt=prompt,
                caller_name="FragmentMerger.judge",
            )
            result_text = result_text.strip()
            if result_text.startswith("```"):
                result_text = (
                    result_text.split("\n", 1)[1]
                    if "\n" in result_text
                    else result_text[3:]
                )
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()

            data = json.loads(result_text)
            decision = data.get("decision", "new")
            merged_summary = data.get("merged_summary", "")

            if decision == "merge" and merged_summary:
                # 合并关键词
                old_keywords = {kw.lower() for kw in latest.get("keywords", [])}
                new_keywords = {kw.lower() for kw in keywords}
                merged_keywords = list(old_keywords | new_keywords)

                return MergeResult(
                    should_merge=True,
                    merged_summary=merged_summary,
                    merged_keywords=merged_keywords,
                )
            else:
                return MergeResult(should_merge=False)

        except json.JSONDecodeError as e:
            logger.warning(f"[FragmentMerger] JSON 解析失败: {e}")
            return MergeResult(should_merge=False)
        except Exception as e:
            logger.error(f"[FragmentMerger] 合并判定失败: {e}")
            return MergeResult(should_merge=False)

    async def merge_into(
        self,
        umo: str,
        topic_id: str,
        fragment: dict,
        round_data: dict,
        new_summary: str,
        new_keywords: list[str],
        ts: str = "",
    ) -> dict:
        """将新轮次合并到已有片段中。

        Args:
            umo: 用户标识
            topic_id: 主题 ID
            fragment: 已有片段
            round_data: 新轮次数据
            new_summary: 合并后的综合摘要
            new_keywords: 合并后的关键词列表
            ts: 消息时间戳（ISO 格式），用于 updated_at

        Returns:
            更新后的片段
        """
        fragment["rounds"].append(round_data)
        fragment["summary"] = new_summary
        fragment["keywords"] = new_keywords
        fragment["updated_at"] = ts or datetime.now().isoformat()

        await self.store.save_fragment(umo, topic_id, fragment)
        return fragment

    async def create_new(
        self,
        umo: str,
        topic_id: str,
        topic_name: str,
        summary: str,
        keywords: list[str],
        round_data: dict,
        ts: str = "",
    ) -> dict:
        """创建新片段。

        Args:
            umo: 用户标识
            topic_id: 主题 ID
            topic_name: 主题名称
            summary: 摘要
            keywords: 关键词
            round_data: 轮次数据
            ts: 消息时间戳（ISO 格式），用于 fragment ID 和 created_at/updated_at

        Returns:
            新创建的片段
        """
        frag_ts = ts or datetime.now().isoformat()
        fragment = {
            "id": MemoryStore.generate_fragment_id(ts),
            "created_at": frag_ts,
            "updated_at": frag_ts,
            "topic": topic_name,
            "summary": summary,
            "rounds": [round_data],
            "keywords": keywords,
        }

        await self.store.save_fragment(umo, topic_id, fragment)
        return fragment
