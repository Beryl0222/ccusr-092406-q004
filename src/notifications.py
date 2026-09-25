"""通知发件箱与中继。

停用/冻结类消息先由领域派生为 ``NOTIFICATION_REQUESTED`` 落日志，
中继再把请求逐条下发。下发以请求内的 ``key`` 为幂等键：

* 日志侧：已有 ``NOTIFICATION_DELIVERED`` 的请求不再处理；
* 通道侧：sink 收到相同 key 必须返回首次下发的回执，不重复发送
  （模拟短信/企业消息平台的幂等接口）。

进程在“已下发但未记录”与“已记录”之间任何位置中断，重跑
:meth:`OutboxDispatcher.dispatch_pending` 都能补齐，且不会重复下发。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Protocol

from .events import EventStore


class NotificationSink(Protocol):
    def send(self, key: str, template: str, payload: dict) -> str:
        """下发一条消息，返回通道回执 id；同 key 重复调用必须幂等。"""


class RecordingSink:
    """测试/单机用内存通道：按 key 去重，模拟平台幂等接口。"""

    def __init__(self):
        self.sent: "OrderedDict[str, dict]" = OrderedDict()

    def send(self, key: str, template: str, payload: dict) -> str:
        if key in self.sent:
            return self.sent[key]["provider_id"]
        provider_id = f"msg-{len(self.sent) + 1:04d}"
        self.sent[key] = {"provider_id": provider_id, "template": template, "payload": payload}
        return provider_id


class FlakySink(RecordingSink):
    """测试用：在通道“已发送”后立即崩溃（不返回），模拟中断。

    ``crash_first=True`` 时只在第一次调用崩溃；``crash_keys`` 指定具体幂等键。
    """

    def __init__(self, crash_keys: set[str] | None = None, crash_first: bool = False):
        super().__init__()
        self._crash_keys = set(crash_keys or ())
        self._crash_first = crash_first

    def send(self, key: str, template: str, payload: dict) -> str:
        provider_id = super().send(key, template, payload)
        if self._crash_first or key in self._crash_keys:
            self._crash_first = False
            self._crash_keys.discard(key)
            raise RuntimeError("模拟进程在通道已发送后中断")
        return provider_id


class OutboxDispatcher:
    def __init__(self, store: EventStore, sink: NotificationSink, clock: Callable[[], str]):
        self.store = store
        self.sink = sink
        self.clock = clock

    def pending(self) -> list[dict]:
        events = self.store.load()
        delivered = {e["payload"]["key"] for e in events
                     if e["kind"] == "NOTIFICATION_DELIVERED"}
        requests: "OrderedDict[str, dict]" = OrderedDict()
        for event in events:
            if event["kind"] == "NOTIFICATION_REQUESTED":
                key = event["payload"]["key"]
                if key not in delivered:
                    requests.setdefault(key, event)
        return list(requests.values())

    def dispatch_pending(self) -> list[dict]:
        """下发所有未确认请求；每个 key 最多产生一条 DELIVERED 事件。"""
        results: list[dict] = []
        for request in self.pending():
            results.append(self.dispatch(request))
        return results

    def dispatch(self, request: dict) -> dict:
        key = request["payload"]["key"]
        provider_id = self.sink.send(
            key, request["payload"]["template"],
            {k: v for k, v in request["payload"].items() if k not in ("key", "template")})
        event = {
            "event_id": f"delivered-{key}",
            "kind": "NOTIFICATION_DELIVERED",
            "occurred_at": self.clock(),
            "subject_id": request["subject_id"],
            "payload": {"key": key, "provider_id": provider_id,
                        "template": request["payload"]["template"]},
        }
        # append 自身幂等：极端情况下重入也不会写第二份确认
        self.store.append(event)
        return event
