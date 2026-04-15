"""经验教训总结器：从用户负反馈中提取经验，写入 experience.md。"""

from datetime import datetime

from astrbot.api import logger

from .store import MemoryStore


class ExperienceManager:
    def __init__(self, llm_caller, store: MemoryStore):
        """
        Args:
            llm_caller: 异步函数，签名为 async (system_prompt, prompt) -> str
            store: MemoryStore 实例
        """
        self.llm_caller = llm_caller
        self.store = store

    async def extract_experience(
        self,
        umo: str,
        topic_id: str,
        topic_name: str,
        user_message: str,
        assistant_response: str,
        feedback_summary: str,
    ) -> str | None:
        """从负反馈中提取经验教训。

        Args:
            umo: 用户标识
            topic_id: 主题 ID
            topic_name: 主题名称
            user_message: 用户的消息（包含负反馈）
            assistant_response: 助手的回复（被反馈的回复）
            feedback_summary: 负反馈的摘要

        Returns:
            新增的经验条目文本，如果无法提取则有意义的经验则返回 None
        """
        date_str = datetime.now().strftime("%Y年%m月%d日")

        # 加载已有经验
        existing = await self.store.load_experience_md(umo, topic_id)

        prompt = f"""请从以下负反馈中提取一条经验教训。

当前日期: {date_str}
主题: {topic_name}

用户反馈内容: {feedback_summary}
用户消息: {user_message}
助手回复: {assistant_response}

已有经验文件:
{existing if existing else "(无)"}

请分析这次负反馈，提取一条简短、具体、可操作的经验教训。
要求：
- 用一个简短标题概括经验（如"不要过度使用术语"）
- 说明具体的背景（用户说了什么/做了什么）
- 给出明确的改进方向
- 如果已有经验文件中已有类似经验，请指出并返回空字符串
- 只返回经验条目文本，不需要 JSON 格式
- 背景描述中使用绝对日期，禁止使用"今天"、"昨天"等相对时间

格式：
### [简短标题]
[背景描述]
→ [改进方向]"""

        try:
            result = await self.llm_caller(
                system_prompt="你是一个经验总结助手。从用户的负反馈中提取可操作的经验教训。",
                prompt=prompt,
                caller_name="ExperienceManager.extract_experience",
            )
            result = result.strip()

            if not result or len(result) < 10:
                return None

            # 追加到 experience.md
            new_content = f"\n\n{result}"
            updated = (
                existing + new_content
                if existing
                else f"# 主题: {topic_name} - 经验教训\n\n## 经验\n{result}"
            )
            await self.store.save_experience_md(umo, topic_id, updated)

            return result

        except Exception as e:
            logger.error(f"[Experience] 经验提取失败: {e}")
            return None
