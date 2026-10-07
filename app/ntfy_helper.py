"""One isolated ntfy HTTP attempt from verified read-only descriptors."""
from __future__ import annotations

import os
import sys
import resource

MEMORY_BYTES = 256 * 1024 * 1024
if __name__ == '__main__' and sys.platform == 'linux':
    _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    _ceiling = MEMORY_BYTES if _hard == resource.RLIM_INFINITY else min(MEMORY_BYTES, _hard)
    resource.setrlimit(resource.RLIMIT_AS, (_ceiling, _ceiling))

try:
    import asyncio
    import fcntl
    from pathlib import Path
    import stat
    import struct
    import threading
    import time
except MemoryError:
    os._exit(73)

READY = b'NTFYFD2 READY\n'
CHUNK_BYTES = 64 * 1024
RESPONSE_BYTES = 64 * 1024
_RESOURCE_FRAMES = {
    'input': b'R\x00\x00\x00\x14FAIL resource input\n',
    'client': b'R\x00\x00\x00\x15FAIL resource client\n',
    'request': b'R\x00\x00\x00\x16FAIL resource request\n',
}
ENVIRONMENT_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
                    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUEST_METHOD')


async def transport(config, body_fd, cookies_fd, state):
    import httpx
    import json
    from http.cookiejar import CookieJar, parse_ns_headers, split_header_words
    from app.ntfy_cookies import HEADER, FRAME_BYTES, COOKIE_RECORD_BYTES, cookie_chunks, restore_cookie

    def write(raw):
        while raw:
            raw = raw[os.write(1, raw):]

    def event(kind, chunks):
        size = sum(len(raw) for raw in chunks())
        if size > COOKIE_RECORD_BYTES:
            raise ValueError("cookie record exceeds inherited network bound")
        write(HEADER.pack(kind, size))
        for raw in chunks():
            for offset in range(0, len(raw), FRAME_BYTES):
                part = raw[offset:offset + FRAME_BYTES]
                write(HEADER.pack(b"T", len(part)))
                write(part)

    class TrackedJar(CookieJar):
        emitting = False

        def set_cookie(self, cookie):
            if self.emitting:
                event(b"S", lambda: cookie_chunks(cookie))
            super().set_cookie(cookie)

        def make_cookies(self, response, request):
            # CookieJar's two parsing guards catch Exception, which also
            # swallows MemoryError. Preserve its ordinary parse behavior while
            # making the selected helper resource stop a failed delivery.
            headers = response.info()
            rfc_headers = headers.get_all('Set-Cookie2', [])
            ns_headers = headers.get_all('Set-Cookie', [])
            self._policy._now = self._now = int(time.time())
            rfc, netscape = self._policy.rfc2965, self._policy.netscape
            if ((not rfc_headers and not ns_headers) or (not ns_headers and not rfc)
                    or (not rfc_headers and not netscape) or (not netscape and not rfc)):
                return []
            try:
                cookies = self._cookies_from_attrs_set(split_header_words(rfc_headers), request)
            except MemoryError:
                raise
            except Exception:
                cookies = []
            if ns_headers and netscape:
                try:
                    ns_cookies = self._cookies_from_attrs_set(parse_ns_headers(ns_headers), request)
                except MemoryError:
                    raise
                except Exception:
                    ns_cookies = []
                self._process_rfc2109_cookies(ns_cookies)
                if rfc:
                    lookup = {(cookie.domain, cookie.path, cookie.name): None for cookie in cookies}
                    ns_cookies = filter(lambda cookie: (cookie.domain, cookie.path, cookie.name) not in lookup,
                                        ns_cookies)
                if ns_cookies:
                    cookies.extend(ns_cookies)
            return cookies

        def clear(self, domain=None, path=None, name=None):
            if self.emitting:
                # Only response-derived expiry deletions reach the shared jar.
                if domain is None or path is None or name is None:
                    raise ValueError("invalid shared cookie deletion")
                self._cookies[domain][path][name]
                raw = json.dumps([domain, path, name], ensure_ascii=False).encode('utf8', 'surrogatepass')
                event(b"X", lambda: (raw,))
            super().clear(domain, path, name)

    if fcntl.fcntl(cookies_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ValueError("ntfy cookies descriptor requires read-only access")
    if not stat.S_ISREG(os.fstat(cookies_fd).st_mode):
        raise ValueError("invalid ntfy cookies descriptor")
    jar = TrackedJar()
    with os.fdopen(os.dup(cookies_fd), "rb") as source:
        for raw in source:
            jar.set_cookie(restore_cookie(json.loads(raw)))
    jar.emitting = True
    from app.provider_http import ProviderResponseTooLarge

    # Preserve relevant operator HTTP environment without inheriting the
    # serving process's credentials or other environment.
    environment = config['environment']
    if not isinstance(environment, dict) or not set(environment) <= set(ENVIRONMENT_KEYS):
        raise ValueError('invalid ntfy environment')
    os.environ.update(environment)
    state['phase'] = 'client'
    headers = {'Title': 'Odograph', 'Tags': 'car', 'Priority': 'default'}
    auth = None
    if config['username'] and config['password']:
        auth = httpx.BasicAuth(config['username'], config['password'])
    elif config['token']:
        headers['Authorization'] = 'Bearer ' + config['token']
    if fcntl.fcntl(body_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ValueError('ntfy body descriptor requires read-only access')
    info = os.fstat(body_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size != config['body_bytes']:
        raise ValueError('invalid ntfy body descriptor')
    url = config['url'].rstrip('/') + '/' + config['topic']
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), cookies=jar) as client:
        with os.fdopen(os.dup(body_fd), 'rb') as source:
            async def content():
                while data := source.read(CHUNK_BYTES):
                    yield data
            state['phase'] = 'request'
            request = client.build_request('POST', url, content=content(), headers=headers)
            # HTTPX adds its string-body length after cookies. Keep the same
            # header order while streaming the already verified body file.
            request.headers.pop('Transfer-Encoding', None)
            request.headers['Content-Length'] = str(info.st_size)
            response = await client.send(request, stream=True, auth=auth)
            try:
                response.raise_for_status()
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > RESPONSE_BYTES:
                        raise ProviderResponseTooLarge('provider response exceeds byte limit')
            finally:
                await response.aclose()


def result(raw):
    from app.ntfy_cookies import HEADER
    packet = HEADER.pack(b'R', len(raw)) + raw
    while packet:
        packet = packet[os.write(1, packet):]


def _descriptor_json(fd):
    import json
    if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ValueError('prepared ntfy input requires a read-only descriptor')
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError('prepared ntfy input is not a regular file')
    # Binary JSON decoding preserves Python surrogate pairs as distinct code
    # points. Complete parsing remains inside the helper memory authority.
    with os.fdopen(os.dup(fd), 'rb') as source:
        return json.load(source)


def main():
    deadline, parent, lifetime = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.smtp_helper import _guard_parent, _watch_parent, _read_exact
    _guard_parent(parent)
    threading.Thread(target=_watch_parent, args=(lifetime, deadline, True), daemon=True).start()
    if time.monotonic() >= deadline or os.getppid() != parent:
        os._exit(71)
    os.write(1, READY)
    state = {'phase': 'input'}
    try:
        config_fd, body_fd, cookies_fd = struct.unpack('!3i', _read_exact(12))
        config = _descriptor_json(config_fd)
        asyncio.run(transport(config, body_fd, cookies_fd, state))
    except MemoryError:
        os.write(1, _RESOURCE_FRAMES[state['phase']])
        return 73
    except Exception as exc:
        # Never emit provider text, endpoints, body or credentials.
        name = type(exc).__name__
        if name not in ('HTTPStatusError', 'ProviderResponseTooLarge', 'TimeoutException',
                        'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout',
                        'ConnectError', 'ReadError', 'WriteError', 'RemoteProtocolError',
                        'LocalProtocolError', 'UnsupportedProtocol', 'ProxyError', 'DecodingError',
                        'InvalidURL', 'ValueError', 'UnicodeEncodeError'):
            name = 'transport'
        result(b'FAIL ' + name.encode('ascii') + b' ' + state['phase'].encode('ascii') + b'\n')
        return 1
    result(b'OK\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
