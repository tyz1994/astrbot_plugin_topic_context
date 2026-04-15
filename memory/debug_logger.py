"""LLM 调用调试日志记录器：将每次 LLM 请求的输入、输出、耗时缓存到本地 debug 文件夹。"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class LLMDebugLogger:
    """记录每次 LLM 调用的完整信息到 debug 目录。"""

    MAX_DEBUG_FILES = 5

    def __init__(self, data_dir: Path):
        self.debug_dir = data_dir / "debug"
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        *,
        caller: str,
        system_prompt: str,
        prompt: str,
        response: str,
        elapsed_ms: float,
        success: bool,
        error: str = "",
        model: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """记录一次 LLM 调用，返回写入的文件路径。

        Args:
            caller: 调用来源标识（如 "Summarizer.summarize"、"TopicMatcher.match"）
            system_prompt: 系统提示词
            prompt: 用户提示词
            response: LLM 返回的文本
            elapsed_ms: 耗时（毫秒）
            success: 是否成功
            error: 错误信息（失败时）
            model: 请求使用的模型名称
            extra: 额外附带的调试信息
        """
        now = datetime.now()
        ts = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond // 1000:03d}"
        safe_caller = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", caller)
        filename = f"{ts}_{safe_caller}.json"
        filepath = self.debug_dir / filename

        record = {
            "timestamp": now.isoformat(),
            "caller": caller,
            "success": success,
            "elapsed_ms": round(elapsed_ms, 2),
            "model": model,
            "request": {
                "system_prompt": system_prompt,
                "prompt": prompt,
            },
            "response": response,
        }
        if error:
            record["error"] = error
        if extra:
            record["extra"] = extra

        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 清理旧文件，仅保留最近 MAX_DEBUG_FILES 条
        existing = sorted(
            self.debug_dir.glob("*.json"), key=lambda f: f.stat().st_mtime
        )
        for old in existing[: -self.MAX_DEBUG_FILES]:
            old.unlink()

        return filepath
