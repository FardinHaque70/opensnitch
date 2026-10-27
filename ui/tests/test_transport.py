"""Real gRPC exchanges with an isolated fake daemon; never the installed daemon."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
import tempfile
import time
import unittest

import grpc
from opensnitch.core.transport import UITransport, node_id, pb, rpc
from opensnitch.core.settings import PromptSettings


class FakeDaemon:
    def __init__(self, address):
        self.channel = grpc.insecure_channel(address)
        grpc.channel_ready_future(self.channel).result(timeout=3)
        self.stub = rpc.UIStub(self.channel)
        self.replies = Queue()
        self.stream = None

    def subscribe(self):
        return self.stub.Subscribe(pb.ClientConfig(name='Fixture node', config='{"DefaultAction":"allow"}'), timeout=3)

    def open_stream(self):
        if self.stream is not None:
            self.replies.put(None)
            self.stream.cancel()
        self.replies = Queue()
        queue = self.replies
        def replies():
            yield pb.NotificationReply(id=0, code=pb.OK)
            while True:
                item = queue.get()
                if item is None:
                    break
                yield item
        self.stream = self.stub.Notifications(replies())
        return self.stream

    def close(self):
        self.replies.put(None)
        if self.stream:
            self.stream.cancel()
        self.channel.close()


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for fixture event')
        time.sleep(.01)


class TestTransport(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='opensnitch-grpc-test-')
        self.address = 'unix://' + self.directory.name + '/ui.sock'
        self.events = []
        self.prompts = Queue()
        self.service = UITransport(self.address, lambda fn, *args: fn(*args),
            lambda *args: self.events.append(args), self.prompts.put, lambda: PromptSettings())
        self.service.start()
        self.daemon = FakeDaemon(self.address)

    def tearDown(self):
        self.daemon.close()
        self.service.stop()
        self.directory.cleanup()

    def connect(self):
        self.daemon.subscribe()
        stream = self.daemon.open_stream()
        self.reader = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.reader.shutdown, wait=False, cancel_futures=True)
        # gRPC begins streaming lazily on iteration.
        self.incoming = self.reader.submit(next, stream)
        wait_for(lambda: any(event[0] == 'connected' for event in self.events))

    def test_ping_alert_subscribe_and_default_override(self):
        reply = self.daemon.subscribe()
        self.assertIn('"DefaultAction": "deny"', reply.config)
        self.assertEqual(self.daemon.stub.Ping(pb.PingRequest(id=42), timeout=2).id, 42)
        self.assertEqual(self.daemon.stub.PostAlert(pb.Alert(id=9, text='Fixture alert'), timeout=2).id, 9)
        self.assertEqual([event[0] for event in self.events], ['subscribe', 'statistics', 'alert'])

    def test_notification_acknowledgment_is_correlated(self):
        self.connect()
        replies = []
        self.service.send('unix:/local', pb.Notification(type=pb.CHANGE_RULE), lambda *args: replies.append(args))
        message = self.incoming.result(timeout=2)
        self.daemon.replies.put(pb.NotificationReply(id=message.id + 100, code=pb.OK))
        time.sleep(.05)
        self.assertEqual(replies, [])
        self.daemon.replies.put(pb.NotificationReply(id=message.id, code=pb.ERROR, data='Fixture rejected'))
        wait_for(lambda: bool(replies))
        self.assertEqual(replies, [(False, 'Fixture rejected')])

    def test_streaming_task_replies_are_distinct_from_rule_acknowledgments(self):
        self.connect()
        replies = []
        cancel = self.service.watch('unix:/local', pb.Notification(type=pb.TASK_START), lambda *args: replies.append(args))
        message = self.incoming.result(timeout=2)
        self.daemon.replies.put(pb.NotificationReply(id=message.id, code=pb.OK, data='one'))
        self.daemon.replies.put(pb.NotificationReply(id=message.id, code=pb.OK, data='two'))
        wait_for(lambda: len(replies) == 2)
        self.assertEqual(replies, [(True, 'one'), (True, 'two')])
        cancel()
        self.daemon.replies.put(pb.NotificationReply(id=message.id, code=pb.OK, data='late'))
        time.sleep(.05)
        self.assertEqual(len(replies), 2)
        self.assertFalse(self.service._pending)

    def test_disconnect_fails_pending_and_reconnects(self):
        self.connect()
        replies = []
        self.service.send('unix:/local', pb.Notification(type=pb.DELETE_RULE), lambda *args: replies.append(args))
        self.incoming.result(timeout=2)
        self.daemon.stream.cancel()
        wait_for(lambda: bool(replies))
        self.assertFalse(replies[0][0])
        self.daemon.subscribe()
        self.assertIn('unix:/local', self.service.sessions)

    def test_prompt_response_and_cancellation(self):
        response = self.daemon.stub.AskRule.future(pb.Connection(process_path='/fixture/app'), timeout=2)
        prompt = self.prompts.get(timeout=2)
        prompt.answer(pb.Rule(name='fixture', action='deny', duration='once'))
        self.assertEqual(response.result(timeout=2).action, 'deny')
        cancelled = self.daemon.stub.AskRule.future(pb.Connection(process_path='/fixture/cancelled'), timeout=2)
        prompt = self.prompts.get(timeout=2)
        cancelled.cancel()
        wait_for(prompt.cancelled.is_set)
        self.assertFalse(prompt.answer(pb.Rule(action='allow')))

    def test_concurrent_prompts_remain_independent(self):
        one = self.daemon.stub.AskRule.future(pb.Connection(process_path='/fixture/one'), timeout=2)
        two = self.daemon.stub.AskRule.future(pb.Connection(process_path='/fixture/two'), timeout=2)
        requests = [self.prompts.get(timeout=2), self.prompts.get(timeout=2)]
        for request in requests:
            path = pb.Connection.FromString(request.connection).process_path
            request.answer(pb.Rule(name=path, action='allow' if path.endswith('one') else 'deny'))
        self.assertEqual(one.result(timeout=2).action, 'allow')
        self.assertEqual(two.result(timeout=2).action, 'deny')

    def test_refuses_an_existing_listener(self):
        other = UITransport(self.address, lambda fn, *args: fn(*args), lambda *args: None,
                            lambda *args: None, lambda: PromptSettings())
        with self.assertRaises(OSError):
            other.start()
        other.stop()
        self.assertEqual(self.daemon.stub.Ping(pb.PingRequest(id=7), timeout=2).id, 7)

    def test_refuses_plain_tcp_migration_endpoint(self):
        other = UITransport('127.0.0.1:0', lambda *args: None, lambda *args: None,
                            lambda *args: None, lambda: PromptSettings())
        with self.assertRaises(ValueError):
            other.start()
        other.stop()

    def test_stale_disconnect_cannot_remove_replacement_session(self):
        self.daemon.subscribe()
        old = self.service.sessions['unix:/local'].generation
        self.daemon.subscribe()
        self.service.disconnect('unix:/local', old)
        self.assertIn('unix:/local', self.service.sessions)

    def test_peer_identity_preserves_ipv6_and_node_boundaries(self):
        self.assertEqual(node_id('unix:'), 'unix:/local')
        self.assertEqual(node_id('ipv6:%5B2001:db8::1%5D:1234'), 'ipv6:2001:db8::1')
        self.assertEqual(node_id('ipv4:192.0.2.1:1234'), 'ipv4:192.0.2.1')

    def test_existing_non_socket_file_is_preserved(self):
        path = Path(self.directory.name) / 'important-file'
        path.write_text('fixture data')
        other = UITransport('unix://' + str(path), lambda *args: None, lambda *args: None,
                            lambda *args: None, lambda: PromptSettings())
        with self.assertRaises(OSError):
            other.start()
        other.stop()
        self.assertEqual(path.read_text(), 'fixture data')

    def test_invalid_subscription_does_not_replace_active_node(self):
        self.daemon.subscribe()
        generation = self.service.sessions['unix:/local'].generation
        with self.assertRaises(grpc.RpcError):
            self.daemon.stub.Subscribe(pb.ClientConfig(config='not json'), timeout=2)
        self.assertEqual(self.service.sessions['unix:/local'].generation, generation)
