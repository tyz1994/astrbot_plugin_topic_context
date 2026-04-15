"""对话轮次总结器：调用 LLM 对每轮对话提取主题+摘要，并判断是否值得记忆。"""

import json
from dataclasses import dataclass, field

from astrbot.api import logger


@dataclass
class SummaryResult:
    """对话总结结果。"""

    worth_remembering: bool
    topic_name: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    is_negative_feedback: bool = False
    negative_feedback_summary: str = ""
    overview: str = ""
    key_info: str = ""


class Summarizer:
    def __init__(self, llm_caller):
        """
        Args:
            llm_caller: 异步函数，签名为 async (system_prompt, prompt) -> str
        """
        self.llm_caller = llm_caller

    async def summarize(
        self,
        user_message: str,
        assistant_response: str,
        existing_topics: list[dict] | None = None,
        message_date: str = "",
        store=None,
        umo: str = "",
    ) -> SummaryResult:
        """对一轮对话进行总结，同时匹配已有主题。

        Args:
            user_message: 用户消息
            assistant_response: 助手回复
            existing_topics: 已有主题列表，每个元素为 {"name": ..., "id": ..., ...}
            message_date: 消息的日期时间（ISO 格式），用于在摘要中使用绝对日期
            store: MemoryStore 实例，用于加载 core.md 概述
            umo: 用户标识

        Returns:
            SummaryResult，其中 worth_remembering=False 表示该轮不值得记忆。
        """
        # 构建已有主题描述，使用 core.md 概述而非关键词
        topics_desc = ""
        if existing_topics:
            topic_lines = []
            for t in existing_topics:
                entry = f"- {t['name']}"
                # 加载 core.md 中的概述
                if store and umo:
                    core_md = await store.load_core_md(umo, t["id"])
                    if core_md:
                        lines = core_md.split("\n")
                        # 加载概述
                        in_summary = False
                        summary_text = ""
                        # 加载关键信息
                        in_key_info = False
                        key_info_text = ""
                        for line in lines:
                            if line.strip() == "## 概述":
                                in_summary = True
                                in_key_info = False
                                continue
                            if line.strip() == "## 关键信息":
                                in_key_info = True
                                in_summary = False
                                continue
                            if in_summary:
                                if line.startswith("## "):
                                    in_summary = False
                                else:
                                    summary_text += line + "\n"
                            if in_key_info:
                                if line.startswith("## "):
                                    in_key_info = False
                                else:
                                    key_info_text += line + "\n"
                        summary_text = summary_text.strip()
                        if summary_text:
                            entry += f"\n  概述: {summary_text}"
                        key_info_text = key_info_text.strip()
                        if key_info_text:
                            entry += f"\n  关键信息: {key_info_text}"
                topic_lines.append(entry)

            topics_desc = "\n已有主题列表（请优先匹配）：\n" + "\n".join(topic_lines)
            topics_desc += "\n\n如果对话内容明显属于某个已有主题，topic_name 必须使用该已有主题的名称（一字不差）。\n如果对话内容不属于任何已有主题，再创建新的 topic_name。"

        # 日期提示
        date_hint = ""
        if message_date:
            date_hint = f"\n当前对话日期: {message_date}\n"

        prompt = f"""请分析以下一轮对话，以 JSON 格式返回分析结果。
{date_hint}{topics_desc}
用户消息:
{user_message}

助手回复:
{assistant_response}

请返回如下 JSON 格式（不要包含 markdown 代码块标记）：
{{
  "worth_remembering": true/false,
  "topic_name": "主题名称（简短，2-5个字。如有匹配的已有主题，必须使用已有主题名称）",
  "summary": "2-3句话的关键信息摘要",
  "keywords": ["关键词1", "关键词2", ...],
  "is_negative_feedback": true/false,
  "negative_feedback_summary": "如果用户表达了不满或纠正，简述用户反馈的内容，否则为空字符串",
  "overview": "如果本轮对话使得该主题的概述需要更新，输出更新后的完整概述。不需要更新则为空字符串。",
  "key_info": "如果本轮对话包含值得记录的新关键信息（偏好、习惯、决定、里程碑等时效性不强的持久信息），以 - 开头输出新增条目，多条用换行分隔。不需要新增则为空字符串。"
}}

判断标准：
- worth_remembering: 如果是寒暄、闲聊、"你好"/"谢谢"等无实质内容的对话，返回 false。只有包含有意义信息的对话才返回 true。
- topic_name: 概括这轮对话的主题。如果已有主题列表中有匹配的主题，必须使用该主题的名称。如果没有匹配的已有主题，创建一个新名称。
- is_negative_feedback: 如果用户表达了不满、纠正了错误、或者表示"不对"/"不是这样"等，返回 true。
- negative_feedback_summary: 当 is_negative_feedback 为 true 时，总结用户不满的具体内容。
- overview: 仅当本轮对话显著改变了主题的进展或状态时才更新。不需要频繁更新。
- key_info: 只提取时效性不强的持久性信息。不要重复已有信息。宁可留空也不要凑数。

重要：在 summary 中必须使用绝对日期（如"2025年3月15日"），禁止使用"今天"、"昨天"、"明天"、"上周"等相对时间。如果用户提到了相对时间，请根据当前对话日期换算为绝对日期后写入 summary。
- 角色区分：用户消息中的称呼（如"某某，帮我..."）是用户在叫助手，不是用户的名字。summary 中统一用"用户"指代使用者，禁止把用户对助手的称呼当作用户名字写入 summary。"""

        try:
            result_text = await self.llm_caller(
                system_prompt="你是一个对话分析助手。你只返回 JSON 格式的分析结果，不包含其他文字。",
                prompt=prompt,
                caller_name="Summarizer.summarize",
            )
            # 清理可能的 markdown 包裹
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

            # 类型校验与清洗：LLM 返回的 JSON 字段类型不可信
            def _bool(v, default=False) -> bool:
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.lower() in ("true", "1", "yes")
                return default

            def _str(v, default="") -> str:
                return str(v) if v is not None else default

            def _str_list(v) -> list[str]:
                if isinstance(v, list):
                    return [str(item) for item in v if item is not None]
                if isinstance(v, str):
                    # LLM 可能返回逗号分隔的字符串而非列表
                    return [s.strip() for s in v.split(",") if s.strip()]
                return []

            return SummaryResult(
                worth_remembering=_bool(data.get("worth_remembering")),
                topic_name=_str(data.get("topic_name")),
                summary=_str(data.get("summary")),
                keywords=_str_list(data.get("keywords")),
                is_negative_feedback=_bool(data.get("is_negative_feedback")),
                negative_feedback_summary=_str(data.get("negative_feedback_summary")),
                overview=_str(data.get("overview")),
                key_info=_str(data.get("key_info")),
            )
        except json.JSONDecodeError as e:
            logger.warning(
                f"[Summarizer] JSON 解析失败: {e}, 原文: {result_text[:200]}"
            )
            return SummaryResult(worth_remembering=False)
        except Exception as e:
            logger.error(f"[Summarizer] 总结失败: {e}")
            return SummaryResult(worth_remembering=False)
