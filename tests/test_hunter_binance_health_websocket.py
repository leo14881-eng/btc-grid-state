"""Real WebSocket matching-PONG and close semantics on literal loopback only."""
from threading import Event,Thread
import unittest

from websockets.frames import OP_PONG
from websockets.sync.client import connect
from websockets.sync.server import serve


class WebSocketHealthTests(unittest.TestCase):
    def peer(self,drop_pong=False):
        ready=Event()
        def handler(connection):
            original=connection.protocol.send_frame
            def send(frame):
                if drop_pong and frame.opcode==OP_PONG:return
                original(frame)
            connection.protocol.send_frame=send
            ready.set()
            try:connection.recv(timeout=5)
            except Exception:pass
        server=serve(handler,'127.0.0.1',0,ping_interval=None,close_timeout=1)
        thread=Thread(target=server.serve_forever);thread.start()
        self.addCleanup(thread.join,3);self.addCleanup(server.shutdown)
        port=server.socket.getsockname()[1]
        client=connect(f'ws://127.0.0.1:{port}',proxy=None,ping_interval=None,close_timeout=1)
        self.addCleanup(client.close)
        self.assertTrue(ready.wait(2))
        return client

    def test_matching_pong_is_positive_connection_evidence(self):
        client=self.peer()
        result=client.ping(b'health-epoch-1',ack_on_close=False)
        self.assertTrue(result.wait(2))

    def test_close_without_matching_pong_never_completes_health_evidence(self):
        client=self.peer(drop_pong=True)
        result=client.ping(b'health-epoch-2',ack_on_close=False)
        self.assertFalse(result.wait(.05))
        client.close()
        self.assertFalse(result.is_set())


if __name__=='__main__':unittest.main()
