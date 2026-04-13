"""存储管理器：读写 topics_index.json、conversation_log.json、core.md、experience.md、fragments/。"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger


class MemoryStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        # 确保数据目录存在
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ─── 用户数据目录 ───

    def user_dir(self, umo: str) -> Path:
        """获取某用户的数据目录，按平台+用户ID隔离。"""
        # umo 格式示例: "aiocqhttp:group_123456:789" 或 "webchat:private:abc"
        # 用下划线替换冒号等特殊字符作为目录名
        safe_name = umo.replace(":", "_").replace("/", "_")
        d = self.data_dir / safe_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ─── topics_index.json ───

    async def load_topics_index(self, umo: str) -> dict:
        path = self.user_dir(umo) / "topics_index.json"
        if not path.exists():
            data = {"version": 1, "topics": []}
            await self._write_json(path, data)
            return data
        return await self._read_json(path)

    async def save_topics_index(self, umo: str, data: dict) -> None:
        path = self.user_dir(umo) / "topics_index.json"
        await self._write_json(path, data)

    async def get_topic_by_id(self, umo: str, topic_id: str) -> dict | None:
        index = await self.load_topics_index(umo)
        for t in index.get("topics", []):
            if t["id"] == topic_id:
                return t
        return None

    async def add_topic(self, umo: str, topic: dict) -> None:
        index = await self.load_topics_index(umo)
        index["topics"].append(topic)
        await self.save_topics_index(umo, index)

    async def update_topic(self, umo: str, topic_id: str, updates: dict) -> None:
        index = await self.load_topics_index(umo)
        for t in index["topics"]:
            if t["id"] == topic_id:
                t.update(updates)
                break
        await self.save_topics_index(umo, index)

    async def remove_topic(self, umo: str, topic_id: str) -> None:
        index = await self.load_topics_index(umo)
        index["topics"] = [t for t in index["topics"] if t["id"] != topic_id]
        await self.save_topics_index(umo, index)
        # 删除主题目录
        topic_dir = self.topic_dir(umo, topic_id)
        if topic_dir.exists():
            import shutil
            shutil.rmtree(topic_dir, ignore_errors=True)

    # ─── conversation_log.json（每主题原始对话日志） ───

    async def append_conversation_log(
        self, umo: str, topic_id: str, round_data: dict
    ) -> None:
        """向某主题的对话日志追加一轮原始对话。"""
        path = self.topic_dir(umo, topic_id) / "conversation_log.json"
        if not path.exists():
            log = {"rounds": []}
        else:
            log = await self._read_json(path)
        log["rounds"].append(round_data)
        await self._write_json(path, log)

    async def load_conversation_log(
        self, umo: str, topic_id: str
    ) -> list[dict]:
        """加载某主题的对话日志，按 timestamp 排序。"""
        path = self.topic_dir(umo, topic_id) / "conversation_log.json"
        if not path.exists():
            return []
        log = await self._read_json(path)
        rounds = log.get("rounds", [])
        rounds.sort(key=lambda r: r.get("timestamp", ""))
        return rounds

    # ─── 主题目录 ───

    def topic_dir(self, umo: str, topic_id: str) -> Path:
        d = self.user_dir(umo) / topic_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ─── core.md ───

    async def load_core_md(self, umo: str, topic_id: str) -> str:
        path = self.topic_dir(umo, topic_id) / "core.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    async def save_core_md(self, umo: str, topic_id: str, content: str) -> None:
        path = self.topic_dir(umo, topic_id) / "core.md"
        path.write_text(content, encoding="utf-8")

    async def get_topic_overview(self, umo: str, topic_id: str) -> str:
        """从 core.md 中提取 ## 概述 部分的文本。"""
        core = await self.load_core_md(umo, topic_id)
        if not core:
            return ""
        lines = core.split("\n")
        in_summary = False
        summary_text = ""
        for line in lines:
            if line.strip() == "## 概述":
                in_summary = True
                continue
            if in_summary:
                if line.startswith("## "):
                    break
                summary_text += line + "\n"
        return summary_text.strip()

    # ─── experience.md ───

    async def load_experience_md(self, umo: str, topic_id: str) -> str:
        path = self.topic_dir(umo, topic_id) / "experience.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    async def save_experience_md(self, umo: str, topic_id: str, content: str) -> None:
        path = self.topic_dir(umo, topic_id) / "experience.md"
        path.write_text(content, encoding="utf-8")

    # ─── fragments/ ───

    def fragments_dir(self, umo: str, topic_id: str) -> Path:
        d = self.topic_dir(umo, topic_id) / "fragments"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def fragment_path(self, umo: str, topic_id: str, fragment_id: str) -> Path:
        return self.fragments_dir(umo, topic_id) / f"{fragment_id}.json"

    async def load_fragment(self, umo: str, topic_id: str, fragment_id: str) -> dict | None:
        path = self.fragment_path(umo, topic_id, fragment_id)
        if not path.exists():
            return None
        return await self._read_json(path)

    async def save_fragment(self, umo: str, topic_id: str, fragment: dict) -> None:
        path = self.fragment_path(umo, topic_id, fragment["id"])
        await self._write_json(path, fragment)

    async def load_all_fragments(self, umo: str, topic_id: str) -> list[dict]:
        """加载某主题下所有片段，按 created_at 排序。"""
        fdir = self.fragments_dir(umo, topic_id)
        fragments = []
        if not fdir.exists():
            return fragments
        for f in fdir.glob("*.json"):
            try:
                data = await self._read_json(f)
                fragments.append(data)
            except Exception as e:
                logger.warning(f"加载片段失败 {f}: {e}")
        fragments.sort(key=lambda x: x.get("created_at", ""))
        return fragments

    async def get_latest_fragment(self, umo: str, topic_id: str) -> dict | None:
        """获取某主题下最新的片段。"""
        fragments = await self.load_all_fragments(umo, topic_id)
        return fragments[-1] if fragments else None

    async def delete_fragment(self, umo: str, topic_id: str, fragment_id: str) -> None:
        path = self.fragment_path(umo, topic_id, fragment_id)
        if path.exists():
            path.unlink()

    async def transfer_fragment(
        self, umo: str, source_topic_id: str, target_topic_id: str, fragment_id: str
    ) -> None:
        """将片段从源主题转移到目标主题，并同步更新双方的 core.md。"""
        # 1. 加载片段
        frag = await self.load_fragment(umo, source_topic_id, fragment_id)
        if not frag:
            raise FileNotFoundError(f"片段 {fragment_id} 不存在")

        # 2. 获取目标主题名
        target_topic = await self.get_topic_by_id(umo, target_topic_id)
        if not target_topic:
            raise FileNotFoundError(f"目标主题 {target_topic_id} 不存在")

        # 3. 更新片段的 topic 字段并保存到目标
        frag["topic"] = target_topic["name"]
        await self.save_fragment(umo, target_topic_id, frag)

        # 4. 从源主题删除片段
        await self.delete_fragment(umo, source_topic_id, fragment_id)

        # 5. 同步 core.md
        frag_summary = frag.get("summary", "")
        frag_date = frag.get("created_at", "")[:10] if frag.get("created_at") else ""

        # 从源 core.md 的「最近记忆」中移除该片段的条目
        source_core = await self.load_core_md(umo, source_topic_id)
        if source_core and fragment_id in source_core:
            # 移除包含该 fragment_id 的行
            lines = source_core.split("\n")
            new_lines = [l for l in lines if fragment_id not in l]
            await self.save_core_md(umo, source_topic_id, "\n".join(new_lines))

        # 向目标 core.md 的「最近记忆」追加该片段条目
        target_core = await self.load_core_md(umo, target_topic_id)
        new_entry = f"- [{frag_date}] {frag_summary} (ID: {fragment_id})\n"
        if target_core:
            # 找到 ## 最近记忆 部分，追加到末尾；如果没有该节则追加到文件末尾
            if "## 最近记忆" in target_core:
                target_core = target_core.rstrip("\n") + "\n" + new_entry
            else:
                target_core = target_core.rstrip("\n") + "\n\n## 最近记忆\n" + new_entry
        else:
            target_core = f"# 主题: {target_topic['name']}\n\n## 概述\n\n## 关键信息\n\n## 最近记忆\n" + new_entry
        await self.save_core_md(umo, target_topic_id, target_core)

    async def search_fragments_by_keyword(
        self, umo: str, topic_id: str, keyword: str
    ) -> list[dict]:
        """在主题下按关键词搜索片段，搜索范围：summary、keywords、rounds 中的消息。"""
        fragments = await self.load_all_fragments(umo, topic_id)
        keyword_lower = keyword.lower()
        results = []
        for frag in fragments:
            score = 0
            # 搜索摘要
            if keyword_lower in frag.get("summary", "").lower():
                score += 3
            # 搜索关键词
            for kw in frag.get("keywords", []):
                if keyword_lower in kw.lower():
                    score += 2
            # 搜索轮次内容
            for rnd in frag.get("rounds", []):
                if keyword_lower in rnd.get("user_message", "").lower():
                    score += 1
                if keyword_lower in rnd.get("assistant_response", "").lower():
                    score += 1
                if keyword_lower in rnd.get("summary", "").lower():
                    score += 2
            if score > 0:
                results.append((frag, score))
        # 按分数降序
        results.sort(key=lambda x: x[1], reverse=True)
        return [item[0] for item in results]

    # ─── 生成唯一片段 ID ───

    @staticmethod
    def generate_fragment_id(ts: str = "") -> str:
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                return f"{dt.strftime('%Y-%m-%d_%H%M%S')}"
            except (ValueError, TypeError):
                pass
        now = datetime.now()
        return f"{now.strftime('%Y-%m-%d_%H%M%S')}"

    # ─── 生成主题 ID（从名称派生） ───

    @staticmethod
    def generate_topic_id(topic_name: str) -> str:
        """将主题名转为安全的目录名。"""
        import re
        # 简单的拼音/英文保留，其他特殊字符移除或替换
        safe = re.sub(r'[^\w\u4e00-\u9fff]', '_', topic_name).strip('_')
        # 限制长度
        return safe[:64] if safe else f"topic_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # ─── 主题重命名（同步三处） ───

    async def rename_topic(self, umo: str, old_topic_id: str, new_name: str) -> None:
        """重命名主题：同步更新 index、文件夹名、片段中的 topic 字段。"""
        import shutil

        new_topic_id = self.generate_topic_id(new_name)
        udir = self.user_dir(umo)

        # 1. 更新 topics_index.json 中的 name 和 id
        index = await self.load_topics_index(umo)
        for t in index["topics"]:
            if t["id"] == old_topic_id:
                t["name"] = new_name
                t["id"] = new_topic_id
                break
        await self.save_topics_index(umo, index)

        # 2. 重命名文件夹（直接用路径，避免 topic_dir 自动 mkdir）
        old_dir = udir / old_topic_id
        new_dir = udir / new_topic_id
        if old_dir.exists() and old_dir != new_dir:
            if new_dir.exists():
                shutil.rmtree(new_dir, ignore_errors=True)
            shutil.move(str(old_dir), str(new_dir))

        # 3. 更新所有片段中的 topic 字段
        fragments = await self.load_all_fragments(umo, new_topic_id)
        for frag in fragments:
            frag["topic"] = new_name
            await self.save_fragment(umo, new_topic_id, frag)

    # ─── 创建空主题 ───

    async def create_empty_topic(self, umo: str, name: str) -> dict:
        """创建一个空主题，返回主题条目字典。"""
        from datetime import datetime
        topic_id = self.generate_topic_id(name)
        now = datetime.now().isoformat()

        # 创建主题目录和空文件
        tdir = self.topic_dir(umo, topic_id)
        (tdir / "core.md").write_text("", encoding="utf-8")
        (tdir / "experience.md").write_text("", encoding="utf-8")
        await self._write_json(tdir / "conversation_log.json", {"rounds": []})

        # 添加到 index
        topic_entry = {
            "id": topic_id,
            "name": name,
            "created_at": now,
            "updated_at": now,
        }
        await self.add_topic(umo, topic_entry)
        return topic_entry

    # ─── 工具方法 ───

    async def _read_json(self, path: Path) -> dict:
        content = path.read_text(encoding="utf-8")
        return json.loads(content)

    async def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
