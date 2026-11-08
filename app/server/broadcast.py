"""WebSocket 접속자 관리와 상태 방송.

접속자마다 크기 1짜리 큐를 둔다. 방송은 큐에 넣기만 하고(기다리지 않음), 실제 전송은 접속자별
전송 태스크가 한다. 그래서
- 느린 접속자 한 명 때문에 시뮬레이션 루프나 다른 접속자가 기다리지 않는다.
- 느린 접속자에게 쌓일 상태는 오래된 것부터 버리고 최신 상태 하나만 남긴다. 화면에는 최신
  상태만 의미가 있으므로 모든 메시지를 보낼 필요가 없다(메모리도 접속자당 메시지 1개로 고정).
"""

import asyncio
import contextlib

from fastapi import WebSocket


class Client:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self.dropped = 0  # 보내기 전에 새 상태로 바뀌어 버린 메시지 수

    def offer(self, message: str) -> None:
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(message)

    async def send_forever(self) -> None:
        while True:
            message = await self.queue.get()
            await self.websocket.send_text(message)


class Broadcaster:
    def __init__(self, max_connections: int) -> None:
        self.max_connections = max_connections
        self._clients: set[Client] = set()

    @property
    def count(self) -> int:
        return len(self._clients)

    def add(self, websocket: WebSocket) -> Client | None:
        """자리가 있으면 접속자로 등록한다. 상한에 닿았으면 None."""
        if len(self._clients) >= self.max_connections:
            return None
        client = Client(websocket)
        self._clients.add(client)
        return client

    def remove(self, client: Client) -> None:
        self._clients.discard(client)

    def publish(self, message: str) -> None:
        """모든 접속자에게 같은 메시지를 보낸다. JSON 직렬화는 호출한 쪽에서 한 번만 한다."""
        for client in self._clients:
            client.offer(message)


async def serve_client(client: Client) -> None:
    """접속이 끊길 때까지 전송 태스크를 돌리고, 클라이언트가 보내는 메시지는 버린다.

    receive를 계속 기다리는 이유: 끊김(WebSocketDisconnect)은 받는 쪽에서 알 수 있다.
    """
    sender = asyncio.create_task(client.send_forever())
    try:
        while True:
            message = await client.websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender
