"""API routes — SSE 实时任务状态推送

v3.5.0: 新增 SSE 端点，替代前端轮询。
任务状态变更时通过 sse_broadcast 推送到所有连接的客户端。
前端用原生 EventSource API 订阅，自动重连，回退到轮询。
"""
import asyncio
import json
import logging
import threading
from fastapi import APIRouter
from starlette.responses import StreamingResponse as _StreamingResponse

_log = logging.getLogger('std_scraper')

router = APIRouter(prefix="/api", tags=["SSE"])

_sse_queues: list = []
_sse_cancelling = False
_sse_lock = threading.Lock()


class StreamingResponse(_StreamingResponse):
    """StreamingResponse 子类，抑制关闭时 listen_for_disconnect 的 CancelledError。"""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            pass


def sse_broadcast(event_type: str, data: dict):
    """广播事件到所有连接的 SSE 客户端。

    由 TaskManager.update() 调用（try/except 保护），
    广播失败不影响主流程。
    """
    with _sse_lock:
        queues = list(_sse_queues)
    dead_queues = []
    for q in queues:
        try:
            q.put_nowait((event_type, data))
        except asyncio.QueueFull:
            dead_queues.append(q)
    if dead_queues:
        with _sse_lock:
            for q in dead_queues:
                if q in _sse_queues:
                    _sse_queues.remove(q)


def sse_close_all():
    """关闭所有 SSE 连接，用于服务关闭时快速释放。"""
    global _sse_cancelling
    _sse_cancelling = True
    with _sse_lock:
        for q in list(_sse_queues):
            try:
                q.put_nowait(("_close", {}))
            except asyncio.QueueFull:
                pass
        _sse_queues.clear()


@router.get("/tasks/stream")
async def task_stream():
    """SSE 端点：实时推送任务状态变更。

    事件格式：
    - init: 初始发送当前所有任务状态
    - task_update: 单个任务状态变更
    - keepalive: 每 30 秒发送心跳
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    with _sse_lock:
        _sse_queues.append(queue)

    async def event_generator():
        try:
            if _sse_cancelling:
                return
            from .state import task_manager
            tasks = task_manager.get_all()
            # SSE init 不推送完整 std_items（可能数百条），仅保留长度信息
            for t in tasks:
                if 'std_items' in t and isinstance(t['std_items'], list):
                    t['std_items_count'] = len(t['std_items'])
                    t['std_items'] = None
            init_data = json.dumps(tasks, ensure_ascii=False)
            yield f"event: init\ndata: {init_data}\n\n"

            while True:
                try:
                    event_type, data = await asyncio.wait_for(queue.get(), timeout=30)
                    if event_type == "_close":
                        break
                    payload = json.dumps(data, ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _sse_lock:
                if queue in _sse_queues:
                    _sse_queues.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
