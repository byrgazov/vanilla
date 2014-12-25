import collections
import urlparse
import logging
import hashlib
import base64
import urllib
import struct
import uuid
import ssl
import os

import vanilla.exception
import vanilla.meta


HTTP_VERSION = 'HTTP/1.1'


log = logging.getLogger(__name__)


class HTTP(object):
    def __init__(self, hub):
        self.hub = hub

    def connect(self, url):
        return HTTPClient(self.hub, url)

    # TODO: hacking in convenience for example, still need to add test
    # TODO: ensure connection is closed after the get is done
    def get(self, uri, params=None, headers=None):
        parsed = urlparse.urlsplit(uri)
        conn = self.connect('%s://%s' % (parsed.scheme, parsed.netloc))
        return conn.get(parsed.path, params=params, headers=headers)

    def listen(
            self,
            port=0,
            host='127.0.0.1',
            serve=None,
            request_timeout=20000):

        def launch(serve):
            @self.hub.tcp.listen(host=host, port=port)
            def server(socket):
                HTTPServer(self.hub, socket, request_timeout, serve)
            return server

        if serve:
            return launch(serve)
        return launch


class Headers(object):
    Value = collections.namedtuple('Value', ['key', 'value'])

    def __init__(self):
        self.store = {}

    def __setitem__(self, key, value):
        self.store[key.lower()] = self.Value(key, value)

    def __contains__(self, key):
        return key.lower() in self.store

    def __getitem__(self, key):
        return self.store[key.lower()].value

    def __repr__(self):
        return repr(dict(self.store.itervalues()))

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class HTTPSocket(object):

    def read_headers(self):
        headers = Headers()
        while True:
            line = self.socket.read_line()
            if not line:
                break
            k, v = line.split(': ', 1)
            headers[k] = v.strip()
        return headers

    def write_headers(self, headers):
        headers = '\r\n'.join(
            '%s: %s' % (k, v) for k, v in headers.iteritems())
        self.socket.write(headers+'\r\n'+'\r\n')

    def read_chunk(self):
        length = int(self.socket.read_line(), 16)
        if length:
            chunk = self.socket.read_bytes(length)
        else:
            chunk = ''
        assert self.socket.read_bytes(2) == '\r\n'
        return chunk

    def write_chunk(self, chunk):
        self.socket.write('%s\r\n%s\r\n' % (hex(len(chunk))[2:], chunk))


class HTTPClient(HTTPSocket):

    Status = collections.namedtuple('Status', ['version', 'code', 'message'])

    class Response(object):
        def __init__(self, status, headers, body):
            self.status = status
            self.headers = headers
            self.body = body

        def consume(self):
            return ''.join(self.body)

        def __repr__(self):
            return 'HTTPClient.Response(status=%r)' % (self.status,)

    def __init__(self, hub, url):
        self.hub = hub

        parsed = urlparse.urlsplit(url)
        assert parsed.query == ''
        assert parsed.fragment == ''

        default_port = 443 if parsed.scheme == 'https' else 80
        host, port = urllib.splitnport(parsed.netloc, default_port)

        self.socket = self.hub.tcp.connect(host=host, port=port)

        # TODO: this shouldn't block on the SSL handshake
        if parsed.scheme == 'https':
            self.socket.d.conn = ssl.wrap_socket(self.socket.d.conn)
            self.socket.d.conn.setblocking(0)

        self.socket.line_break = '\r\n'

        self.agent = 'vanilla/%s' % vanilla.meta.__version__

        self.default_headers = dict([
            ('Accept', '*/*'),
            ('User-Agent', self.agent),
            ('Host', parsed.netloc), ])

        # TODO: fix API
        self.requests = self.hub.router().pipe(self.hub.queue(10))
        self.requests.pipe(self.hub.consumer(self.writer))

        self.responses = self.hub.router().pipe(self.hub.queue(10))
        self.responses.pipe(self.hub.consumer(self.reader))

    def reader(self, response):
        try:
            version, code, message = self.socket.read_line().split(' ', 2)
        except vanilla.exception.Halt:
            # TODO: could we offer the ability to auto-reconnect?
            try:
                response.send(vanilla.exception.ConnectionLost())
            except vanilla.exception.Abandoned:
                # TODO: super need to think this through
                pass
            return

        code = int(code)
        status = self.Status(version, code, message)
        # TODO:
        # if status.code == 408:

        headers = self.read_headers()
        sender, recver = self.hub.pipe()

        response.send(self.Response(status, headers, recver))

        if headers.get('Connection') == 'Upgrade':
            sender.close()
            return

        try:
            if headers.get('transfer-encoding') == 'chunked':
                while True:
                    chunk = self.read_chunk()
                    if not chunk:
                        break
                    sender.send(chunk)
            else:
                # TODO:
                # http://www.w3.org/Protocols/rfc2616/rfc2616-sec4.html#sec4.4
                body = self.socket.read_bytes(int(headers['content-length']))
                sender.send(body)
        except vanilla.exception.Halt:
            # TODO: could we offer the ability to auto-reconnect?
            sender.send(vanilla.exception.ConnectionLost())

        sender.close()

    def request(
            self,
            method,
            path='/',
            params=None,
            headers=None,
            data=None):

        self.requests.send((method, path, params, headers, data))
        sender, recver = self.hub.pipe()
        self.responses.send(sender)
        return recver

    def writer(self, request):
        method, path, params, headers, data = request

        request_headers = {}
        request_headers.update(self.default_headers)
        if headers:
            request_headers.update(headers)

        if params:
            path += '?' + urllib.urlencode(params)

        request = '%s %s %s\r\n' % (method, path, HTTP_VERSION)
        self.socket.write(request)

        # TODO: handle chunked transfers
        if data is not None:
            request_headers['Content-Length'] = len(data)
        self.write_headers(request_headers)

        # TODO: handle chunked transfers
        if data is not None:
            self.socket.write(data)

    def get(self, path='/', params=None, headers=None, auth=None):
        if auth:
            if not headers:
                headers = {}
            headers['Authorization'] = \
                'Basic ' + base64.b64encode('%s:%s' % auth)
        return self.request('GET', path, params, headers, None)

    def post(self, path='/', params=None, headers=None, data=''):
        return self.request('POST', path, params, headers, data)

    def put(self, path='/', params=None, headers=None, data=''):
        return self.request('PUT', path, params, headers, data)

    def delete(self, path='/', params=None, headers=None):
        return self.request('DELETE', path, params, headers, None)

    def websocket(self, path='/', params=None, headers=None):
        key = base64.b64encode(uuid.uuid4().bytes)

        headers = headers or {}
        headers.update({
            'Upgrade': 'WebSocket',
            'Connection': 'Upgrade',
            'Sec-WebSocket-Key': key,
            'Sec-WebSocket-Version': 13, })

        response = self.request('GET', path, params, headers, None).recv()
        assert response.status.code == 101
        assert response.headers['Upgrade'].lower() == 'websocket'
        assert response.headers['Sec-WebSocket-Accept'] == \
            WebSocket.accept_key(key)

        return WebSocket(self.hub, self.socket)


