"""织忆 - 基于主题的记忆组织与上下文管理。

自动总结对话、按主题存储、注入上下文，让 Bot 拥有跨会话记忆能力。
采用 Agent 自主检索设计，LLM 通过 function-calling 工具按需查阅记忆片段。
"""

import asyncio
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .memory.coldstart import ColdStarter  # noqa: F401 - 仅用于类型提示
from .memory.context_injector import ContextInjector
from .memory.debug_logger import LLMDebugLogger
from .memory.dream import DreamManager
from .memory.experience import ExperienceManager
from .memory.fragment_merger import FragmentMerger
from .memory.store import MemoryStore
from .memory.summarizer import Summarizer
from .memory.topic_matcher import TopicMatcher
from .tools.memory_tools import create_memory_tools


@register(
    "astrbot_plugin_topic_context",
    "zhangtianyu",
    "织忆 - 基于主题的记忆组织与上下文管理",
    "1.0.0",
)
class TopicContextPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._plugin_config: dict = config or {}
        self.store: MemoryStore | None = None
        self.debug_logger: LLMDebugLogger | None = None
        self.summarizer: Summarizer | None = None
        self.merger: FragmentMerger | None = None
        self.experience_mgr: ExperienceManager | None = None
        self.dream_mgr: DreamManager | None = None
        self.topic_matcher: TopicMatcher | None = None
        self.context_injector: ContextInjector | None = None
        self.cold_starter: ColdStarter | None = None
        self.webui_server = None
        self.dream_task: asyncio.Task | None = None
        self._coldstart_running_umo: set[str] = set()

        # 用户消息缓存：在 on_llm_request 中存入，on_llm_response 中取出并消费。
        # key 为 event.span.span_id（每条消息事件唯一），value 为用户消息文本。
        # 解决 on_llm_response 触发时 event.message_str 可能为空或已变化的问题。
        self._pending_user_messages: dict[str, str] = {}

    async def initialize(self):
        """插件初始化。"""
        # 获取数据目录
        data_dir = StarTools.get_data_dir("astrbot_plugin_topic_context")

        # 初始化存储
        self.store = MemoryStore(data_dir)

        # 获取配置
        config = await self._get_config()

        # 调试日志记录器（仅通过修改此处代码手动启用，不暴露配置项）
        # 如需调试 LLM 调用细节，将下行改为: self.debug_logger = LLMDebugLogger(data_dir)
        self.debug_logger = None

        # 创建记忆总结专用调用器（支持独立 provider）
        summary_caller, self._summary_provider = self._create_provider_caller(
            config, "summary_provider_id", "记忆总结"
        )

        # 创建主题匹配专用调用器（支持独立 provider 以降低延迟）
        topic_match_caller, _ = self._create_provider_caller(
            config, "topic_match_provider_id", "主题匹配"
        )

        # 初始化各模块
        self.summarizer = Summarizer(summary_caller)
        self.merger = FragmentMerger(summary_caller, self.store)
        self.experience_mgr = ExperienceManager(summary_caller, self.store)
        self.dream_mgr = DreamManager(summary_caller, self.store)
        self.topic_matcher = TopicMatcher(topic_match_caller, self.store)
        self.context_injector = ContextInjector(self.store)
        self.cold_starter = ColdStarter(self.store)

        # 注册 function-calling 工具
        tools = create_memory_tools(self.store)
        for tool in tools:
            self.context.add_llm_tools(tool)

        # 启动 WebUI
        if config.get("webui_enabled", True):
            await self._start_webui(config, summary_caller)

        # 启动 Dream 定时任务
        if config.get("dream_enabled", True):
            dream_hour = config.get("dream_hour", 2)
            self.dream_task = asyncio.create_task(self._dream_scheduler(dream_hour))

        logger.info("[TopicContext] 插件初始化完成")

    async def terminate(self):
        """插件销毁。"""
        if self.dream_task and not self.dream_task.done():
            self.dream_task.cancel()
        if self.webui_server:
            await self.webui_server.stop()
        logger.info("[TopicContext] 插件已卸载")

    # ─── WebUI ───

    async def _start_webui(self, config: dict, llm_caller):
        """启动 WebUI 控制台。"""
        try:
            from .webui.server import WebUIServer

            self.webui_server = WebUIServer(self.store, config, llm_caller)
            await self.webui_server.start()
        except ImportError:
            logger.warning(
                "[TopicContext] WebUI 依赖缺失（需要 fastapi, uvicorn），跳过启动"
            )
        except Exception as e:
            logger.error(f"[TopicContext] WebUI 启动失败: {e}")

    # ─── LLM 调用器 ───

    def _create_provider_caller(self, config: dict, provider_key: str, label: str):
        """创建指定用途的 LLM 调用函数，支持从配置加载独立 provider。

        Args:
            config: 插件配置
            provider_key: provider_settings 中的 key（如 "summary_provider_id"）
            label: 日志标签（如 "记忆总结"）

        Returns:
            (caller, provider) 元组：caller 为调用函数，provider 为加载的 provider 实例。
        """
        provider_id = (
            config.get("provider_settings", {}).get(provider_key, "")
            if isinstance(config.get("provider_settings"), dict)
            else ""
        )
        provider = None

        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider:
                    logger.info(
                        f"[TopicContext] {label}使用独立 Provider: {provider_id}"
                    )
                else:
                    logger.warning(
                        f"[TopicContext] 未找到{label} Provider: {provider_id}"
                    )
            except Exception as e:
                logger.warning(
                    f"[TopicContext] 无法加载{label} Provider {provider_id}: {e}"
                )
        else:
            logger.warning(f"[TopicContext] {label}未配置 Provider ID")

        async def caller(system_prompt: str, prompt: str, caller_name: str = "") -> str:
            return await self._call_llm(
                system_prompt,
                prompt,
                provider=provider,
                caller_name=caller_name,
            )

        return caller, provider

    async def _call_llm(
        self,
        system_prompt: str,
        prompt: str,
        provider=None,
        caller_name: str = "",
        max_retries: int = 5,
        retry_interval: float = 3.0,
        timeout: float = 60.0,
    ) -> str:
        """调用 LLM，返回文本结果。失败时自动重试。

        Args:
            provider: 指定 provider 实例（必须从配置加载，不允许为 None）。
            caller_name: 调用来源标识，用于调试日志记录。
            max_retries: 最大重试次数（含首次请求）。
            retry_interval: 重试间隔（秒）。
            timeout: 单次请求超时时间（秒）。
        """
        model_name = (
            getattr(provider, "model_name", "") or getattr(provider, "model", "") or ""
        )

        if not provider:
            logger.warning(
                "[TopicContext] 无法获取 LLM Provider，请检查插件配置中的 Provider ID"
            )
            if self.debug_logger:
                self.debug_logger.log(
                    caller=caller_name or "_call_llm",
                    system_prompt=system_prompt,
                    prompt=prompt,
                    response="",
                    elapsed_ms=0,
                    success=False,
                    error="无法获取 LLM Provider，请检查插件配置中的 Provider ID",
                    model=model_name,
                )
            return ""

        import time

        last_error = ""
        for attempt in range(1, max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        system_prompt=system_prompt,
                    ),
                    timeout=timeout,
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000
                result = (
                    resp.completion_text
                    if hasattr(resp, "completion_text")
                    else str(resp.result_chain)
                )

                if self.debug_logger:
                    self.debug_logger.log(
                        caller=caller_name or "_call_llm",
                        system_prompt=system_prompt,
                        prompt=prompt,
                        response=result,
                        elapsed_ms=elapsed_ms,
                        success=True,
                        model=model_name,
                    )
                return result
            except asyncio.TimeoutError:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                last_error = "timeout"
                logger.warning(f"[TopicContext] LLM 调用超时 ({attempt}/{max_retries})")
                if self.debug_logger:
                    self.debug_logger.log(
                        caller=caller_name or "_call_llm",
                        system_prompt=system_prompt,
                        prompt=prompt,
                        response="",
                        elapsed_ms=elapsed_ms,
                        success=False,
                        error=f"timeout ({attempt}/{max_retries})",
                        model=model_name,
                    )
            except Exception as e:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                last_error = str(e)
                logger.warning(
                    f"[TopicContext] LLM 调用失败 ({attempt}/{max_retries}): {e}"
                )
                if self.debug_logger:
                    self.debug_logger.log(
                        caller=caller_name or "_call_llm",
                        system_prompt=system_prompt,
                        prompt=prompt,
                        response="",
                        elapsed_ms=elapsed_ms,
                        success=False,
                        error=f"{e} ({attempt}/{max_retries})",
                        model=model_name,
                    )

            if attempt < max_retries:
                await asyncio.sleep(retry_interval)

        logger.error(
            f"[TopicContext] LLM 调用在 {max_retries} 次尝试后仍然失败: {last_error}"
        )
        return ""

    async def _get_config(self) -> dict:
        """获取插件配置。"""
        return self._plugin_config

    # ─── 核心钩子：LLM 响应后 ───

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        """LLM 响应后：总结对话 → 合并判定 → 经验提取。

        使用 on_llm_response 而非 after_message_sent，
        因为后者依赖平台适配器转换后的 result.chain，
        可能取不到 Plain 组件导致 summarize 被静默跳过。

        参考 LivingMemory 插件：
        - 跳过工具调用中间轮（resp.tools_call_name 非空）
        - 跳过工具调用后的总结轮（resp.tools_call_extra_content 非空）
        - 从 on_llm_request 缓存中获取用户消息（event.message_str 在此钩子中不可靠）
        """
        logger.info("[TopicContext] >>> on_llm_response 钩子已触发 <<<")

        config = await self._get_config()
        if not config.get("enabled", True):
            logger.info("[TopicContext] 插件未启用 (enabled=False)，退出")
            return

        umo = event.unified_msg_origin
        logger.info(f"[TopicContext] umo={umo}")

        # 跳过工具调用中间轮次（LLM 发起 function call，等待工具返回结果）
        if hasattr(response, "tools_call_name") and response.tools_call_name:
            logger.info(
                f"[TopicContext] 检测到工具调用（tools={response.tools_call_name}），跳过总结"
            )
            return

        # 跳过工具调用后的总结轮（tools_call_extra_content 非空说明是 tool loop 产生的内容）
        if (
            hasattr(response, "tools_call_extra_content")
            and response.tools_call_extra_content
        ):
            logger.info("[TopicContext] 检测到 tool loop 总结响应，跳过总结")
            return

        # 从缓存获取用户消息（在 on_llm_request 中存入，以 span_id 为键）
        span_id = event.span.span_id
        user_message = self._pending_user_messages.pop(span_id, "")
        logger.info("[TopicContext] 缓存的用户消息: " + ("有" if user_message else "无"))
        if not user_message:
            logger.info("[TopicContext] 未找到缓存的用户消息，跳过总结")
            return

        # 跳过斜杠指令型消息（如 /new, /memory, /help 等），无需总结记忆
        # 注意：is_at_or_wake_command 在私聊场景下永远为 True，不能用于此判断
        if user_message.strip().startswith("/"):
            logger.info("[TopicContext] 是斜杠指令消息，跳过总结")
            return

        # 从 LLMResponse 提取助手回复文本
        assistant_response = ""
        if hasattr(response, "completion_text") and response.completion_text:
            assistant_response = response.completion_text
        elif hasattr(response, "result_chain") and response.result_chain and response.result_chain.chain:
            from astrbot.api.message_components import Plain

            parts = [
                p.text for p in response.result_chain.chain if isinstance(p, Plain)
            ]
            assistant_response = "\n".join(parts)

        logger.info("[TopicContext] 提取到的助手回复: " + ("有" if assistant_response else "无"))
        if not assistant_response:
            logger.info("[TopicContext] 助手回复为空，退出")
            return

        logger.info("[TopicContext] 即将进入 _process_round ...")
        try:
            await self._process_round(umo, user_message, assistant_response, config)
            logger.info("[TopicContext] _process_round 执行完成")
        except Exception as e:
            logger.error(f"[TopicContext] 处理轮次失败: {e}", exc_info=True)

    async def _process_round(
        self,
        umo: str,
        user_message: str,
        assistant_response: str,
        config: dict,
        timestamp: str | None = None,
    ) -> None:
        """处理一轮对话：总结（含主题匹配）→ 保存原文 → 记忆处理。

        每轮对话都会保存原始对话日志到 conversation_log.json（用于构建上下文），
        worth_remembering 仅控制是否进行 fragment 合并、core.md 更新等长期记忆操作。
        """
        # 1. 加载已有主题列表，供 summarizer 做精确匹配
        index = await self.store.load_topics_index(umo)
        existing_topics = index.get("topics", [])

        # 确定消息时间戳，优先使用传入的 timestamp，否则用当前时间
        ts = timestamp or datetime.now().isoformat()

        # 2. 总结（主题匹配 + 记忆判断一步完成），传入日期以使用绝对时间
        summary_result = await self.summarizer.summarize(
            user_message,
            assistant_response,
            existing_topics,
            message_date=ts,
            store=self.store,
            umo=umo,
        )

        # 3. 确定主题 —— LLM 返回的 topic_name 已经是精确匹配的名称
        topic_name = summary_result.topic_name
        if not topic_name:
            logger.debug("[TopicContext] 无法确定主题，跳过")
            return

        topic_id = MemoryStore.generate_topic_id(topic_name)

        # 查找是否为已有主题（LLM 已做匹配，这里只是按名称找 ID）
        existing_topic = None
        for t in existing_topics:
            if t["name"] == topic_name:
                existing_topic = t
                topic_id = t["id"]
                break

        if not existing_topic:
            # 创建新主题
            now = datetime.now().isoformat()
            await self.store.add_topic(
                umo,
                {
                    "id": topic_id,
                    "name": topic_name,
                    "created_at": now,
                    "updated_at": now,
                },
            )

        # 4. 保存原始对话到 conversation_log（始终执行，保证上下文连续性）
        await self.store.append_conversation_log(
            umo,
            topic_id,
            {
                "timestamp": ts,
                "user_message": user_message,
                "assistant_response": assistant_response,
            },
        )

        # 更新主题的 updated_at
        await self.store.update_topic(
            umo,
            topic_id,
            {"updated_at": ts},
        )

        # 5. 以下仅 worth_remembering 时执行（长期记忆处理）
        if not summary_result.worth_remembering:
            logger.debug("[TopicContext] 该轮不值得长期记忆，对话原文已记录")
            return

        logger.info(
            f"[TopicContext] 记忆轮次: topic={topic_name}, "
            f"summary={summary_result.summary[:50]}..."
        )

        # 6. 构建带摘要的轮次数据
        round_data = {
            "timestamp": ts,
            "user_message": user_message,
            "assistant_response": assistant_response,
            "summary": summary_result.summary,
        }

        # 7. 合并判定
        merge_result = await self.merger.judge(
            umo=umo,
            topic_id=topic_id,
            topic_name=topic_name,
            summary=summary_result.summary,
            keywords=summary_result.keywords,
            round_data=round_data,
        )

        # 记录实际写入的 fragment ID，用于 core.md 引用
        actual_fragment_id = ""
        is_merge = False
        core_summary = summary_result.summary

        if merge_result.should_merge:
            latest = await self.store.get_latest_fragment(umo, topic_id)
            if latest:
                await self.merger.merge_into(
                    umo=umo,
                    topic_id=topic_id,
                    fragment=latest,
                    round_data=round_data,
                    new_summary=merge_result.merged_summary,
                    new_keywords=merge_result.merged_keywords,
                    ts=ts,
                )
                actual_fragment_id = latest["id"]
                is_merge = True
                core_summary = merge_result.merged_summary
                logger.debug(f"[TopicContext] 合并到已有片段 {latest['id']}")
        else:
            fragment = await self.merger.create_new(
                umo=umo,
                topic_id=topic_id,
                topic_name=topic_name,
                summary=summary_result.summary,
                keywords=summary_result.keywords,
                round_data=round_data,
                ts=ts,
            )
            actual_fragment_id = fragment["id"]
            logger.debug(f"[TopicContext] 创建新片段 {fragment['id']}")

        # 8. 更新 core.md
        await self._update_core_md(
            umo,
            topic_id,
            topic_name,
            core_summary,
            round_data,
            ts=ts,
            fragment_id=actual_fragment_id,
            is_merge=is_merge,
            summary_result=summary_result,
        )

        # 9. 经验提取（如果检测到负反馈）
        if summary_result.is_negative_feedback and config.get(
            "experience_detect_enabled", True
        ):
            await self.experience_mgr.extract_experience(
                umo=umo,
                topic_id=topic_id,
                topic_name=topic_name,
                user_message=user_message,
                assistant_response=assistant_response,
                feedback_summary=summary_result.negative_feedback_summary,
            )

    async def _update_core_md(
        self,
        umo: str,
        topic_id: str,
        topic_name: str,
        new_summary: str,
        round_data: dict,
        ts: str = "",
        fragment_id: str = "",
        is_merge: bool = False,
        summary_result=None,
    ) -> None:
        """更新 core.md：首次创建时构建完整内容，后续更新概述/关键信息/最近记忆。"""
        core = await self.store.load_core_md(umo, topic_id)
        fragment_id = fragment_id or MemoryStore.generate_fragment_id(ts)
        date_str = (
            datetime.fromisoformat(ts).strftime("%Y-%m-%d")
            if ts
            else datetime.now().strftime("%Y-%m-%d")
        )

        overview = summary_result.overview if summary_result else ""
        key_info = summary_result.key_info if summary_result else ""

        if not core:
            # 新主题首次创建 core.md，使用 summarizer 输出的概述和关键信息
            new_entry = f"- [{date_str}] {new_summary} (ID: {fragment_id})\n"
            overview_text = overview or new_summary
            key_info_text = key_info or f"- {new_summary}"
            core = (
                f"# 主题: {topic_name}\n\n"
                f"## 概述\n{overview_text}\n\n"
                f"## 关键信息\n{key_info_text}\n\n"
                f"## 最近记忆\n{new_entry}"
            )
        else:
            # 后续更新
            if overview:
                # 替换概述部分
                lines = core.split("\n")
                new_lines = []
                in_overview = False
                for line in lines:
                    if line.strip() == "## 概述":
                        in_overview = True
                        new_lines.append(line)
                        new_lines.append(overview)
                        continue
                    if in_overview:
                        if line.startswith("## "):
                            in_overview = False
                            new_lines.append(line)
                        # 跳过原有概述内容
                        continue
                    new_lines.append(line)
                core = "\n".join(new_lines)

            if key_info:
                # 追加关键信息到 ## 关键信息 部分
                lines = core.split("\n")
                new_lines = []
                inserted = False
                for line in lines:
                    new_lines.append(line)
                    if not inserted and line.strip() == "## 关键信息":
                        new_lines.append(key_info)
                        inserted = True
                if inserted:
                    core = "\n".join(new_lines)
                else:
                    core += f"\n\n## 关键信息\n{key_info}"

            if is_merge:
                # 合并：找到已有条目，更新其摘要文本
                lines = core.split("\n")
                updated = False
                for i, line in enumerate(lines):
                    if f"(ID: {fragment_id})" in line:
                        lines[i] = f"- [{date_str}] {new_summary} (ID: {fragment_id})"
                        updated = True
                        break
                if not updated:
                    # 兜底：找不到已有条目时追加
                    lines.append(f"- [{date_str}] {new_summary} (ID: {fragment_id})")
                core = "\n".join(lines)
            else:
                # 新建：追加到"最近记忆"部分
                new_entry = f"- [{date_str}] {new_summary} (ID: {fragment_id})\n"
                if "## 最近记忆" in core:
                    core = core.rstrip() + "\n" + new_entry
                else:
                    core += f"\n\n## 最近记忆\n{new_entry}"

        await self.store.save_core_md(umo, topic_id, core)

    # ─── 核心钩子：LLM 请求前 ───

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求前：主题匹配 → 记忆注入（补充式，不替换原始上下文）。"""
        config = await self._get_config()
        if not config.get("enabled", True):
            return

        umo = event.unified_msg_origin
        user_message = event.message_str or ""
        if not user_message:
            return

        # 缓存用户消息，供 on_llm_response 使用（以 span_id 为键，避免并发覆盖）
        self._pending_user_messages[event.span.span_id] = user_message

        try:
            # 保存原始信息，用于 debug
            original_system_prompt = req.system_prompt

            # 从 AstrBot 原始 contexts 中提取上一轮对话，辅助主题匹配
            prev_round = self._extract_prev_round(req.contexts)

            # 1. 主题匹配（支持多主题）
            matched_topics = await self.topic_matcher.match(
                umo, user_message, prev_round
            )

            if matched_topics:
                topic_names = [t["name"] for t in matched_topics]
                logger.debug(f"[TopicContext] 主题匹配: {topic_names}")

                # 2. 在 system_prompt 后追加所有匹配主题的 core + experience
                #    不替换 req.contexts，保留主框架的短期记忆
                req.system_prompt = await self.context_injector.inject(
                    umo=umo,
                    matched_topics=matched_topics,
                    system_prompt=req.system_prompt,
                )

            # 记录结果到 debug
            if self.debug_logger:
                self.debug_logger.log(
                    caller="on_llm_request",
                    system_prompt=req.system_prompt,
                    prompt=user_message,
                    response="",
                    elapsed_ms=0,
                    success=True,
                    extra={
                        "matched_topics": [t["name"] for t in matched_topics]
                        if matched_topics
                        else [],
                        "original_system_prompt": original_system_prompt,
                        "context_modified": len(matched_topics) > 0,
                    },
                )
        except Exception as e:
            logger.error(f"[TopicContext] LLM 请求前处理失败: {e}")

    @staticmethod
    def _extract_prev_round(contexts: list[dict]) -> str:
        """从 contexts 中提取最后一轮 user+assistant 对话作为上下文。"""
        if not contexts or len(contexts) < 2:
            return ""
        # 找最后一对 user + assistant
        last_user = ""
        last_asst = ""
        for msg in reversed(contexts):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "assistant" and not last_asst:
                last_asst = content
            elif role == "user" and not last_user:
                last_user = content
            if last_user and last_asst:
                break
        if last_user and last_asst:
            return f"用户: {last_user}\n助手: {last_asst}"
        return ""

    # ─── 管理命令 ───

    @filter.command_group("memory")
    async def memory_group(self, event: AstrMessageEvent):
        """记忆管理命令组。用法: /memory <子命令>"""
        pass

    @memory_group.command("topics")
    async def memory_topics(self, event: AstrMessageEvent):
        """列出所有主题。"""
        umo = event.unified_msg_origin
        index = await self.store.load_topics_index(umo)
        topics = index.get("topics", [])

        if not topics:
            yield event.plain_result("暂无记忆主题。")
            return

        lines = ["📂 所有主题:"]
        for t in topics:
            overview = await self.store.get_topic_overview(umo, t["id"])
            overview_line = f"\n    概述: {overview}" if overview else ""
            lines.append(
                f"  - {t['name']} (ID: {t['id']})\n"
                f"{overview_line}\n"
                f"    创建: {t.get('created_at', '')[:10]}"
            )

        yield event.plain_result("\n".join(lines))

    @memory_group.command("show")
    async def memory_show(self, event: AstrMessageEvent, topic_name: str):
        """查看指定主题的记忆详情。用法: /memory show <主题名称>"""
        umo = event.unified_msg_origin
        index = await self.store.load_topics_index(umo)

        # 查找主题（按名称或 ID 模糊匹配）
        topic = None
        for t in index.get("topics", []):
            if topic_name in t["name"] or topic_name == t["id"]:
                topic = t
                break

        if not topic:
            yield event.plain_result(
                f"未找到主题 '{topic_name}'。使用 /memory topics 查看所有主题。"
            )
            return

        topic_id = topic["id"]
        overview = await self.store.get_topic_overview(umo, topic_id)
        lines = [
            f"📖 主题: {topic['name']}",
        ]
        if overview:
            lines.append(f"概述: {overview}")

        # core.md
        core = await self.store.load_core_md(umo, topic_id)
        if core:
            lines.append(f"\n📄 核心记忆:\n{core}")

        # experience.md
        exp = await self.store.load_experience_md(umo, topic_id)
        if exp:
            lines.append(f"\n💡 经验教训:\n{exp}")

        # 片段数量
        fragments = await self.store.load_all_fragments(umo, topic_id)
        lines.append(f"\n片段总数: {len(fragments)}")

        yield event.plain_result("\n".join(lines))

    @memory_group.command("coldstart")
    async def memory_coldstart(self, event: AstrMessageEvent, days: int = 7):
        """冷启动：扫描历史对话构建初始记忆。用法: /memory coldstart [天数]"""
        umo = event.unified_msg_origin

        if umo in self._coldstart_running_umo:
            yield event.plain_result("冷启动正在进行中，请等待完成。")
            return

        if days < 1 or days > 365:
            yield event.plain_result("天数范围: 1-365")
            return

        self._coldstart_running_umo.add(umo)

        try:
            yield event.plain_result(f"开始冷启动，扫描过去 {days} 天的对话...")

            async def progress(current, total, msg):
                pass  # 冷启动进度不实时推送，避免干扰

            config = await self._get_config()

            # 将 _process_round 包装为冷启动回调
            async def process_round(
                umo, user_message, assistant_response, timestamp=None
            ):
                await self._process_round(
                    umo, user_message, assistant_response, config, timestamp=timestamp
                )

            stats = await self.cold_starter.run(
                umo=umo,
                conversation_manager=self.context.conversation_manager,
                process_round_fn=process_round,
                days=days,
                progress_callback=progress,
            )

            # 统计结果
            index = await self.store.load_topics_index(umo)
            topics = index.get("topics", [])
            total_fragments = 0
            for t in topics:
                frags = await self.store.load_all_fragments(umo, t["id"])
                total_fragments += len(frags)

            lines = [
                "冷启动完成！",
                f"扫描会话: {stats['conversations_scanned']}",
                f"处理轮次: {stats['rounds_processed']}",
                f"创建主题: {len(topics)}",
                f"创建片段: {total_fragments}",
            ]
            if stats.get("errors"):
                lines.append(f"错误数: {len(stats['errors'])}")

            yield event.plain_result("\n".join(lines))

        except Exception as e:
            logger.error(f"[TopicContext] 冷启动失败: {e}")
            yield event.plain_result(f"冷启动失败: {e}")
        finally:
            self._coldstart_running_umo.discard(umo)

    # ─── Dream 定时整理 ───

    async def _dream_scheduler(self, hour: int):
        """每天在指定时间执行 Dream。"""
        while True:
            try:
                now = datetime.now()
                target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                sleep_seconds = (target - now).total_seconds()
                await asyncio.sleep(sleep_seconds)
                await self._run_dream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Dream] 调度器异常: {e}")
                await asyncio.sleep(3600)  # 出错后等 1 小时再试

    async def _run_dream(self):
        """执行 Dream：遍历所有用户的所有主题，整理记忆。"""
        logger.info("[Dream] 开始记忆整理...")
        await self._get_config()

        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_topic_context")
            if not data_dir.exists():
                return

            user_count = 0
            topic_count = 0

            for user_dir in data_dir.iterdir():
                if not user_dir.is_dir():
                    continue

                user_count += 1
                # 从目录名还原 umo（简单处理）
                umo = user_dir.name

                try:
                    index = await self.store.load_topics_index(umo)
                    topics = index.get("topics", [])

                    for topic in topics:
                        topic_id = topic["id"]
                        topic_name = topic["name"]
                        topic_count += 1

                        # 整理 core.md
                        await self.dream_mgr.organize_core(umo, topic_id, topic_name)

                        # 整理 experience.md
                        await self.dream_mgr.organize_experience(
                            umo, topic_id, topic_name
                        )

                        # 限速
                        await asyncio.sleep(2)

                except Exception as e:
                    logger.warning(f"[Dream] 处理用户 {user_dir.name} 失败: {e}")

            logger.info(
                f"[Dream] 记忆整理完成。处理 {user_count} 个用户, {topic_count} 个主题"
            )

        except Exception as e:
            logger.error(f"[Dream] 执行失败: {e}")
