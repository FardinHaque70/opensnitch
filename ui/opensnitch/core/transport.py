"""Toolkit-independent gRPC UI service. Domain callbacks use an injected dispatcher.

All sessions/queues are owned by this transport; application state stays on the UI
thread. A session generation prevents a stale stream from disconnecting its replacement.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import itertools
import json
import os
from pathlib import Path
from queue import Empty, Queue
import socket
import stat
from threading import Event, RLock, Thread
import uuid
from urllib.parse import unquote

import grpc
from opensnitch.proto import import_

pb, rpc = import_()


def node_id(peer):
    peer = unquote(peer)
    if peer.startswith('unix:'):
        return 'unix:/local'
    if peer.startswith(('ipv4:', 'ipv6:')):
        kind, address = peer.split(':', 1)
        return kind + ':' + address.rsplit(':', 1)[0].strip('[]')
    raise ValueError('Unsupported daemon peer')


@dataclass
class PromptRequest:
    node: str
    connection: bytes
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: Event = field(default_factory=Event)
    cancelled: Event = field(default_factory=Event)
    response: object = None

    def answer(self, rule):
        if self.done.is_set() or self.cancelled.is_set():
            return False
        self.response = rule
        self.done.set()
        return True


@dataclass
class Session:
    node: str
    configuration: bytes
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    outgoing: Queue = field(default_factory=Queue)
    closed: Event = field(default_factory=Event)
    streaming: bool = False


class UITransport(rpc.UIServicer):
    def __init__(self, address, dispatch, on_event, on_prompt, prompt_settings, workers=16):
        self.address = address
        self.dispatch = dispatch
        self.on_event = on_event
        self.on_prompt = on_prompt
        self.prompt_settings = prompt_settings
        self.sessions = {}
        self._pending = {}
        self._watching = {}
        self._prompts = {}
        self._ids = itertools.count(1)
        self._lock = RLock()
        self._stopping = False
        self.server = grpc.server(ThreadPoolExecutor(max_workers=workers), options=(
            ('grpc.max_send_message_length', 4 * 1024 * 1024),
            ('grpc.max_receive_message_length', 4 * 1024 * 1024),
            ('grpc.keepalive_time_ms', 5000), ('grpc.keepalive_timeout_ms', 20000)))
        rpc.add_UIServicer_to_server(self, self.server)

    def _emit(self, kind, node, data=None):
        self.dispatch(self.on_event, kind, node, data)

    def start(self):
        # Migration launcher intentionally supports a local Unix endpoint only.
        # Never silently expose an unauthenticated TCP endpoint.
        if not self.address.startswith('unix:///'):
            raise ValueError('GTK daemon session currently requires an absolute Unix socket')
        path = Path(self.address[7:])
        if os.path.lexists(path):
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise OSError('Refusing to replace a non-socket endpoint: ' + str(path))
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            else:
                raise OSError('Another frontend is already listening on ' + str(path))
            finally:
                probe.close()
        if not self.server.add_insecure_port(self.address):
            raise OSError('Could not bind GTK daemon socket')
        self.server.start()
        os.chmod(path, 0o640)

    def stop(self):
        self._stopping = True
        with self._lock:
            sessions = tuple(self.sessions.values())
        for session in sessions:
            self.disconnect(session.node, session.generation)
        self.server.stop(0).wait(timeout=3)

    def Subscribe(self, request, context):
        if self._stopping:
            context.abort(grpc.StatusCode.UNAVAILABLE, 'Frontend stopping')
        node = node_id(context.peer())
        result = pb.ClientConfig()
        result.CopyFrom(request)
        try:
            config = json.loads(result.config or '{}')
            # Matches the Qt frontend's session-only default action override.
            config['DefaultAction'] = 'allow' if self.prompt_settings().action == 'allow' else 'deny'
            result.config = json.dumps(config)
        except (ValueError, TypeError):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'Invalid daemon configuration')
        session = Session(node, request.SerializeToString())
        with self._lock:
            previous = self.sessions.get(node)
            if previous:
                self.disconnect(node, previous.generation)
            self.sessions[node] = session
        self._emit('subscribe', node, session.configuration)
        return result

    def Ping(self, request, context):
        self._emit('statistics', node_id(context.peer()), request.stats.SerializeToString())
        return pb.PingReply(id=request.id)

    def PostAlert(self, request, context):
        self._emit('alert', node_id(context.peer()), request.SerializeToString())
        return pb.MsgResponse(id=request.id)

    def AskRule(self, request, context):
        prompt = PromptRequest(node_id(context.peer()), request.SerializeToString())
        context.add_callback(lambda: self._cancel_prompt(prompt))
        with self._lock:
            self._prompts[prompt.id] = prompt
        try:
            self.dispatch(self.on_prompt, prompt)
            while not prompt.done.wait(.1):
                if self._stopping or not context.is_active():
                    self._cancel_prompt(prompt)
                    context.abort(grpc.StatusCode.CANCELLED, 'Prompt cancelled')
            if prompt.response is None:
                context.abort(grpc.StatusCode.CANCELLED, 'Prompt cancelled')
            self._emit('prompt-reply', prompt.node, prompt.response.SerializeToString())
            return prompt.response
        finally:
            with self._lock:
                self._prompts.pop(prompt.id, None)

    def has_prompts(self, node):
        with self._lock:
            return any(prompt.node == node for prompt in self._prompts.values())

    def _cancel_prompt(self, prompt):
        if not prompt.done.is_set():
            prompt.cancelled.set()
            prompt.done.set()
            self.dispatch(self.on_prompt, prompt)

    def _enqueue(self, node, notification, callback=None, stream=False):
        with self._lock:
            session = self.sessions.get(node)
            if session is None or session.closed.is_set() or not session.streaming:
                if callback:
                    self.dispatch(callback, False, 'Node notification stream disconnected')
                return None
            notification.id = next(self._ids)
            if callback:
                target = self._watching if stream else self._pending
                target[notification.id] = (session.generation, node, callback)
            session.outgoing.put(notification)
            return notification.id

    def send(self, node, notification, callback):
        return self._enqueue(node, notification, callback)

    def notify(self, node, notification):
        # TASK_STOP has no reply in daemon 1.7.2; do not leave a pending ack.
        return self._enqueue(node, notification)

    def watch(self, node, notification, callback):
        identifier = self._enqueue(node, notification, callback, stream=True)
        def cancel():
            with self._lock:
                self._watching.pop(identifier, None)
        return cancel

    def disconnect(self, node, generation=None):
        with self._lock:
            session = self.sessions.get(node)
            if session is None or (generation and session.generation != generation):
                return
            session.closed.set()
            session.outgoing.put(None)
            del self.sessions[node]
            for target in (self._pending, self._watching):
                pending = [key for key, value in target.items() if value[0] == session.generation]
                for key in pending:
                    _, _, callback = target.pop(key)
                    self.dispatch(callback, False, 'Node disconnected before acknowledgment')
        self._emit('disconnect', node)

    def Notifications(self, replies, context):
        node = node_id(context.peer())
        with self._lock:
            session = self.sessions.get(node)
        if session is None:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'Subscribe first')
        generation = session.generation
        context.add_callback(lambda: self.disconnect(node, generation))
        def receive():
            try:
                for reply in replies:
                    if session.closed.is_set():
                        break
                    if reply.id == 0:
                        session.streaming = True
                        self._emit('connected', node)
                        continue
                    with self._lock:
                        pending = self._pending.get(reply.id)
                        if pending and pending[:2] == (generation, node):
                            self._pending.pop(reply.id, None)
                        else:
                            pending = self._watching.get(reply.id)
                            if not pending or pending[:2] != (generation, node):
                                pending = None
                            elif reply.code != pb.OK:
                                self._watching.pop(reply.id, None)
                    if pending:
                        self.dispatch(pending[2], reply.code == pb.OK, reply.data)
                    else:
                        self._emit('task-reply', node, reply.SerializeToString())
            except grpc.RpcError:
                pass
            finally:
                self.disconnect(node, generation)
        Thread(target=receive, name='daemon-replies', daemon=True).start()
        while not session.closed.is_set() and context.is_active() and not self._stopping:
            try:
                notification = session.outgoing.get(timeout=.2)
            except Empty:
                continue
            if notification is not None:
                yield notification
        self.disconnect(node, generation)