class HTTPServer(HTTPSocket):
    Request = collections.namedtuple(
        'Request', ['method', 'path', 'version', 'headers'])

    class Request(Request):
        def consume(self):
            return self.body

    class Response(object):
        """
        manages the state of a HTTP Server Response
        """
        class HTTPStatus(Exception):
            pass

        class HTTP404(HTTPStatus):
            code = 404
            message = 'Not Found'

        def __init__(self, server, request, sender):
            self.server = server
            self.request = request
            self.sender = sender

            self.status = (200, 'OK')
            self.headers = {}

            self.is_started = False
            self.is_upgraded = False

        def start(self):
            assert not self.is_started
            self.is_started = True
            self.sender.send(self.status)
            self.sender.send(self.headers)

        def send(self, data):
            if not self.is_started:
                self.headers['Transfer-Encoding'] = 'chunked'
                self.start()
            self.sender.send(data)

        def end(self, data):
            if not self.is_started:
                self.headers['Content-Length'] = len(data)
                self.start()
                self.sender.send(data or '')
            else:
                if data:
                    self.sender.send(data)
            self.sender.close()

        def upgrade(self):
            # TODO: the connection header can be a list of tokens, this should
            # be handled more comprehensively
            connection_tokens = [
                x.strip().lower()
                for x in self.request.headers['Connection'].split(',')]
            assert 'upgrade' in connection_tokens

            assert self.request.headers['Upgrade'].lower() == 'websocket'

            key = self.request.headers['Sec-WebSocket-Key']
            accept = WebSocket.accept_key(key)

            self.status = (101, 'Switching Protocols')
            self.headers.update({
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Accept": accept, })

            self.start()
            self.sender.close()
            ws = WebSocket(
                self.server.hub, self.server.socket, is_client=False)
            self.is_upgraded = ws
            return ws

    def __init__(self, hub, socket, request_timeout, serve):
        self.hub = hub

        self.socket = socket
        self.socket.timeout = request_timeout
        self.socket.line_break = '\r\n'

        self.serve = serve

        self.responses = self.hub.consumer(self.writer)

        # TODO: handle Connection: close
        # TODO: spawn a green thread this request
        # TODO: handle when this is a websocket upgrade request

        while True:
            try:
                request = self.read_request()
            except vanilla.exception.Halt:
                return

            except vanilla.exception.Timeout:
                print "Request Timeout"
                self.write_response(408, 'Request Timeout')
                self.socket.close()
                return

            sender, recver = self.hub.pipe()
            response = self.Response(self, request, sender)
            self.responses.send(recver)

            try:
                data = serve(request, response)
            except response.HTTPStatus, e:
                response.status = (e.code, e.message)
                data = e.message
            except Exception, e:
                # TODO: send 500
                raise

            if response.is_upgraded:
                response.is_upgraded.close()
                return

            response.end(data)

    def writer(self, response):
        try:
            code, message = response.recv()
            self.write_response(code, message)

            headers = response.recv()
            self.write_headers(headers)

            if headers.get('Connection') == 'Upgrade':
                return

            if headers.get('Transfer-Encoding') == 'chunked':
                for chunk in response:
                    self.write_chunk(chunk)
                self.write_chunk('')
            else:
                self.socket.write(response.recv())
        except vanilla.exception.Halt:
            # TODO: should this log as a http access log line?
            log.error('HTTP Response: connection lost')

    def read_request(self, timeout=None):
        method, path, version = self.socket.read_line().split(' ', 2)
        headers = self.read_headers()
        request = self.Request(method, path, version, headers)
        # TODO: handle chunked transfers
        length = int(headers.get('content-length', 0))
        request.body = self.socket.read_bytes(length)
        return request

    def write_response(self, code, message):
        self.socket.write('HTTP/1.1 %s %s\r\n' % (code, message))


class WebSocket(object):
    MASK = FIN = 0b10000000
    RSV = 0b01110000
    OP = 0b00001111
    CONTROL = 0b00001000
    PAYLOAD = 0b01111111

    OP_TEXT = 0x1
    OP_BIN = 0x2
    OP_CLOSE = 0x8
    OP_PING = 0x9
    OP_PONG = 0xA

    SANITY = 1024**3  # limit fragments to 1GB

    def __init__(self, hub, socket, is_client=True):
        self.hub = hub
        self.socket = socket
        self.socket.timeout = -1
        self.is_client = is_client
        self.recver = self.hub.producer(self.reader)

    @staticmethod
    def mask(mask, s):
        mask_bytes = [ord(c) for c in mask]
        return ''.join(
            chr(mask_bytes[i % 4] ^ ord(c)) for i, c in enumerate(s))

    @staticmethod
    def accept_key(key):
        value = key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        return base64.b64encode(hashlib.sha1(value).digest())

    def reader(self, sender):
        while True:
            try:
                sender.send(self._recv())
            except vanilla.exception.Halt:
                sender.close()
                return

    def recv(self):
        return self.recver.recv()

    def _recv(self):
        b1, length, = struct.unpack('!BB', self.socket.read_bytes(2))
        assert b1 & WebSocket.FIN, "Fragmented messages not supported yet"

        if self.is_client:
            assert not length & WebSocket.MASK
        else:
            assert length & WebSocket.MASK
            length = length & WebSocket.PAYLOAD

        # TODO: support binary
        opcode = b1 & WebSocket.OP

        if opcode & WebSocket.CONTROL:
            # this is a control frame
            assert length <= 125
            if opcode == WebSocket.OP_CLOSE:
                self.socket.read_bytes(length)
                self.socket.close()
                raise vanilla.exception.Closed

        if length == 126:
            length, = struct.unpack('!H', self.socket.read_bytes(2))

        elif length == 127:
            length, = struct.unpack('!Q', self.socket.read_bytes(8))

        assert length < WebSocket.SANITY, "Frames limited to 1Gb for sanity"

        if self.is_client:
            return self.socket.read_bytes(length)

        mask = self.socket.read_bytes(4)
        return self.mask(mask, self.socket.read_bytes(length))

    def send(self, data):
        length = len(data)

        MASK = WebSocket.MASK if self.is_client else 0

        if length <= 125:
            header = struct.pack(
                '!BB',
                WebSocket.OP_TEXT | WebSocket.FIN,
                length | MASK)

        elif length <= 65535:
            header = struct.pack(
                '!BBH',
                WebSocket.OP_TEXT | WebSocket.FIN,
                126 | MASK,
                length)
        else:
            assert length < WebSocket.SANITY, \
                "Frames limited to 1Gb for sanity"
            header = struct.pack(
                '!BBQ',
                WebSocket.OP_TEXT | WebSocket.FIN,
                127 | MASK,
                length)

        if self.is_client:
            mask = os.urandom(4)
            self.socket.write(header + mask + self.mask(mask, data))
        else:
            self.socket.write(header + data)

    def close(self):
        if not self.socket.closed:
            MASK = WebSocket.MASK if self.is_client else 0
            header = struct.pack(
                '!BB',
                WebSocket.OP_CLOSE | WebSocket.FIN,
                MASK)
            self.socket.write(header)
            self.socket.close()