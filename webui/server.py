"""TopicContext WebUI - FastAPI 服务端。"""

import asyncio
import secrets
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from astrbot.api import logger

from ..memory.dream import DreamManager
from ..memory.store import MemoryStore


class WebUIServer:
    def __init__(self, store: MemoryStore, config: dict, llm_caller=None):
        self.store = store
        self.config = config
        self.llm_caller = llm_caller
        self.dream_mgr = DreamManager(llm_caller, store) if llm_caller else None
        self.host = "0.0.0.0"
        self.port = config.get("webui_port", 8900)
        self._password = config.get("webui_password", "")
        if not self._password:
            self._password = secrets.token_urlsafe(16)
            logger.info(f"[WebUI] 自动生成访问密码: {self._password}")

        self._tokens: dict[str, str] = {}  # token -> "user"
        self._app = FastAPI(title="TopicContext Memory", docs_url=None, redoc_url=None)
        self._server = None
        self._server_task = None

        self._setup_routes()

    def _setup_routes(self):
        static_dir = Path(__file__).resolve().parent / "static"
        index_path = static_dir / "index.html"

        if static_dir.exists():
            self._app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @self._app.get("/", response_class=HTMLResponse)
        async def serve_index():
            if not index_path.exists():
                raise HTTPException(404, "前端文件缺失")
            return HTMLResponse(index_path.read_text(encoding="utf-8"))

        # ─── 认证 ───

        @self._app.post("/api/login")
        async def login(body: dict):
            if body.get("password") != self._password:
                raise HTTPException(401, "密码错误")
            token = secrets.token_urlsafe(32)
            self._tokens[token] = "admin"
            return {"token": token}

        async def auth_dep(request: Request) -> str:
            auth = request.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if token not in self._tokens:
                raise HTTPException(401, "未认证")
            return token

        # ─── 用户列表 ───

        @self._app.get("/api/users")
        async def list_users(_token: str = Depends(auth_dep)):
            data_dir = self.store.data_dir
            users = []
            if data_dir.exists():
                for d in data_dir.iterdir():
                    if d.is_dir() and not d.name.startswith(".") and d.name != "debug":
                        index = await self.store.load_topics_index(d.name)
                        users.append(
                            {
                                "umo": d.name,
                                "topic_count": len(index.get("topics", [])),
                            }
                        )
            return users

        # ─── 主题管理 ───

        @self._app.post("/api/users/{umo}/topics")
        async def create_topic(umo: str, body: dict, _token: str = Depends(auth_dep)):
            name = body.get("name", "").strip()
            if not name:
                raise HTTPException(400, "名称不能为空")
            topic = await self.store.create_empty_topic(umo, name)
            return {"ok": True, "topic": topic}

        @self._app.get("/api/users/{umo}/topics")
        async def list_topics(umo: str, _token: str = Depends(auth_dep)):
            index = await self.store.load_topics_index(umo)
            topics = []
            for t in index.get("topics", []):
                fragments = await self.store.load_all_fragments(umo, t["id"])
                overview = await self.store.get_topic_overview(umo, t["id"])
                t = {**t, "fragment_count": len(fragments), "overview": overview}
                topics.append(t)
            return {"topics": topics}

        @self._app.get("/api/users/{umo}/topics/{topic_id}")
        async def get_topic(umo: str, topic_id: str, _token: str = Depends(auth_dep)):
            topic = await self.store.get_topic_by_id(umo, topic_id)
            if not topic:
                raise HTTPException(404, "主题不存在")
            core = await self.store.load_core_md(umo, topic_id)
            experience = await self.store.load_experience_md(umo, topic_id)
            fragments = await self.store.load_all_fragments(umo, topic_id)
            return {
                **topic,
                "core_md": core,
                "experience_md": experience,
                "fragments": fragments,
            }

        @self._app.put("/api/users/{umo}/topics/{topic_id}/core")
        async def update_core(
            umo: str, topic_id: str, body: dict, _token: str = Depends(auth_dep)
        ):
            content = body.get("content", "")
            await self.store.save_core_md(umo, topic_id, content)
            return {"ok": True}

        @self._app.put("/api/users/{umo}/topics/{topic_id}/experience")
        async def update_experience(
            umo: str, topic_id: str, body: dict, _token: str = Depends(auth_dep)
        ):
            content = body.get("content", "")
            await self.store.save_experience_md(umo, topic_id, content)
            return {"ok": True}

        @self._app.put("/api/users/{umo}/topics/{topic_id}/name")
        async def rename_topic(
            umo: str, topic_id: str, body: dict, _token: str = Depends(auth_dep)
        ):
            new_name = body.get("name", "")
            if not new_name:
                raise HTTPException(400, "名称不能为空")
            try:
                await self.store.rename_topic(umo, topic_id, new_name)
            except ValueError as e:
                raise HTTPException(409, str(e))
            return {"ok": True}

        @self._app.delete("/api/users/{umo}/topics/{topic_id}")
        async def delete_topic(
            umo: str, topic_id: str, _token: str = Depends(auth_dep)
        ):
            await self.store.remove_topic(umo, topic_id)
            return {"ok": True}

        # ─── 片段管理 ───

        @self._app.get("/api/users/{umo}/topics/{topic_id}/fragments/{fragment_id}")
        async def get_fragment(
            umo: str, topic_id: str, fragment_id: str, _token: str = Depends(auth_dep)
        ):
            frag = await self.store.load_fragment(umo, topic_id, fragment_id)
            if not frag:
                raise HTTPException(404, "片段不存在")
            return frag

        @self._app.delete("/api/users/{umo}/topics/{topic_id}/fragments/{fragment_id}")
        async def delete_fragment(
            umo: str, topic_id: str, fragment_id: str, _token: str = Depends(auth_dep)
        ):
            await self.store.delete_fragment(umo, topic_id, fragment_id)
            return {"ok": True}

        @self._app.post("/api/users/{umo}/transfer-fragment")
        async def transfer_fragment(
            umo: str, body: dict, _token: str = Depends(auth_dep)
        ):
            source_topic_id = body.get("source_topic_id", "")
            target_topic_id = body.get("target_topic_id", "")
            fragment_id = body.get("fragment_id", "")
            if not source_topic_id or not target_topic_id or not fragment_id:
                raise HTTPException(
                    400, "缺少 source_topic_id、target_topic_id 或 fragment_id"
                )
            if source_topic_id == target_topic_id:
                raise HTTPException(400, "源主题和目标主题不能相同")
            try:
                await self.store.transfer_fragment(
                    umo, source_topic_id, target_topic_id, fragment_id
                )
            except FileNotFoundError as e:
                raise HTTPException(404, str(e))
            return {"ok": True}

        # ─── 合并主题 ───

        @self._app.post("/api/users/{umo}/merge-topics")
        async def merge_topics(umo: str, body: dict, _token: str = Depends(auth_dep)):
            source_ids = body.get("source_ids", [])
            target_id = body.get("target_id", "")
            if not source_ids or not target_id:
                raise HTTPException(400, "缺少 source_ids 或 target_id")

            target_topic = await self.store.get_topic_by_id(umo, target_id)
            if not target_topic:
                raise HTTPException(404, "目标主题不存在")

            merged_fragments = 0
            for src_id in source_ids:
                if src_id == target_id:
                    continue
                src_topic = await self.store.get_topic_by_id(umo, src_id)
                if not src_topic:
                    continue
                # 迁移片段
                fragments = await self.store.load_all_fragments(umo, src_id)
                for frag in fragments:
                    frag["topic"] = target_topic["name"]
                    await self.store.save_fragment(umo, target_id, frag)
                    merged_fragments += 1
                # 删除源主题
                await self.store.remove_topic(umo, src_id)

            return {"ok": True, "merged_fragments": merged_fragments}

        # ─── Dream ───

        @self._app.post("/api/users/{umo}/dream")
        async def trigger_dream(umo: str, body: dict, _token: str = Depends(auth_dep)):
            if not self.dream_mgr:
                raise HTTPException(400, "LLM caller 未配置")

            instruction = body.get("instruction", "")
            topic_id = body.get("topic_id", "")
            results = []

            if topic_id:
                # 整理单个主题
                index = await self.store.load_topics_index(umo)
                topic = None
                for t in index.get("topics", []):
                    if t["id"] == topic_id:
                        topic = t
                        break
                if not topic:
                    raise HTTPException(404, "主题不存在")

                # 整理 core.md
                await self.dream_mgr.organize_core(
                    umo, topic["id"], topic["name"], instruction
                )
                # 整理 experience.md
                await self.dream_mgr.organize_experience(
                    umo, topic["id"], topic["name"]
                )
                results.append({"topic_id": topic_id, "status": "done"})
            else:
                # 整理所有主题
                index = await self.store.load_topics_index(umo)

                for topic in index.get("topics", []):
                    try:
                        await self.dream_mgr.organize_core(
                            umo, topic["id"], topic["name"], instruction
                        )
                        await self.dream_mgr.organize_experience(
                            umo, topic["id"], topic["name"]
                        )
                        results.append({"topic_id": topic["id"], "status": "done"})
                    except Exception as e:
                        results.append(
                            {"topic_id": topic["id"], "status": f"error: {e}"}
                        )
                    await asyncio.sleep(2)

            return {"results": results}

        # ─── 搜索 ───

        @self._app.get("/api/users/{umo}/search")
        async def search(umo: str, keyword: str, _token: str = Depends(auth_dep)):
            index = await self.store.load_topics_index(umo)
            results = []
            for topic in index.get("topics", []):
                frags = await self.store.search_fragments_by_keyword(
                    umo, topic["id"], keyword
                )
                for f in frags[:5]:
                    results.append(
                        {
                            "topic_id": topic["id"],
                            "topic_name": topic["name"],
                            "fragment_id": f["id"],
                            "summary": f.get("summary", ""),
                            "rounds_count": len(f.get("rounds", [])),
                            "created_at": f.get("created_at", ""),
                        }
                    )
            return {"results": results}

    # ─── 生命周期 ───

    async def start(self):
        if self._server_task and not self._server_task.done():
            return
        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        logger.info(f"[WebUI] 记忆管理控制台已启动: http://{self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        self._server = None
        self._server_task = None
        logger.info("[WebUI] 已停止")
