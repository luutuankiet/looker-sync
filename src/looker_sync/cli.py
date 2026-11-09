#!/usr/bin/env python3
"""looker-sync: pull and push LookML between a local folder and a Looker dev workspace.

Talks to the Looker REST API (4.0) directly with the user's own API key. Standard library
only. Never commits, deploys, resets or creates branches: Looker's API has no commit or push
endpoint, so those stay with git and the Looker IDE.

    looker-sync [-C PROJECT_DIR] [--env-file PATH] COMMAND ...

    init    [--project P] [--mode dev|prod] [--branch B]  set up a folder, then a first pull
            [--git-remote NAME | --no-git]                link to git, or not (default: offer)
    link    [--remote NAME]                               link the folder to a git remote
    pull    [--force] [--dry-run]                         copy Looker files to disk
    status                                                show what differs on each side
    push    [--force] [--delete] [--dry-run]              write local edits, read back, validate
    watch   [--validate] [--verbose] [--poll MS] [--force] [--no-delete]  push each save as it
            happens: ack, then verified; a file you delete or rename while it runs is deleted in
            Looker too, and a folder that vanished with it once Looker has no file left there
            (--no-delete only reports it); holds Looker edits and syntax errors,
            pauses off-branch; a git checkout, pull or rebase never deletes anything
    switch  (--branch B | --mode dev|prod) [--local-changes push|backup|cancel] [--yes] [--force]
            --force = --local-changes backup --yes; when linked, git follows: stash, checkout, ff
    skill   [--path]                                      print the full usage guide for agents

Metadata lives in PROJECT_DIR/.looker-sync/ (config.json, state.json, backups/). Credentials
come only from the env file: LOOKER_URL, LOOKER_CLIENT_ID, LOOKER_CLIENT_SECRET. A folder whose
config names a `git_remote` is linked: every command first checks that the clone holds the
commit Looker is on. Without `git_remote` no git process is ever started.

Exit codes: 0 ok, 1 Looker or network error, 2 refused by a guard, 3 validation errors.
"""
import argparse
import collections
import contextlib
import datetime
import errno
import functools
import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
# connection layer (keep-alive pool, parallel reads, token refresh)
import concurrent.futures
import http.client
import socket
import ssl
import threading

META_DIR = '.looker-sync'
DEFAULT_INCLUDE = ['**/*.lkml', '**/*.dashboard.lookml']
# What a fresh init writes. Looker's file API accepted these types (live check, 2026-11-09).
INIT_INCLUDE = DEFAULT_INCLUDE + ['**/*.js', '**/*.md', '**/*.strings.json']
INIT_EXCLUDE = ['**/node_modules/**']
OK, LOOKER_ERROR, REFUSED, INVALID = 0, 1, 2, 3
WORKSPACE = {'dev': 'dev', 'prod': 'production'}


class LookerError(Exception):
    pass


class Refused(Exception):
    pass


def out(msg=''):
    print(msg, flush=True)


def sha(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- env and API

def load_env(path):
    if not os.path.isfile(path):
        raise LookerError(f'env file not found: {path}')
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('export '):
                line = line[len('export '):]
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in ('LOOKER_URL', 'LOOKER_CLIENT_ID', 'LOOKER_CLIENT_SECRET') if not env.get(k)]
    if missing:
        raise LookerError(f'env file {path} lacks {", ".join(missing)}')
    return env


class AuthExpired(LookerError):
    """Looker still answers HTTP 401 after a fresh login, or the fresh login itself failed.

    A one-shot command exits 1 on it like any LookerError; watch catches it to pause.
    """


class ConnectionPool:
    """Keep-alive HTTP(S) connections to one Looker host, safe to share between threads.

    Each connection is used by one thread at a time: a request takes an idle one (or opens
    a new one) and gives it back when the response is fully read. Every request therefore
    runs on its own connection, and the pool grows to the number of threads in flight.
    """

    IDEMPOTENT = ('GET', 'HEAD', 'OPTIONS')
    MAX_IDLE = 16

    def __init__(self, base_url, timeout=120):
        u = urllib.parse.urlsplit(base_url)
        if u.scheme not in ('http', 'https') or not u.hostname:
            raise LookerError(f'LOOKER_URL must start with http:// or https://, got {base_url!r}')
        self.scheme, self.host = u.scheme, u.hostname
        self.port = u.port or (443 if u.scheme == 'https' else 80)
        self.prefix = u.path.rstrip('/')
        self.timeout = timeout
        # urlopen honoured *_proxy and no_proxy; keep that by tunnelling through the proxy
        proxy = urllib.request.getproxies().get(u.scheme)
        self.proxy = None
        if proxy and not urllib.request.proxy_bypass(self.host):
            p = urllib.parse.urlsplit(proxy if '://' in proxy else 'http://' + proxy)
            self.proxy = (p.hostname, p.port or 80)
        self._ssl = ssl.create_default_context() if u.scheme == 'https' else None
        self._idle = []
        self._lock = threading.Lock()
        self.opened = 0              # connections opened so far; tests and timings read it

    def _open(self):
        host, port = self.proxy or (self.host, self.port)
        if self.scheme == 'https':
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._ssl)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        if self.proxy:
            conn.set_tunnel(self.host, self.port)
        with self._lock:
            self.opened += 1
        return conn

    def _acquire(self):
        with self._lock:
            if self._idle:
                return self._idle.pop(), True
        return self._open(), False

    def _release(self, conn):
        with self._lock:
            if len(self._idle) < self.MAX_IDLE:
                self._idle.append(conn)
                return
        conn.close()

    def close(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    def request(self, method, path, data=None, headers=None):
        """(status, body bytes) for any HTTP status; LookerError for a network failure."""
        for attempt in (1, 2):
            conn, reused = self._acquire()
            got_response = False
            try:
                if conn.sock is None:
                    conn.connect()
                    try:   # newer http.client does this itself; older ones stall ~40 ms per write
                        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except (OSError, AttributeError):
                        pass
                conn.request(method, self.prefix + path, body=data, headers=headers or {})
                resp = conn.getresponse()
                got_response = True
                payload = resp.read()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError, socket.timeout) as e:
                conn.close()
                # Retry once, and only on a connection that sat in the pool: a server or proxy
                # may close an idle keep-alive connection, and the client finds out only when it
                # reuses it. A fresh connection failing is a real error.
                # Reads are safe to repeat whatever happened. A write (POST, PUT, PATCH, DELETE)
                # is repeated only if not a single response byte was read and the error is a
                # dead connection, not a timeout: on a closed idle connection the server never
                # saw the request. The remaining risk, a server that applied the write and then
                # dropped the connection before answering, would repeat an identical write
                # (same path, same bytes), and the read-back after every push still checks it.
                timed_out = isinstance(e, socket.timeout)
                retry = reused and attempt == 1 and (
                    method in self.IDEMPOTENT or (not got_response and not timed_out))
                if retry:
                    continue
                raise LookerError(f'{method} {path}: {e or type(e).__name__}')
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                raise LookerError(f'{method} {path}: {e or type(e).__name__}')
            if resp.will_close:
                conn.close()
            else:
                self._release(conn)
            if 300 <= resp.status < 400:
                # urlopen followed redirects; an API call that redirects means LOOKER_URL is off
                raise LookerError(f'{method} {path}: HTTP {resp.status} redirect to '
                                  f'{resp.getheader("Location")}; set LOOKER_URL to the address it names')
            return resp.status, payload


class Looker:
    """One API client per process: a keep-alive connection pool and one in-memory token.

    The token is shared by every thread and connection. It is refreshed (a new login, then the
    session workspace set again on the new token, then an atomic swap) once 80% of its
    `expires_in` has passed: lazily before the next request, or by the opt-in background thread
    (`start_auto_refresh`, which watch uses). An HTTP 401 triggers one refresh and one retry; a
    second 401 raises AuthExpired. The token is never printed and never written to disk.
    """

    REFRESH_AT = 0.8

    def __init__(self, env, timeout=120):
        url = env['LOOKER_URL'].rstrip('/')
        self.base = url if url.endswith('/api/4.0') else url + '/api/4.0'
        self.pool = ConnectionPool(self.base, timeout)
        self._form = urllib.parse.urlencode({'client_id': env['LOOKER_CLIENT_ID'],
                                             'client_secret': env['LOOKER_CLIENT_SECRET']}).encode()
        self.workspace = None        # set by set_workspace; a refresh sets it again on the new token
        # Called as before_token_swap(new_token) after the new token is in the right workspace and
        # before it replaces the old one; raise to keep the old token (watch checks the branch here).
        self.before_token_swap = None
        self.on_refresh_error = None  # called with the exception when a background refresh fails
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._refresher = None
        self._auth = self._login()   # (token, monotonic time to refresh at)

    # ---- token

    @property
    def token(self):
        return self._auth[0]

    def _login(self):
        started = time.monotonic()
        status, payload = self.pool.request('POST', '/login', self._form,
                                            {'Content-Type': 'application/x-www-form-urlencoded'})
        if status != 200:
            # The request carried the secret; the response never does, but keep it terse anyway.
            raise LookerError(f'login failed: HTTP {status}')
        res = json.loads(payload)
        expires = res.get('expires_in')
        due = started + self.REFRESH_AT * float(expires) if expires else float('inf')
        return res['access_token'], due

    def set_workspace(self, workspace_id):
        """PATCH /session to the workspace, confirm it, and remember it for every refresh."""
        self._set_workspace(workspace_id, None)
        self.workspace = workspace_id

    def _set_workspace(self, want, token):
        self.call('PATCH', '/session', body={'workspace_id': want}, token=token)
        got = (self.call('GET', '/session', token=token) or {}).get('workspace_id')
        if got != want:
            raise LookerError(f'session workspace is {got!r} after asking for {want!r}')

    def refresh(self, stale=None):
        """Log in again, put the new token in the workspace, then swap it in.

        `stale` is the token the caller saw fail or expire; if another thread has already
        replaced it, nothing is done. Raises AuthExpired if the new login is refused.
        """
        with self._refresh_lock:
            if stale is not None and self._auth[0] != stale:
                return
            try:
                auth = self._login()
            except LookerError as e:
                if 'login failed' in str(e):
                    raise AuthExpired(f'{e}; check LOOKER_CLIENT_ID and LOOKER_CLIENT_SECRET') from None
                raise
            if self.workspace:
                self._set_workspace(self.workspace, auth[0])
            if self.before_token_swap:
                self.before_token_swap(auth[0])
            self._auth = auth        # one assignment: every thread sees the old or the new pair

    def _token_for_request(self):
        token, due = self._auth
        if time.monotonic() >= due:
            try:
                self.refresh(stale=token)
            except LookerError:
                pass                 # the old token may still work; a 401 retries the refresh
            token = self._auth[0]
        return token

    def start_auto_refresh(self):
        """Refresh in a background thread at 80% of the token lifetime. Watch only; idempotent."""
        if self._refresher:
            return
        self._stop.clear()
        self._refresher = threading.Thread(target=self._refresh_loop, name='looker-token-refresh',
                                           daemon=True)
        self._refresher.start()

    def stop_auto_refresh(self):
        self._stop.set()
        if self._refresher:
            self._refresher.join(timeout=5)
        self._refresher = None

    def _refresh_loop(self):
        while not self._stop.is_set():
            token, due = self._auth
            if self._stop.wait(max(0.0, min(due - time.monotonic(), 3600.0))):
                return
            if self._auth[0] != token or time.monotonic() < due:
                continue
            try:
                self.refresh(stale=token)
            except Exception as e:   # keep the loop alive; requests still refresh on 401
                if self.on_refresh_error:
                    self.on_refresh_error(e)
                self._stop.wait(30)

    def close(self):
        self.stop_auto_refresh()
        self.pool.close()

    # ---- requests

    def _send(self, method, path, data=None, headers=None):
        return self.pool.request(method, path, data, headers)

    def try_call(self, method, path, query=None, body=None, raw=False, token=None):
        """(status, payload); an HTTP error is (status, message), a network error LookerError.

        `token` sends the request with that token, without the 401 refresh (used while a
        refresh prepares a new token).
        """
        if query:
            path += '?' + urllib.parse.urlencode(query)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        use = token or self._token_for_request()
        status, payload = self._send(method, path, data, dict(headers, Authorization='token ' + use))
        if status == 401 and token is None:
            self.refresh(stale=use)
            status, payload = self._send(method, path, data, dict(headers, Authorization='token ' + self.token))
            if status == 401:
                raise AuthExpired(f'{method} {path.split("?")[0]} -> HTTP 401 again after a fresh login')
        if status >= 400:
            try:
                msg = json.loads(payload).get('message') or payload.decode(errors='replace')
            except (ValueError, AttributeError):
                msg = payload.decode(errors='replace')
            return status, msg[:300]
        if raw:
            return status, payload
        return status, json.loads(payload) if payload else None

    def call(self, method, path, query=None, body=None, raw=False, token=None):
        status, payload = self.try_call(method, path, query, body, raw, token)
        if status >= 400:
            raise LookerError(f'{method} {path.split("?")[0]} -> HTTP {status}: {payload}')
        return payload

    def parallel(self, fn, items, workers=8):
        """[fn(item) for item in items] on up to `workers` threads over the pool, in input order.

        The first exception is raised once the calls already started have finished.
        """
        items = list(items)
        if workers <= 1 or len(items) <= 1:
            return [fn(i) for i in items]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
            futures = [ex.submit(fn, i) for i in items]
            try:
                return [f.result() for f in futures]
            except BaseException:
                for f in futures:
                    f.cancel()
                raise

    def read_file(self, project, path):
        return self.call('GET', f'/projects/{project}/file/content', {'file_path': path}, raw=True)

    def read_files(self, project, paths, workers=8):
        """[bytes] for each path of a Looker project, read in parallel, in input order."""
        return self.parallel(lambda p: self.read_file(project, p), paths, workers)


# ---------------------------------------------------------------- project folder

@functools.lru_cache(maxsize=512)
def glob_regex(pattern):
    i, rx = 0, ''
    while i < len(pattern):
        if pattern.startswith('**/', i):
            rx, i = rx + '(?:.*/)?', i + 3
        elif pattern.startswith('**', i):
            rx, i = rx + '.*', i + 2
        elif pattern[i] == '*':
            rx, i = rx + '[^/]*', i + 1
        elif pattern[i] == '?':
            rx, i = rx + '[^/]', i + 1
        else:
            rx, i = rx + re.escape(pattern[i]), i + 1
    return re.compile(rx + r'\Z')


# Local scan. Two pieces, so a watcher can poll cheaply and read only what changed:
#   scan_tree    stat only (os.scandir), no file is opened, so a FIFO cannot block it
#   read_stable  one verified read of one file
# Names starting with '.', '~' or '#' are never synced, at any depth: editor temps and lock
# files ('.#x.lkml', '._x.lkml', '~x.lkml', '#x.lkml#') and hidden folders ('.git').

ScanEntry = collections.namedtuple('ScanEntry', 'path mtime_ns size ino dev')
ScanEntry.__doc__ = """One regular file seen by scan_tree: its path relative to the root ('/'
separated) and the stat fields that tell a changed file from an unchanged one."""

Scan = collections.namedtuple('Scan', 'entries unreadable carried started_ns')
Scan.__doc__ = """Result of scan_tree.
entries     {relative path: ScanEntry} for every regular file in the sync set
unreadable  sorted relative folder paths ('' for the root) that could not be listed; their
            files are unknown, NOT absent
carried     paths in entries copied from the previous scan because their folder could not be
            listed this time
started_ns  time.time_ns() just before the scan, to spot an mtime within one tick of it"""

ReadResult = collections.namedtuple('ReadResult', 'status data entry')
ReadResult.__doc__ = """Result of read_stable.
status  'ok'           data is the whole file and nothing changed while reading
        'vanished'     the path no longer exists (or is a dangling symlink)
        'unstable'     size, mtime or inode changed during the read, or the path now names
                       another file; read again later
        'not_regular'  FIFO, socket, device, folder or symlink loop; never synced
        'unreadable'   permission denied or another OS error
data    the bytes when status is 'ok', else None
entry   ScanEntry from the fstat after the read when status is 'ok' or 'unstable', else None"""


def ignored_name(name):
    """True for a file or folder name that is never synced: it starts with '.', '~' or '#'."""
    return name[:1] in ('.', '~', '#')


def _entry(rel, st):
    return ScanEntry(rel, st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev)


def scan_tree(root, synced, previous=None):
    """Stat every file under root without opening any; return a Scan.

    synced(rel) decides the sync set. Hidden and ignored names (see ignored_name) are skipped
    and their folders not entered; symlinked folders are not followed (as os.walk does), a
    symlink to a regular file counts as that file. Skipped without error: FIFOs, sockets,
    devices, dangling symlinks, symlink loops, and anything that vanishes mid-scan (ENOENT).
    A folder that cannot be listed goes into Scan.unreadable; when `previous` (an earlier Scan
    or its entries dict) is given, its entries under that folder are kept and listed in
    Scan.carried, so a caller never mistakes them for deleted files.
    """
    started = time.time_ns()
    entries, unreadable = {}, []
    stack = ['']
    while stack:
        rel_dir = stack.pop()
        full_dir = os.path.join(root, *rel_dir.split('/')) if rel_dir else root
        try:
            it = os.scandir(full_dir)
        except FileNotFoundError:
            if not rel_dir:
                unreadable.append(rel_dir)  # the root itself is gone: unknown, not empty
            continue                        # folder vanished between listing and entering
        except OSError:
            unreadable.append(rel_dir)      # EACCES, ENOTDIR raced in, EIO, ...
            continue
        with it:
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    unreadable.append(rel_dir)
                    break
                if ignored_name(e.name):
                    continue
                rel = f'{rel_dir}/{e.name}' if rel_dir else e.name
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(rel)
                        continue
                    if not synced(rel):
                        continue
                    st = e.stat(follow_symlinks=True)
                except OSError:
                    continue                # vanished, dangling symlink, loop (ELOOP), EACCES
                if stat.S_ISREG(st.st_mode):
                    entries[rel] = _entry(rel, st)
    unreadable.sort()
    carried = []
    if previous is not None and unreadable:
        prev = previous.entries if isinstance(previous, Scan) else previous
        for path, ent in prev.items():
            if path in entries:
                continue
            if any(d == '' or path.startswith(d + '/') for d in unreadable):
                entries[path] = ent
                carried.append(path)
    return Scan(entries, unreadable, sorted(carried), started)


def read_stable(full_path, rel=None):
    """Read one file without ever blocking, and prove nothing changed while reading.

    Opens with O_NONBLOCK (a FIFO returns at once instead of waiting for a writer), checks
    S_ISREG on the open descriptor, reads to EOF, fstats again and finally stats the path.
    Returns a ReadResult; never raises for a vanished, special or changing file.
    """
    rel = full_path if rel is None else rel
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOCTTY', 0)
    try:
        fd = os.open(full_path, flags)
    except FileNotFoundError:
        return ReadResult('vanished', None, None)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENXIO, errno.EISDIR):
            return ReadResult('not_regular', None, None)   # ENXIO: a socket
        return ReadResult('unreadable', None, None)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return ReadResult('not_regular', None, None)
        chunks = []
        while True:
            chunk = os.read(fd, max(before.st_size, 65536))
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
    except OSError:
        return ReadResult('unreadable', None, None)
    finally:
        os.close(fd)
    data = b''.join(chunks)
    entry = _entry(rel, after)
    key = lambda s: (s.st_size, s.st_mtime_ns, s.st_ino, s.st_dev)  # noqa: E731
    if key(before) != key(after) or len(data) != after.st_size:
        return ReadResult('unstable', None, entry)
    try:
        now = os.stat(full_path)
    except FileNotFoundError:
        return ReadResult('vanished', None, None)
    except OSError:
        return ReadResult('unstable', None, entry)
    if (now.st_ino, now.st_dev) != (after.st_ino, after.st_dev):
        return ReadResult('unstable', None, entry)     # replaced (atomic save) while reading
    return ReadResult('ok', data, entry)


class Project:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.meta = os.path.join(self.root, META_DIR)
        self.config_path = os.path.join(self.meta, 'config.json')
        self.state_path = os.path.join(self.meta, 'state.json')
        self.config = self._read(self.config_path)
        self.state = self._read(self.state_path) or {'files': {}}

    @staticmethod
    def _read(path):
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            return json.load(f)

    def _write(self, path, data):
        os.makedirs(self.meta, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp, path)

    def save_config(self):
        self._write(self.config_path, self.config)

    def save_state(self):
        self._write(self.state_path, self.state)

    def require_config(self):
        if not self.config:
            raise Refused(f'no {META_DIR}/config.json in {self.root}; run init first')
        return self.config

    def synced(self, path):
        """True if the path is in the sync set. The metadata folder never is, nor any path with
        a name part starting with '.', '~' or '#' (editor temps, hidden folders)."""
        parts = path.split('/')
        if parts[0] == META_DIR or any(ignored_name(p) for p in parts):
            return False
        cfg = self.config or {}
        inc = [glob_regex(p) for p in cfg.get('include', DEFAULT_INCLUDE)]
        exc = [glob_regex(p) for p in cfg.get('exclude', [])]
        return any(r.match(path) for r in inc) and not any(r.match(path) for r in exc)

    def full_path(self, rel):
        return os.path.join(self.root, *rel.split('/'))

    def scan(self, previous=None):
        """Stat-only Scan of the sync set (scan_tree with this project's rules); reads nothing."""
        return scan_tree(self.root, self.synced, previous)

    def read_entry(self, rel):
        """read_stable for one relative path of the sync set."""
        return read_stable(self.full_path(rel), rel)

    def local_files(self, retries=3):
        """{path: bytes} for every regular file in the sync set, from scan() plus read_entry().

        Skips FIFOs, sockets, dangling symlinks and files that vanish before they are read.
        Refuses (rather than report files as absent, which a push --delete would act on) when a
        folder cannot be listed or a file cannot be read, and when a file keeps changing
        while it is read.
        """
        result = self.scan()
        if result.unreadable:
            names = ', '.join(d or '.' for d in result.unreadable)
            raise Refused(f'cannot list folder(s) {names} in {self.root}; '
                          'fix their permissions so their files are not taken as deleted')
        found = {}
        for rel in sorted(result.entries):
            for attempt in range(retries + 1):
                r = self.read_entry(rel)
                if r.status != 'unstable':
                    break
                time.sleep(0.05 * (attempt + 1))
            if r.status == 'ok':
                found[rel] = r.data
            elif r.status == 'unstable':
                raise Refused(f'{rel} kept changing while being read; wait for the save to finish')
            elif r.status == 'unreadable':
                raise Refused(f'cannot read {rel}; fix its permissions')
            # 'vanished' and 'not_regular': not a file of the sync set any more
        return found

    def write_local(self, path, data):
        full = os.path.join(self.root, *path.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(data)

    def env_path(self, flag):
        flag = flag or os.environ.get('LOOKER_SYNC_ENV')
        if flag:
            return flag
        if self.config and self.config.get('env_file'):
            return os.path.join(self.root, self.config['env_file'])
        return os.path.join(self.root, '.env')


# ---------------------------------------------------------------- local git
#
# Every git process goes through run_git, so the scope rule is checked in this one place.
# Allowed: reads, `fetch`, `stash push`, `checkout` (including creating a branch from its remote
# counterpart) and `merge --ff-only`. Never: commit, push, pull, reset, rebase, clean, or popping
# a stash. Looker's own git state is never touched (see the branch endpoint and `ref` above).
# Only a linked folder (config key `git_remote`, a remote *name*) or the init offer in a folder
# that looks like a git clone ever reaches run_git.
#
# LOOKER_SYNC_GIT_LOG names a file; when set, run_git appends one JSON line per call:
# {"args": [...], "cwd": "...", "rc": N}. Tests read it to assert which git commands ran. It
# records arguments and exit codes only, never git's output.

GIT_READS = {'rev-parse', 'symbolic-ref', 'status', 'cat-file', 'merge-base', 'remote', 'for-each-ref'}
GIT_ALLOWED = GIT_READS | {'fetch', 'stash', 'checkout', 'merge'}


def git_in_scope(args):
    cmd = args[0] if args else None
    if cmd not in GIT_ALLOWED:
        return False
    if cmd == 'remote':
        return args[1:2] == ['get-url']
    if cmd == 'for-each-ref':
        return '--contains' in args   # only the commit check's containment read
    if cmd == 'stash':
        return args[1:2] == ['push']
    if cmd == 'merge':
        return '--ff-only' in args
    if cmd == 'checkout' and '-b' in args:
        return '--track' in args      # a new branch only ever tracks its remote counterpart
    return True


def run_git(root, *args, timeout=60):
    """Run `git -C root ARGS`; return (returncode, stdout, stderr), both stripped."""
    args = list(args)
    if not git_in_scope(args):
        raise RuntimeError(f'git {" ".join(args)} is outside the git scope of looker-sync')
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')   # never hang on a credential prompt
    try:
        r = subprocess.run(['git', '-C', root, *args], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=timeout, env=env)
        rc, stdout, stderr = r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        rc, stdout, stderr = 127, '', 'git is not installed or not on PATH'
    except subprocess.TimeoutExpired:
        rc, stdout, stderr = 124, '', f'timed out after {timeout}s'
    log = os.environ.get('LOOKER_SYNC_GIT_LOG')
    if log:
        with open(log, 'a') as f:
            f.write(json.dumps({'args': args, 'cwd': root, 'rc': rc}) + '\n')
    return rc, stdout.strip(), stderr.strip()


def looks_like_git(root):
    """True when root or a parent holds a .git entry. Pure Python: spawns no git."""
    d = os.path.abspath(root)
    while True:
        if os.path.exists(os.path.join(d, '.git')):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


class LocalGit:
    """Reads of the local clone at root, all through run_git."""

    def __init__(self, root):
        self.root = root

    def run(self, *args, **kw):
        return run_git(self.root, *args, **kw)

    def is_work_tree(self):
        rc, stdout, _ = self.run('rev-parse', '--is-inside-work-tree')
        return rc == 0 and stdout == 'true'

    def branch(self):
        """The checked-out branch, or None on a detached HEAD."""
        rc, stdout, _ = self.run('symbolic-ref', '--quiet', '--short', 'HEAD')
        return stdout if rc == 0 and stdout else None

    def dirty(self, exclude=()):
        """Changed and untracked paths under root, leaving out the `exclude` paths (relative)."""
        rc, stdout, stderr = self.run('status', '--porcelain', '--untracked-files=all', *pathspecs(exclude))
        if rc:
            raise Refused(f'git status failed in {self.root}: {stderr}')
        return stdout.splitlines()

    def ignored(self, paths):
        """The given relative paths that git ignores."""
        rc, stdout, _ = self.run('status', '--porcelain', '--ignored=matching', '--untracked-files=normal',
                                 '--', *[f':(literal){p}' for p in paths])
        found = {line[3:].rstrip('/') for line in stdout.splitlines() if line.startswith('!! ')}
        return [p for p in paths if p in found]

    def has_ref(self, ref):
        return self.run('rev-parse', '--verify', '--quiet', ref)[0] == 0

    def head(self):
        rc, stdout, _ = self.run('rev-parse', 'HEAD')
        return stdout if rc == 0 else None

    def short_head(self):
        rc, stdout, _ = self.run('rev-parse', '--short', 'HEAD')
        return stdout if rc == 0 else None

    def has_commit(self, sha):
        if not sha or not re.fullmatch(r'[0-9a-f]{4,64}', sha):
            return False
        return self.run('cat-file', '-e', sha + '^{commit}')[0] == 0

    def on_remote(self, sha, remote):
        """True when commit sha is reachable from a ref under refs/remotes/<remote>/ (as last fetched)."""
        if not sha or not re.fullmatch(r'[0-9a-f]{4,64}', sha):
            return False
        rc, stdout, _ = self.run('for-each-ref', '--count=1', '--format=%(refname)', '--contains', sha,
                                 f'refs/remotes/{remote}/')
        return rc == 0 and bool(stdout)

    def is_ancestor(self, a, b):
        return self.run('merge-base', '--is-ancestor', a, b)[0] == 0

    def remote_url(self, name):
        rc, stdout, _ = self.run('remote', 'get-url', name)
        return stdout if rc == 0 else None

    def fetch(self, remote):
        """None on success, else the last line of git's complaint."""
        rc, _, stderr = self.run('fetch', '--quiet', remote, timeout=180)
        return None if rc == 0 else (stderr.splitlines() or [f'exit {rc}'])[-1]


def pathspecs(exclude):
    """`-- . :(exclude,literal)P ...`: the whole folder except the given relative paths."""
    return ['--', '.'] + [f':(exclude,literal){p}' for p in exclude]


def own_paths(proj, env_flag=None):
    """The tool's own paths inside the folder, relative: never git's business, whatever .gitignore says."""
    paths = [META_DIR]
    env = os.path.abspath(proj.env_path(env_flag))
    if env.startswith(proj.root + os.sep):
        paths.append(os.path.relpath(env, proj.root).replace(os.sep, '/'))
    return paths


def git_target(sess, mode, branch):
    """The git branch a folder in `mode` (dev: on Looker branch `branch`) belongs on."""
    if mode == 'dev':
        return branch
    return (sess.project_info() or {}).get('git_production_branch_name') or sess.branch


def expected_git_branch(proj, sess):
    """The git branch the config says the folder is on: the config branch in dev mode, Looker's
    production branch name in prod mode (falling back to the branch Looker reports)."""
    cfg = proj.require_config()
    return git_target(sess, cfg['mode'], cfg.get('branch'))


def git_drift(proj, sess):
    """(expected, actual) when a linked folder's git branch is not the expected one, else None.
    actual is None on a detached HEAD. An unlinked folder never drifts and spawns no git."""
    if not (proj.config or {}).get('git_remote'):
        return None
    expected, actual = expected_git_branch(proj, sess), LocalGit(proj.root).branch()
    return None if actual == expected else (expected, actual)


def drift_guard(proj, sess, cmd, refuse):
    """In a linked folder off the expected branch: status warns (refuse False); pull and push
    refuse, so one branch's files never land on another branch's checkout. Both print the fix."""
    drift = git_drift(proj, sess)
    if not drift:
        return
    expected, actual = drift
    cfg = proj.require_config()
    where = f'{actual!r}' if actual else 'a detached HEAD'
    fix = f'looker-sync switch --branch {cfg["branch"]} --force' if cfg['mode'] == 'dev' else \
        'looker-sync switch --mode prod --force'
    if refuse:
        raise Refused(f'git is on {where}, not {expected!r}, so {cmd} would sync one branch\'s files '
                      f'with another branch\'s checkout. Put git back on {expected!r} with:\n    {fix}')
    out(f'warning: git is on {where}, not {expected!r}; pull and push refuse until it is back. Fix:\n    {fix}')


# A real remote is a URL with a scheme or scp-like `user@host:path`. Anything else Looker
# reports (live: a relative path into its own bare-model store) is a Looker-hosted repository.
REMOTE_URL = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://|[^/\s@:]+@[^/\s:]+:')
REMOTE_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._/-]*\Z')


def safe_url(url):
    """A remote URL fit to print: user info of an http(s) URL (it can hold a token) is masked."""
    return re.sub(r'^(https?://)[^/@]*@', r'\1***@', url) if url else '(none)'


def not_linkable(info, project):
    """Why a Looker project cannot be linked to a clone, or None."""
    if not info.get('uses_git'):
        return f'Looker project {project!r} does not use git, so there is no repository to clone'
    url = info.get('git_remote_url')
    if not url:
        return f'Looker reports no remote repository for project {project!r}, so there is nothing to clone'
    if not REMOTE_URL.match(url):
        return (f'project {project!r} keeps its git in a Looker-hosted bare repository ({url}), '
                'so there is nothing to clone')
    return None


def commit_check(proj, sess, remote, fetch=True):
    """Prove the linked remote and the Looker project are one repository: the remote has Looker's commit.

    Fetch first (a failure only warns). Then Looker's `ref`, else `remote_ref`, must exist in
    the clone AND be reachable from a ref under refs/remotes/<remote>/, so a commit fetched
    from another remote does not count. If neither qualifies and Looker reports unpushed
    commits, warn; otherwise refuse, printing both remote URLs. No URL is ever compared.
    fetch=False (push) fetches only when needed: when the commit already passes against the
    remote as last fetched, a fetch could not change the verdict, so it is skipped.
    Returns the local remote's URL.
    """
    g = LocalGit(proj.root)
    if not g.is_work_tree():
        raise Refused(f'{proj.root} is not a git work tree, so it cannot be linked to remote {remote!r}')
    local_url = g.remote_url(remote)
    if local_url is None:
        raise Refused(f'no git remote named {remote!r} in {proj.root}; '
                      'list them with `git remote -v`, then: looker-sync link --remote NAME')
    info = getattr(sess, 'branch_info', None) or {}
    name, ref, remote_ref = info.get('name') or sess.branch, info.get('ref'), info.get('remote_ref')
    shas = list(dict.fromkeys(s for s in (ref, remote_ref) if s))
    # fetch adds refs and never removes one (no --prune), so a pass without it stays a pass after it
    if not fetch and any(g.has_commit(s) and g.on_remote(s, remote) for s in shas):
        return local_url
    err = g.fetch(remote)
    if err:
        out(f'warning: git fetch {remote} failed ({err}); checking against what was fetched before')
    if not (ref or remote_ref):
        out(f'warning: Looker reported no commit for branch {name!r}; the clone was not checked')
        return local_url
    local = [sha for sha in dict.fromkeys((ref, remote_ref)) if g.has_commit(sha)]
    if any(g.on_remote(sha, remote) for sha in local):
        return local_url
    shown = (ref or remote_ref)[:12]
    ahead = info.get('ahead_count') or 0
    if ahead > 0:
        out(f"warning: Looker's branch {name!r} is on {shown}, which remote {remote!r} does not have; it has "
            f'{ahead} commit(s) Looker has not pushed to the remote yet. Push them from the Looker IDE.')
        return local_url
    looker_url = (sess.project_info() or {}).get('git_remote_url')
    if local:
        what = (f"commit {local[0][:12]}, which Looker's branch {name!r} is on, is in this clone but on no "
                f'branch of remote {remote!r}, even after fetching it. That remote looks like a different '
                'repository, or a copy without Looker\'s commits.\n')
    else:
        what = (f"this clone does not have commit {shown}, which Looker's branch {name!r} is on, even "
                f'after fetching {remote!r}. It looks like a clone of a different repository.\n')
    raise Refused(what +
                  f"    Looker's remote:  {safe_url(looker_url)}\n"
                  f'    remote {remote!r}:  {safe_url(local_url)}\n'
                  '  Point the remote at the right repository (git remote set-url), or link another '
                  'one: looker-sync link --remote NAME')


def link_folder(proj, sess, remote):
    """Check everything, then write `git_remote` to the config. Returns the remote's URL."""
    if not REMOTE_NAME.match(remote or ''):
        raise Refused(f'{remote!r} is not a git remote name; give the name (such as origin), not a URL')
    reason = not_linkable(sess.project_info() or {}, sess.project)
    if reason:
        raise Refused(reason)
    url = commit_check(proj, sess, remote)
    proj.config['git_remote'] = remote
    proj.save_config()
    return url


# ---------------------------------------------------------------- session

class Session:
    """Log in, set the workspace from the config mode, confirm it, print who and where."""

    def __init__(self, proj, env_flag, mode, project, branch, check_branch=True):
        self.proj, self.project, self.mode = proj, project, mode
        self.L = Looker(load_env(proj.env_path(env_flag)))
        self.set_mode(mode)
        u = self.L.call('GET', '/user')
        self.user = f'{u.get("display_name")} <{u.get("email")}> [id {u.get("id")}]'
        self.branch = self.current_branch()
        shown = self.branch if mode == 'dev' else f'{self.branch} (production)'
        out(f'looker-sync: as {self.user} · project {project} · mode {mode} · branch {shown}')
        if mode == 'dev' and check_branch and branch != self.branch:
            raise Refused(f'branch mismatch: config says {branch!r}, Looker dev workspace is on '
                          f'{self.branch!r}. Run switch, or check out {branch!r} in the Looker IDE.')

    def set_mode(self, mode):
        self.L.set_workspace(WORKSPACE[mode])   # remembered: a token refresh sets it again
        self.mode = mode

    def current_branch(self):
        status, b = self.L.try_call('GET', f'/projects/{self.project}/git_branch')
        if status >= 400:
            if self.mode == 'dev':
                raise LookerError(f'GET git_branch -> HTTP {status}: {b}')
            self.branch_info = None   # never fast-forward production to a stale dev ref
            return None
        self.branch_info = b
        return b.get('name')

    def project_info(self):
        if not hasattr(self, '_project_info'):
            self._project_info = self.L.call('GET', f'/projects/{self.project}')
        return self._project_info

    def file_list(self):
        return self.L.call('GET', f'/projects/{self.project}/files') or []

    def remote_files(self):
        """{path: bytes} for every Looker file in the sync set."""
        paths = [f['path'] for f in self.file_list() if self.proj.synced(f['path'])]
        return dict(zip(paths, self.L.read_files(self.project, paths)))

    def read(self, path):
        return self.L.call('GET', f'/projects/{self.project}/file/content', {'file_path': path}, raw=True)

    def write(self, path, data, exists):
        body = {'path': path, 'content': data.decode('utf-8')}
        base = f'/projects/{self.project}/files'
        if exists:
            return self.L.call('PUT', base, body=body)
        status, msg = self.L.try_call('POST', base, body=body)
        # Looker answers 400 'No such file or directory' for a missing folder; older/other builds 404
        missing_folder = status == 404 or (status == 400 and 'No such file or directory' in str(msg))
        if missing_folder and '/' in path:
            parts = path.split('/')[:-1]
            for i in range(1, len(parts) + 1):
                self.L.try_call('POST', f'/projects/{self.project}/directories', body={'path': '/'.join(parts[:i])})
            status, msg = self.L.try_call('POST', base, body=body)
        if status >= 400:
            raise LookerError(f'POST {base} {path} -> HTTP {status}: {msg}')

    def delete(self, path):
        self.L.call('DELETE', f'/projects/{self.project}/files', {'file_path': path})

    def validate(self, verbose=True):
        """Validate the project, print its findings, return the error count.
        verbose=False hides info-severity lines and says how many it hid."""
        res = self.L.call('POST', f'/projects/{self.project}/validate') or {}
        errors = hidden = 0
        for e in res.get('errors') or []:
            sev = e.get('severity') or 'error'
            if sev == 'info' and not verbose:
                hidden += 1
                continue
            where = e.get('source_file') or e.get('file_path') or f'(no file; model {e.get("model_id") or "?"})'
            if e.get('line_number') or e.get('line'):
                where += f':{e.get("line_number") or e.get("line")}'
            out(f'  {sev:7} {where}  {e.get("message")}')
            if sev in ('error', 'fatal'):
                errors += 1
        out((f'validation: {errors} error(s)' if errors else 'validation: no errors')
            + (f' ({hidden} info line(s) hidden; --verbose shows them)' if hidden else ''))
        return errors

    def uncommitted(self):
        return [(f['path'], f['git_status'].get('text') or f['git_status'].get('action'))
                for f in self.file_list() if f.get('git_status')]


def open_session(proj, args, check_branch=True, fetch=True, phases=None):
    """The Looker session every command starts with; in a linked folder, the commit check too.
    fetch=False lets the commit check skip `git fetch` when it is not needed (push)."""
    phases = phases or Phases()
    cfg = proj.require_config()
    with phases('session'):
        sess = Session(proj, args.env_file, cfg['mode'], cfg['project'], cfg.get('branch'), check_branch)
    if cfg.get('git_remote'):
        with phases('commit check'):
            commit_check(proj, sess, cfg['git_remote'], fetch=fetch)
    return sess


class Phases:
    """Wall time of each step of a command; printed on stderr only when verbose."""

    def __init__(self, verbose=False):
        self.verbose, self.started = verbose, time.perf_counter()

    @contextlib.contextmanager
    def __call__(self, label, detail=''):
        s = time.perf_counter()
        try:
            yield
        finally:
            self.note(label, time.perf_counter() - s, detail)

    def note(self, label, seconds, detail=''):
        if self.verbose:
            print(f'timing: {label:<14} {seconds:7.3f}s{"  " + detail if detail else ""}',
                  file=sys.stderr, flush=True)

    def total(self):
        self.note('total', time.perf_counter() - self.started)


# ---------------------------------------------------------------- sync plan

KIND_TEXT = {
    'local-new': 'only local (push creates it)',
    'local-modified': 'changed locally (push writes it)',
    'local-deleted': 'deleted locally (push --delete removes it from Looker)',
    'remote-new': 'only in Looker (pull writes it; push --delete removes it)',
    'remote-modified': 'changed in Looker since last sync (pull writes it)',
    'remote-deleted': 'deleted in Looker since last sync (pull keeps the local copy)',
    'conflict': 'changed on both sides',
}
REMOTE_CHANGED = ('remote-modified', 'remote-deleted', 'conflict')


def plan(local, remote, known):
    """{path: kind} for every path that differs. known = {path: sha at last sync}."""
    return plan_hashes({p: sha(d) for p, d in local.items()}, {p: sha(d) for p, d in remote.items()}, known)


def plan_hashes(local, remote, known):
    """plan() on hashes: local and remote are {path: sha}; a path that is absent does not exist."""
    kinds = {}
    for path in sorted(set(local) | set(remote) | set(known)):
        lh, rh, s = local.get(path), remote.get(path), known.get(path)
        if lh == rh:
            continue
        lc, rc = lh != s, rh != s
        if s is None:
            kinds[path] = 'remote-new' if lh is None else 'local-new' if rh is None else 'conflict'
        elif lc and rc:
            kinds[path] = 'conflict'
        elif lc:
            kinds[path] = 'local-deleted' if lh is None else 'local-modified'
        else:
            kinds[path] = 'remote-deleted' if rh is None else 'remote-modified'
    return kinds


def show(kinds, header):
    if not kinds:
        out(f'{header}: nothing differs')
        return
    out(f'{header}:')
    for kind in KIND_TEXT:
        paths = [p for p, k in kinds.items() if k == kind]
        if paths:
            out(f'  {KIND_TEXT[kind]}:')
            for p in paths:
                out(f'    {p}')


def record(proj, sess, updates, drops=()):
    files = proj.state.setdefault('files', {})
    files.update(updates)
    for p in drops:
        files.pop(p, None)
    proj.state.update({'mode': sess.mode, 'branch': sess.branch if sess.mode == 'dev' else None,
                       'production_branch': sess.branch if sess.mode == 'prod' else None,
                       'user': sess.user,
                       'synced_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')})
    proj.save_state()


def in_sync(local, remote):
    return {p: sha(d) for p, d in remote.items() if local.get(p) == d}


# ---------------------------------------------------------------- watch core
#
# The local half of `watch`: decides WHAT to push and WHEN, never talks to Looker itself.
# Level-triggered: every tick compares a fresh stat scan (desired state) with the last-known
# Looker state ({path: sha}), so a lost, merged or late event can never lose a change. One
# batch slot, never a queue: the scanner keeps ticking while the worker pushes, and after a
# stall only the latest bytes are pushed. A synced file that vanishes while watch runs is
# deleted in Looker after DELETE_GRACE, unless git moved it (see _plan_deletes). With
# --no-delete (delete_one is None) a missing file is only reported.

GIT_BUSY = ('index.lock', 'rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD',
            'REVERT_HEAD', 'BISECT_LOG')


def find_git_dir(root):
    """The git dir of the clone holding root (following a `.git` file of a worktree), or None.
    Pure Python: spawns no git."""
    d = os.path.abspath(root)
    while True:
        dot = os.path.join(d, '.git')
        if os.path.isdir(dot):
            return dot
        if os.path.isfile(dot):
            try:
                with open(dot) as f:
                    line = f.readline().strip()
            except OSError:
                return None
            if line.startswith('gitdir:'):
                return os.path.normpath(os.path.join(d, line[7:].strip()))
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _gate_default():
    try:
        from looker_sync import gate
        return gate.check
    except Exception:  # noqa: BLE001  gate missing or broken: watch still runs, ungated
        return lambda path, data: None


def _skey(e):
    return (e.mtime_ns, e.size, e.ino, e.dev)


def _under_any(paths, folder):
    """True when any of paths lies inside folder."""
    prefix = folder + '/'
    return any(p.startswith(prefix) for p in paths)


def _under(path, folders):
    return any(d == '' or path.startswith(d + '/') for d in folders)


COARSE_TICK_NS = 2_000_000_000


def mtime_tick(meta_dir, budget=0.1):
    """The mtime step of the volume holding meta_dir, in ns: keep appending to a probe file
    in meta_dir until fstat's mtime has changed twice and return the smallest step seen.
    No change within `budget` seconds (or no probe possible): 2 s, the coarsest tick
    (FAT). Only meta_dir is written; the probe is always removed."""
    path = os.path.join(meta_dir, f'.mtime-probe-{os.getpid()}')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError:
        return COARSE_TICK_NS
    try:
        seen, end = [], time.monotonic() + budget
        while len(seen) < 3 and time.monotonic() < end:
            os.write(fd, b'.')
            m = os.fstat(fd).st_mtime_ns
            if not seen or m != seen[-1]:
                seen.append(m)
        if len(seen) < 3:
            return COARSE_TICK_NS
        return min(b - a for a, b in zip(seen, seen[1:]))
    except OSError:
        return COARSE_TICK_NS
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass


class _Track:
    """A path whose stat changed and is waiting to settle."""
    __slots__ = ('first', 'last', 'settle')

    def __init__(self, now, settle):
        self.first, self.last, self.settle = now, now, settle


def git_common_dir(git_dir):
    """Where a clone keeps its refs: git_dir itself, or the main one for a linked worktree."""
    if not git_dir:
        return None
    try:
        with open(os.path.join(git_dir, 'commondir')) as f:
            return os.path.normpath(os.path.join(git_dir, f.read().strip()))
    except OSError:
        return git_dir


class _Delete:
    """A batch value meaning: delete this path. On a rename, names the new path to check first."""
    __slots__ = ('new_path', 'new_sha')

    def __init__(self, new_path=None, new_sha=None):
        self.new_path, self.new_sha = new_path, new_sha


class _DeleteFolder:
    """A batch value meaning: remove this vanished folder from Looker. Its key ends in '/'."""
    __slots__ = ()


class WatchCore:
    """Scanner plus one-slot worker for `watch`. No network: the caller supplies

    push_one(rel, data)  push one file; return None once Looker acknowledged the write, or a
                         string to hold the file with that reason (for example "changed in
                         Looker since last sync") until its local bytes change; raise to
                         report an error (the file stays pending, retried with backoff)
    remote_state()       {rel: sha} of Looker's files as last known (state.json by default)
    on_event(kind, text, paths)
                         kind: paused | resumed | held | released | pushed | error | notice;
                         emitted on change only, never repeated every tick
    gate(rel, data)      None if the bytes may be pushed, else a reason (default: gate.check)
    delete_one(rel, new_path, new_sha)
                         delete one file; return None once Looker no longer has it, or a string
                         to hold the delete with that reason until the local file comes back;
                         raise to retry with backoff. new_path and new_sha name a renamed file's
                         new home, to confirm first. None (the default) means --no-delete: a
                         vanished file is only reported
    hold_check()         None, or a reason to hold every push this tick (branch mismatch...)
    on_change(rel)       called when a path starts settling, so the caller can read ahead

    tick() runs one reconcile and is what tests call with a fake clock. start()/stop() run the
    scanner and the worker in threads. pause(reason)/resume() pause pushing from outside.
    """

    SETTLE_IN_PLACE = 0.15
    SETTLE_BURST = 0.5
    BURST_PATHS = 5
    REPEAT_WINDOW = 1.0
    MAX_WAIT = 3.0
    GIT_QUIET = 0.3
    CEILING = 200
    DELETE_GRACE = 1.5      # seconds a path must stay absent: outlasts an editor's atomic save
    BATCH_WORKERS = 8       # files of one batch sent at once

    def __init__(self, proj, push_one, remote_state=None, on_event=None, gate=None,
                 hold_check=None, poll=0.05, clock=time.monotonic, wall_ns=time.time_ns,
                 threaded=True, ceiling=None, on_change=None, delete_one=None, delete_folder=None):
        self.proj, self.push_one = proj, push_one
        self.delete_one = delete_one
        self.delete_folder = delete_folder
        self.dir_watch = {}     # folder that lost a synced file this run -> clock first seen absent
        self.dir_left = set()   # folders never to clean up, unless they reappear locally
        # A scan tests every path 20 times a second. The answer depends only on the path and
        # the config, which a running watch never reloads, so it is worked out once per path.
        proj.synced = functools.lru_cache(maxsize=65536)(proj.synced)
        self.on_change = on_change   # on_change(rel) when a path starts settling (watch prefetches)
        self.on_event = on_event or (lambda kind, text, paths=(): None)
        self.gate = gate if gate is not None else _gate_default()
        self.hold_check = hold_check or (lambda: None)
        self.poll, self.clock, self.wall_ns, self.threaded = poll, clock, wall_ns, threaded
        self.ceiling = self.CEILING if ceiling is None else ceiling
        self.interval, self.scan_duration = poll, 0.0
        self.lock = threading.RLock()
        self.synced = dict(remote_state() if remote_state else proj.state.get('files', {}))
        self.cache = {}         # rel -> (stat key, sha, data or None, read wall ns)
        self.tracks = {}        # rel -> _Track
        self.last_change = {}   # rel -> clock of its last stat change
        self.recheck = set()    # racy mtimes: read again at the next scan
        self.gone = {}          # rel -> (ino, dev) of a local file seen and then vanished
        self.gone_at = {}       # rel -> clock when it vanished; starts the delete grace period
        self.git_gone = set()   # vanished while git was active: never deleted by this run
        self.held_delete = {}   # rel -> reason returned by delete_one
        self.git_moved_at = float('-inf')
        self.moved_to = {}      # old rel -> new rel of a rename, while the old path still exists
        self.held_remote = {}   # rel -> (sha, reason) returned by push_one
        self.gate_cache = {}    # sha -> reason
        self.bad = {}           # rel -> read status of a file that cannot be read
        try:
            self.meta_dev = os.stat(proj.meta).st_dev
        except OSError:
            self.meta_dev = None
        self.mtime_tick = mtime_tick(proj.meta)   # ns, measured once on the meta dir's volume
        self._racy = {}         # dev -> racy-mtime window in ns, refreshed every scan
        self.cycle = poll       # measured seconds between tick starts (moving average)
        self.last_scan = None
        self.inflight = None
        self.external_pause = None
        self.retry_at, self.backoff = 0.0, 0.0
        self.git_dir = find_git_dir(proj.root)
        self.git_busy_paths = [(m, os.path.join(self.git_dir or '', m)) for m in GIT_BUSY]
        self.git_head = self._read_head()
        self.git_common = git_common_dir(self.git_dir)
        self.git_refs = self._git_refs() if delete_one else None
        self.git_busy_until = None
        self.root_id = self._root_id()
        self.paused = None
        self.holds = {}         # key -> (text, paths) currently reported
        self.last_tick = None
        self.offset = None      # wall minus monotonic, to spot a clock jump
        self.stop_event = threading.Event()
        self.cond = threading.Condition(self.lock)
        self.threads = []

    # ------------------------------------------------------------ outside controls

    def pause(self, reason):
        """Stop pushing (scanning goes on) until resume(); emits `paused` once."""
        with self.lock:
            self.external_pause = reason
        self._set_paused(reason)

    def resume(self):
        """Undo pause(); the next tick emits `resumed`."""
        with self.lock:
            self.external_pause = None

    def set_remote(self, rel, sha_value):
        """Tell the core what Looker now holds for rel (None: absent)."""
        with self.lock:
            if sha_value is None:
                self.synced.pop(rel, None)
            else:
                self.synced[rel] = sha_value

    def start(self):
        self.stop_event.clear()
        self.threads = [threading.Thread(target=self._scan_loop, daemon=True, name='watch-scan')]
        if self.threaded:
            self.threads.append(threading.Thread(target=self._work_loop, daemon=True,
                                                 name='watch-push'))
        for t in self.threads:
            t.start()

    def stop(self, timeout=5):
        self.stop_event.set()
        with self.cond:
            self.cond.notify_all()
        for t in self.threads:
            t.join(timeout)

    def _scan_loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001  a bad tick must not end the watch
                self.on_event('error', f'scan failed: {e}', ())
            self.stop_event.wait(self.interval)

    # ------------------------------------------------------------ helpers

    def _emit_hold(self, key, text, paths=()):
        paths = tuple(sorted(paths))
        if self.holds.get(key) != (text, paths):
            self.holds[key] = (text, paths)
            self.on_event('held', text, paths)

    def _clear_holds(self, keep):
        for key in [k for k in self.holds if k not in keep]:
            text, paths = self.holds.pop(key)
            self.on_event('released', text, paths)

    def _set_paused(self, reason):
        if self.paused != reason:
            self.paused = reason
            self.on_event('paused', reason, ())

    def _root_id(self):
        try:
            st = os.stat(self.proj.root)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def _root_ok(self):
        if self._root_id() != self.root_id or self.root_id is None:
            return f'the folder {self.proj.root} is gone or was replaced'
        if not os.path.isfile(self.proj.config_path):
            return f'{META_DIR}/config.json is missing from {self.proj.root}'
        return None

    def _read_head(self):
        if not self.git_dir:
            return None
        try:
            with open(os.path.join(self.git_dir, 'HEAD'), 'rb') as f:
                return f.read()
        except OSError:
            return None

    def _attach_git(self):
        """A `git init` after watch started: without this its checkouts would look like `rm`."""
        self.git_dir = find_git_dir(self.proj.root)
        if not self.git_dir:
            return False
        self.git_busy_paths = [(m, os.path.join(self.git_dir, m)) for m in GIT_BUSY]
        self.git_head = self._read_head()
        self.git_common = git_common_dir(self.git_dir)
        self.git_refs = self._git_refs() if self.delete_one else None
        return True

    def _git_busy(self, now):
        """A reason while git is rewriting the clone, and for GIT_QUIET after it stops."""
        if not self.git_dir:
            if not self._attach_git():
                return None
            self.git_busy_until = now + self.GIT_QUIET
            return 'git appeared in this folder'
        busy = [m for m, path in self.git_busy_paths if os.path.lexists(path)]
        head = self._read_head()
        if head != self.git_head:
            self.git_head = head
            busy.append('HEAD moved')
        if busy:
            self.git_busy_until = now + self.GIT_QUIET
            return 'git is busy (' + ', '.join(busy) + ')'
        if self.git_busy_until is not None and now < self.git_busy_until:
            return 'git just finished; waiting for it to settle'
        self.git_busy_until = None
        return None

    def _git_refs(self):
        """A fingerprint of what git moves after rewriting the tree: HEAD, the branch it names,
        packed refs, ORIG_HEAD and the stash. A pull or reset stays on one branch, so HEAD's own
        text does not change, and a fast one can finish between two scans."""
        if not self.git_dir:
            return None
        head = self._read_head() or b''
        paths = [os.path.join(self.git_common, 'packed-refs'),
                 os.path.join(self.git_common, 'refs', 'stash'),
                 os.path.join(self.git_dir, 'ORIG_HEAD')]
        if head.startswith(b'ref:'):
            name = head[4:].decode('utf-8', 'replace').strip()
            paths.append(os.path.join(self.git_common, *name.split('/')))
        stats = []
        for p in paths:
            try:
                st = os.stat(p)
                stats.append((st.st_mtime_ns, st.st_size, st.st_ino))
            except OSError:
                stats.append(None)
        return head, tuple(stats)

    def _git_refs_moved(self):
        refs = self._git_refs()
        moved, self.git_refs = refs != self.git_refs, refs
        return moved

    def _git_locked(self):
        return bool(self.git_dir) and any(os.path.lexists(p) for _, p in self.git_busy_paths)

    def _forget_gone(self, rel):
        """rel came back, or Looker no longer has it: drop everything tracked for its delete."""
        self.gone.pop(rel, None)
        self.git_gone.discard(rel)
        self.moved_to.pop(rel, None)
        with self.lock:
            self.gone_at.pop(rel, None)
            self.held_delete.pop(rel, None)

    def _settle_for(self, rel, now, atomic, burst):
        prev = self.last_change.get(rel)
        if prev is not None and now - prev < self.REPEAT_WINDOW:
            return min(max(2.5 * (now - prev), 0.5), 3.0)
        if burst:
            return self.SETTLE_BURST
        return 0.0 if atomic else self.SETTLE_IN_PLACE

    def _changed(self, rel, now, atomic, burst):
        settle = self._settle_for(rel, now, atomic, burst)
        t = self.tracks.get(rel)
        if t is None:
            self.tracks[rel] = _Track(now, settle)
            if self.on_change:
                try:
                    self.on_change(rel)
                except Exception:  # noqa: BLE001  a hint only; never stops the scan
                    pass
        else:
            t.last, t.settle = now, settle
        self.last_change[rel] = now

    def _store(self, rel, entry, data, read_ns):
        h = sha(data)
        with self.lock:
            keep = data if h != self.synced.get(rel) else None
        self.cache[rel] = (_skey(entry), h, keep, read_ns)
        if entry.mtime_ns >= read_ns - self._racy_for(entry):
            self.recheck.add(rel)

    def _racy_for(self, entry):
        return self._racy.get(entry.dev) or self._window(self._base_tick(entry.dev))

    # ------------------------------------------------------------ one reconcile

    def tick(self):
        """Scan, settle, hash, decide; hand one batch to the worker when it is free.
        Returns the batch handed over this tick ({rel: data}) or None."""
        now = self.clock()
        self._watch_clock(now)
        reason = self._root_ok()
        if reason:
            self._set_paused(reason)
            return None
        t0 = self.clock()
        scan = self.proj.scan(previous=self.last_scan)
        self.scan_duration = self.clock() - t0
        self.interval = max(self.poll, 9 * self.scan_duration)
        reason = self._root_ok() or ('the folder cannot be listed' if '' in scan.unreadable
                                     else None)
        if reason:
            self._set_paused(reason)
            return None
        prev = self.last_scan.entries if self.last_scan else {}
        self.last_scan = scan
        if self.external_pause:
            self._set_paused(self.external_pause)
        elif self.paused:
            self.paused = None
            self.on_event('resumed', 'watching again', ())
        if scan.entries != prev or not self._racy:   # a pure function of the entries
            self._racy = self._racy_windows(scan)
        self._detect(scan, now, prev)
        return self._decide(scan, now)

    def _watch_clock(self, now):
        """Stall and clock-jump notices; keeps `cycle`, the real time between tick starts
        (a 50 ms sleep can take about 118 ms on a laptop on battery)."""
        if self.last_tick is not None:
            gap = now - self.last_tick
            if gap > max(2.0, 10 * self.cycle):
                self.on_event('notice', f'no scan for {gap:.1f} s (stalled or '
                                        'asleep); catching up with the latest state', ())
            elif gap >= 0:              # clamped: one slow tick must not double the average
                self.cycle = 0.8 * self.cycle + 0.2 * min(gap, 3 * self.cycle)
        self.last_tick = now
        offset = self.wall_ns() / 1e9 - now
        if self.offset is not None and abs(offset - self.offset) > 1.0:
            self.on_event('notice', f'system clock jumped by {offset - self.offset:+.1f} s', ())
        self.offset = offset

    DEFAULT_TICK_NS = 10_000_000    # a volume other than the meta dir's: Linux steps 1-4 ms

    def _base_tick(self, dev):
        return self.mtime_tick if dev == self.meta_dev else self.DEFAULT_TICK_NS

    @staticmethod
    def _window(tick):
        """A write within this many ns of a read may share the read's mtime tick."""
        return max(2 * tick, 2_000_000)

    def _racy_windows(self, scan):
        """dev -> window. The tick is the measured one, raised to 2 s or 1 s when every
        sampled mtime on that volume is a multiple of it (HFS+, FAT, tar-extracted files)."""
        sample = {}
        for e in scan.entries.values():
            ms = sample.setdefault(e.dev, [])
            if len(ms) < 50:
                ms.append(e.mtime_ns)
        out = {}
        for dev, ms in sample.items():
            tick = self._base_tick(dev)
            for coarse in (2_000_000_000, 1_000_000_000):
                if all(m % coarse == 0 for m in ms):
                    tick = max(tick, coarse)
                    break
            out[dev] = self._window(tick)
        return out

    def _detect(self, scan, now, prev):
        """Turn stat changes into settle timers; read and hash every file that has settled."""
        entries, wall = scan.entries, self.wall_ns()
        changed = []
        for rel, e in entries.items():
            p = prev.get(rel)
            if p is not None and _skey(p) == _skey(e):
                continue
            c = self.cache.get(rel)
            if p is None and c is not None and c[0] == _skey(e):
                continue                            # carried back from an unreadable folder
            if p is None and (wall - e.mtime_ns) / 1e9 >= self.MAX_WAIT:
                self.tracks[rel] = _Track(now, 0.0)  # an old file seen for the first time
                continue
            atomic = p is not None and (p.ino, p.dev) != (e.ino, e.dev)
            changed.append((rel, atomic))
        burst = len(changed) >= self.BURST_PATHS
        for rel, atomic in changed:
            self._changed(rel, now, atomic, burst)
        # racy mtimes: a same-size write in the same mtime tick is invisible to stat
        for rel in sorted(self.recheck):
            self.recheck.discard(rel)
            e, c = entries.get(rel), self.cache.get(rel)
            if e is None or c is None or rel in self.tracks or c[0] != _skey(e):
                continue
            read_ns = self.wall_ns()
            r = self.proj.read_entry(rel)
            if r.status == 'ok' and _skey(r.entry) == c[0] and sha(r.data) == c[1]:
                if e.mtime_ns >= read_ns - self._racy_for(e):
                    self.recheck.add(rel)
                continue
            self._changed(rel, now, False, False)
        for rel, e in entries.items():
            if rel not in self.tracks and e.mtime_ns >= scan.started_ns - self._racy_for(e):
                self.recheck.add(rel)
        # vanished local files: forget them; keep a synced one's inode for move detection,
        # forget a never-synced one entirely (editor temp names would pile up forever)
        with self.lock:
            synced = set(self.synced)
        for rel, p in prev.items():
            if rel not in entries and not _under(rel, scan.unreadable):
                if rel in synced:
                    self.gone[rel] = (p.ino, p.dev)
                    with self.lock:
                        self.gone_at[rel] = now
                else:
                    self.last_change.pop(rel, None)
                self.cache.pop(rel, None)
                self.tracks.pop(rel, None)
                self.bad.pop(rel, None)
        for rel in [r for r in self.gone if r in entries]:
            self._forget_gone(rel)
        # settled tracks: one verified read each
        for rel in sorted(self.tracks):
            t, e = self.tracks[rel], entries.get(rel)
            if e is None:
                del self.tracks[rel]
                continue
            forced = now >= t.first + self.MAX_WAIT
            if now < t.last + t.settle and not forced:
                continue
            read_ns = self.wall_ns()
            r = self.proj.read_entry(rel)
            if r.status == 'ok' and (_skey(r.entry) == _skey(e) or forced):
                self._store(rel, r.entry, r.data, read_ns)
                self.bad.pop(rel, None)
                del self.tracks[rel]
            elif r.status in ('ok', 'unstable', 'vanished'):
                t.last = now                        # still being written, or listed but
                                                    # mid-rename: wait; a scan that no
                                                    # longer lists it drops the track
            else:
                del self.tracks[rel]
                self.cache.pop(rel, None)
                self.bad[rel] = r.status

    def _decide(self, scan, now):
        """Desired (disk) versus last-known Looker state: holds, then at most one batch."""
        entries = scan.entries
        with self.lock:
            synced = dict(self.synced)
            held_remote = dict(self.held_remote)
            held_delete = dict(self.held_delete)
            busy = self.inflight is not None
        keep = set()
        # after the scan, every tick: a path the scan saw vanish must meet this tick's git state
        git_reason = self._git_busy(now)
        git_now = bool(git_reason)
        if self.delete_one and self._git_refs_moved():
            self.git_moved_at = now
        if now - self.git_moved_at < self.DELETE_GRACE:     # git stash moves its ref, then the files
            git_now = True
        for rel in [r for r in self.gone if r not in synced]:
            self._forget_gone(rel)
        stuck = sorted(r for r, t in self.tracks.items()
                       if r in entries and now >= t.first + self.MAX_WAIT)
        if stuck:
            keep.add('busy')
            self._emit_hold('busy', f'{len(stuck)} file(s) still being written after '
                            f'{self.MAX_WAIT:.0f} s; pushing once they hold still', stuck)
        bad = sorted(f'{r} ({s})' for r, s in self.bad.items() if r in entries)
        if bad:
            keep.add('unreadable')
            self._emit_hold('unreadable', f'{len(bad)} file(s) cannot be read', bad)
        missing = sorted(p for p in synced if p not in entries
                         and not _under(p, scan.unreadable))
        miss_by_sha = {synced[m]: m for m in missing}
        miss_names = {m.rsplit('/', 1)[-1]: m for m in missing}
        inode_gone = {self.gone[m]: m for m in missing if m in self.gone}
        deleting = self.delete_one is not None
        live_sha = {}
        for rel, c in self.cache.items():
            if c[1] == synced.get(rel) and rel in entries:
                live_sha.setdefault(c[1], rel)
                if c[2] is not None:
                    self.cache[rel] = (c[0], c[1], None, c[3])   # in Looker: drop the bytes
        pending, moves, suspects, remote, broken = {}, [], [], [], []
        for rel in sorted(entries):
            c = self.cache.get(rel)
            if c is None or c[1] == synced.get(rel):
                continue
            if rel in self.tracks:                  # settling: a broken last read still holds
                if self.gate_cache.get(c[1]):
                    broken.append(f'{rel}: {self.gate_cache[c[1]]}')
                continue
            hr = held_remote.get(rel)
            if hr and hr[0] == c[1]:
                remote.append(f'{rel}: {hr[1]}')
                continue
            if deleting:                            # a rename, maybe over a synced file: old path later
                e = entries[rel]
                src = inode_gone.get((e.ino, e.dev)) or miss_by_sha.get(c[1])
                if src:
                    self.moved_to.setdefault(src, rel)
            elif rel not in synced:
                e = entries[rel]
                src = miss_by_sha.get(c[1]) or inode_gone.get((e.ino, e.dev))
                if src:
                    moves.append(f'{src} -> {rel}')
                    continue
                if live_sha.get(c[1]) or rel.rsplit('/', 1)[-1] in miss_names:
                    suspects.append(rel)
                    continue
            if c[2] is None:                        # bytes not kept: read again
                self.tracks[rel] = _Track(now, 0.0)
                continue
            pending[rel] = c[2]
        if remote:
            keep.add('remote')
            self._emit_hold('remote', f'{len(remote)} file(s) held by Looker', remote)
        if moves:
            keep.add('moves')
            self._emit_hold('moves', f'{len(moves)} moved file(s) not synced (watch never '
                            'deletes); run `looker-sync push --delete` to apply the move',
                            moves)
        if suspects:
            keep.add('suspects')
            self._emit_hold('suspects', f'{len(suspects)} new file(s) look like a copy or a '
                            'move of another file; edit them or run `looker-sync push`',
                            suspects)
        due, delete_count = {}, 0
        if deleting:
            due, delete_count = self._plan_deletes(scan, missing, synced, held_remote, held_delete,
                                                   now, keep, git_now)
            if not scan.unreadable:
                folders = self._plan_folders(missing, synced, now, git_now)
                due.update(folders)
                delete_count += len(folders)
        else:
            moved_from = set(m.split(' -> ')[0] for m in moves)
            deleted = [m for m in missing if m not in moved_from]
            if deleted:
                keep.add('deleted')
                self._emit_hold('deleted', f'{len(deleted)} file(s) deleted locally; watch never '
                                'deletes, run `looker-sync push --delete`', deleted)
        batch = self._gatekeep(pending, now, keep, broken, git_reason, due, delete_count)
        self._clear_holds(keep)
        if batch is None or busy:
            return None
        with self.lock:
            if self.inflight is not None:
                return None
            self.inflight = batch
        if self.threaded:
            with self.cond:
                self.cond.notify_all()
        else:
            self._push_batch(batch)
        return batch

    def _plan_deletes(self, scan, missing, synced, held_remote, held_delete, now, keep, git_now):
        """The deletes to send this tick ({rel: _Delete}) and how many are on their way, due or
        still in the grace period (the ceiling counts them). A path qualifies only when this
        run saw it vanish, git was quiet meanwhile, it has stayed absent for DELETE_GRACE, and
        for a rename Looker has accepted the new path. Each hold here is reported, never lost."""
        entries = scan.entries
        if git_now:     # the grace period doubles as the window that ties a vanish to git
            for rel in missing:
                if rel in self.gone and now - self.gone_at.get(rel, now) < self.DELETE_GRACE:
                    self.git_gone.add(rel)
        by_git = [r for r in missing if r in self.git_gone]
        if by_git:
            keep.add('git-gone')
            self._emit_hold('git-gone', f'{len(by_git)} file(s) vanished during a git operation; '
                            'not deleted in Looker, run `looker-sync push --delete` if you meant '
                            'that', by_git)
        stale = [r for r in missing if r not in self.gone]
        if stale:
            keep.add('deleted')
            self._emit_hold('deleted', f'{len(stale)} file(s) already missing when watch started; '
                            'not deleted, run `looker-sync push --delete`', stale)
        held = [f'{r}: {held_delete[r]}' for r in missing if r in held_delete]
        if held:
            keep.add('delete-held')
            self._emit_hold('delete-held', f'{len(held)} local delete(s) held', held)
        candidates = [r for r in missing if r in self.gone and r not in self.git_gone
                      and r not in held_delete]
        if scan.unreadable and candidates:
            keep.add('delete-unreadable')
            self._emit_hold('delete-unreadable', f'{len(candidates)} local delete(s) held while a '
                            'folder cannot be read', candidates)
            return {}, 0
        # a rename's new path may still be settling: do not mistake it for a delete
        new_settling = any(r in entries for r in self.tracks)    # the busy hold reports a stuck one
        due, parked = {}, []
        for rel in candidates:
            if now - self.gone_at.get(rel, now) < self.DELETE_GRACE:
                continue
            to = self.moved_to.get(rel)
            if to is not None and to not in entries:
                del self.moved_to[rel]      # the new path went too: a plain delete now
                to = None
            if to is None:
                if not new_settling:
                    due[rel] = _Delete()
                continue
            c = self.cache.get(to)
            if c is not None and to not in self.tracks and synced.get(to) == c[1]:
                due[rel] = _Delete(to, c[1])
            elif to in held_remote:
                parked.append(f'{rel} -> {to}')
        if parked:
            keep.add('move-wait')
            self._emit_hold('move-wait', f'{len(parked)} old path(s) kept in Looker because the '
                            'new path is held', parked)
        return due, len(candidates)

    def _plan_folders(self, missing, synced, now, git_now):
        """Vanished folders to remove from Looker ({'dir/': _DeleteFolder}), topmost only. A folder
        qualifies when a synced file under it vanished this run, it has stayed absent for
        DELETE_GRACE with git quiet, and no synced file remains under it: a held, failed or
        conflicted file stays in synced, so it blocks. Nothing here looks at Looker."""
        if self.delete_folder is None:
            return {}
        with self.lock:
            for rel in missing:
                if rel in self.gone and rel not in self.git_gone:
                    parts = rel.split('/')[:-1]
                    for i in range(1, len(parts) + 1):
                        self.dir_watch.setdefault('/'.join(parts[:i]), None)
            self.dir_left = {d for d in self.dir_left if self._absent(d)}   # back: may qualify again
            chosen = []
            for d in sorted(self.dir_watch):
                if not self._absent(d):
                    self.dir_watch[d] = None        # still here, or back: a full grace again
                    continue
                if self.dir_watch[d] is None:
                    if git_now:                     # vanished with git active: never cleaned up
                        self.dir_left.add(d)
                        continue
                    self.dir_watch[d] = now
                if git_now or now - self.dir_watch[d] < self.DELETE_GRACE:
                    continue
                if (d in self.dir_left or _under(d, self.dir_left)
                        or not self.proj.synced(d + '/x.lkml')   # outside the sync set
                        or _under_any(synced, d) or _under(d, chosen)):
                    continue
                chosen.append(d)
        return {d + '/': _DeleteFolder() for d in chosen}

    def _gatekeep(self, pending, now, keep, broken=(), git_reason=None, deletes=None,
                  delete_count=0):
        """The batch to push now, or None. Every reason to hold applies to the whole batch,
        deletes included: delete_count keeps a delete-only tick from slipping past them."""
        if git_reason:                        # checked every tick, so a moved HEAD is seen at once
            keep.add('git')
            self._emit_hold('git', git_reason, ())
            return None
        if not pending and not delete_count:
            return None
        if self.external_pause:
            return None
        reason = self.hold_check()
        if reason:
            keep.add('external')
            self._emit_hold('external', reason, ())
            return None
        total = len(pending) + delete_count
        if total > self.ceiling:
            keep.add('ceiling')
            hint = ('run `looker-sync push --delete` to apply them, then restart watch'
                    if delete_count else 'run `looker-sync push` to send them')
            self._emit_hold('ceiling', f'{total} files changed (more than {self.ceiling}); {hint}', ())
            return None
        bad = list(broken)
        for rel, data in pending.items():
            h = sha(data)
            if h not in self.gate_cache:
                try:
                    self.gate_cache[h] = self.gate(rel, data)
                except Exception as e:  # noqa: BLE001  a crashing gate holds, never passes
                    self.gate_cache[h] = f'syntax check failed: {e}'
            if self.gate_cache[h]:
                bad.append(f'{rel}: {self.gate_cache[h]}')
        if bad:
            keep.add('gate')
            self._emit_hold('gate', f'syntax error; holding all {total} pending '
                            'file(s) until it is fixed', bad)
            return None
        if now < self.retry_at:
            return None
        return {**pending, **(deletes or {})} or None

    # ------------------------------------------------------------ worker

    def _work_loop(self):
        while True:
            with self.cond:
                while self.inflight is None and not self.stop_event.is_set():
                    self.cond.wait(0.5)
                if self.stop_event.is_set():
                    return
                batch = self.inflight
            self._push_batch(batch)

    def _push_batch(self, batch):
        """New files first (an include may need them), then changed ones, then deletes, then
        folders. Each phase runs at once on a bounded pool and the next waits for it, so a
        rename's old path can never overtake a write. A failure ends the batch."""
        try:
            with self.lock:
                new = sorted(p for p, v in batch.items()
                             if not isinstance(v, (_Delete, _DeleteFolder)) and p not in self.synced)
            changed = sorted(p for p, v in batch.items()
                             if not isinstance(v, (_Delete, _DeleteFolder)) and p not in new)
            deletes = sorted(p for p, v in batch.items() if isinstance(v, _Delete))
            folders = sorted(p for p, v in batch.items() if isinstance(v, _DeleteFolder))
            for paths, work in ((new, self._write_path), (changed, self._write_path),
                                (deletes, self._delete_path), (folders, self._delete_folder)):
                if not self._run_phase(paths, work, batch):
                    return
        finally:
            with self.lock:
                self.inflight = None

    def _run_phase(self, paths, work, batch):
        """Run work(rel, batch[rel]) for each path; False once any of them ended the batch."""
        stop = threading.Event()

        def one(rel):
            if not stop.is_set() and not work(rel, batch[rel]):
                stop.set()
        if not self.threaded or len(paths) < 2:
            for rel in paths:
                one(rel)
            return not stop.is_set()
        with concurrent.futures.ThreadPoolExecutor(min(self.BATCH_WORKERS, len(paths)),
                                                   thread_name_prefix='watch-send') as pool:
            for f in [pool.submit(one, rel) for rel in paths]:
                f.result()
        return not stop.is_set()

    def _write_path(self, rel, data):
        """One write from a batch. Returns False to end the batch."""
        try:
            result = self.push_one(rel, data)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.backoff = min(max(self.backoff * 2, 1.0), 30.0)
                self.retry_at = self.clock() + self.backoff
            self.on_event('error', f'{rel}: {e}; retrying in {self.backoff:.0f} s', (rel,))
            return False
        with self.lock:
            if isinstance(result, str):
                self.held_remote[rel] = (sha(data), result)
            else:
                self.synced[rel] = sha(data)
                self.held_remote.pop(rel, None)
                self.backoff = 0.0
        if not isinstance(result, str):
            self.on_event('pushed', rel, (rel,))
        return True

    def _delete_folder(self, key, _item):
        """One folder from a batch; every local guard is checked again just before the call.
        The outcome is final: a folder left or a failed call is reported once, never retried."""
        d = key[:-1]
        if self._root_ok() or self.external_pause or self._git_locked():
            return False
        with self.lock:
            if _under_any(self.synced, d):
                return True
        if not self._absent(d):
            return True
        try:
            left = self.delete_folder(d)
        except Exception as e:  # noqa: BLE001
            left = f'the call failed ({e})'
        with self.lock:
            self.dir_watch.pop(d, None)
            if left is None:
                for child in [c for c in self.dir_watch if c.startswith(key)]:
                    del self.dir_watch[child]
            else:
                self.dir_left.add(d)
        if left is not None:
            self.on_event('notice', f'folder {d}/ left in Looker: {left}', ())
        return True

    def _absent(self, rel):
        """True only when the path is certainly not on disk; any other answer keeps the file."""
        full = os.path.join(self.proj.root, *rel.split('/'))
        try:
            os.lstat(full)
        except (FileNotFoundError, NotADirectoryError):
            return True
        except OSError:
            return False
        try:        # a case-insensitive volume answers lstat for the old spelling of a renamed file
            return os.path.basename(full) not in os.listdir(os.path.dirname(full))
        except OSError:
            return False

    def _delete_path(self, rel, item):
        """One delete from a batch. Returns False to end the batch; the next tick decides again.
        Everything is checked once more here: the tick that planned this may be a moment old."""
        if self._root_ok() or self.external_pause or self._git_locked():
            return False
        if not self._absent(rel):
            with self.lock:
                if rel in self.gone_at:
                    self.gone_at[rel] = self.clock()     # back, or unsure: a full grace again
            return True
        try:
            result = self.delete_one(rel, item.new_path, item.new_sha)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.backoff = min(max(self.backoff * 2, 1.0), 30.0)
                self.retry_at = self.clock() + self.backoff
            self.on_event('error', f'{rel}: {e}; delete retrying in {self.backoff:.0f} s', (rel,))
            return False
        with self.lock:
            if isinstance(result, str):
                self.held_delete[rel] = result
            else:
                self.synced.pop(rel, None)
                self.backoff = 0.0
        if not isinstance(result, str):
            self.on_event('deleted', rel, (rel,))
        return True


# ---------------------------------------------------------------- watch network
#
# Wires WatchCore to Looker. One save: conflict read (started when the path first changed, so
# it overlaps the settle wait), the write, an `ack` line; then a read-back on another thread
# prints `verified` or MISMATCH, and only verified bytes reach state.json. A background loop
# confirms dev mode and the branch on its own connection; a mismatch pauses the core.

GATE_HINT = '(fix it, or run `looker-sync push` to send it anyway)'


class WatchNet:
    BRANCH_EVERY = 5.0       # seconds between background branch checks
    BRANCH_STALE = 30.0      # hold every write when the branch is unconfirmed this long
    QUIET = 2.0              # --validate: seconds without a write before validating
    PREFETCH_MAX_AGE = 10.0  # an older read-ahead is read again

    def __init__(self, proj, sess, force=False, validate=False, verbose=False, poll=0.05,
                 write_line=None, delete=True, **core_kw):
        if sess.mode != 'dev':
            raise Refused('watch is refused in prod mode; switch --mode dev first')
        self.proj, self.sess, self.L = proj, sess, sess.L
        self.branch = proj.require_config().get('branch')
        self.force, self.validate, self.verbose = force, validate, verbose
        self.delete = delete
        self.write_line = write_line or (lambda s: print(s, flush=True))
        self.lock = threading.RLock()
        self.print_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.known = dict(proj.state.get('files', {}))   # rel -> sha Looker holds as far as we know
        self.gen = {}            # rel -> count of acked writes
        self.mismatch = {}       # rel -> sha Looker kept after a read-back mismatch
        self.verified = {}       # rel -> sha read back from Looker since its last write
        self.dropped = set()     # rel deleted in Looker: a read-back still queued must not record it
        self.prefetch = {}       # rel -> (monotonic start, gen at start, Future)
        self.reasons = {}        # pause key -> text
        self.writes_since_check = 0
        self.checked_at = time.monotonic()   # open_session just confirmed the branch
        self.attempted_at = self.checked_at  # last check started, whatever its outcome
        self.check_notice = None             # last failure notice, printed once until it changes
        self.last_write, self.to_validate = 0.0, False
        self.validations = 0
        self.stop_event = threading.Event()
        self.kick = threading.Event()
        self.verify_q = queue.Queue()
        self.reader = concurrent.futures.ThreadPoolExecutor(WatchCore.BATCH_WORKERS, thread_name_prefix='watch-read')
        self.threads = []
        self.core = WatchCore(proj, self.push_one, remote_state=lambda: dict(self.known),
                              on_event=self.on_event, hold_check=self.hold_check,
                              on_change=self.on_change, poll=poll,
                              delete_one=self.delete_one if delete else None,
                              delete_folder=self.delete_folder if delete else None, **core_kw)
        self.L.before_token_swap = self._before_swap
        self.L.on_refresh_error = lambda e: self.line('notice', f'token refresh failed ({e}); retrying')

    # ---- output

    def line(self, kind, text, more=()):
        """Print one event; its lines stay together however many threads are printing."""
        stamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
        with self.print_lock:
            for t in (text, *more):
                self.write_line(f'{stamp} {kind:<8} {t}')

    def on_event(self, kind, text, paths=()):
        if kind in ('pushed', 'deleted'):
            return                   # push_one and delete_one already printed their own line
        gate = kind == 'held' and text.startswith('syntax error')
        self.line(kind, text, [f'  {p} {GATE_HINT}' if gate else f'  {p}' for p in paths])

    # ---- pause reasons (branch, auth) on top of the core's own

    def _pause(self, key, text):
        with self.lock:
            self.reasons[key] = text
            self.prefetch.clear()    # a read-ahead from another branch or session proves nothing
            joined = '; '.join(self.reasons[k] for k in sorted(self.reasons))
        self.core.pause(joined)

    def _unpause(self, key):
        with self.lock:
            if self.reasons.pop(key, None) is None:
                return
            self.prefetch.clear()
            joined = '; '.join(self.reasons[k] for k in sorted(self.reasons))
        if joined:
            self.core.pause(joined)
        else:
            self.core.resume()

    # ---- Looker reads

    def _read_remote(self, rel, token=None):
        st, payload = self.L.try_call('GET', f'/projects/{self.sess.project}/file/content',
                                      {'file_path': rel}, raw=True, token=token)
        if st == 404:
            return None
        if st >= 400:
            raise LookerError(f'GET {rel} -> HTTP {st}: {payload}')
        return payload

    def on_change(self, rel):
        """Start the conflict read as soon as a path changes, in parallel with its settle wait."""
        if self.stop_event.is_set():
            return
        if rel not in self.core.cache and rel in self.known:
            return                   # the first scan meets every synced file: no read storm
        with self.lock:
            # always a new read: a new track means the remote may have moved since any older one
            try:
                fut = self.reader.submit(self._read_remote, rel)
            except RuntimeError:     # shutting down
                return
            self.prefetch[rel] = (time.monotonic(), self.gen.get(rel, 0), fut)

    def _conflict_read(self, rel):
        with self.lock:
            p = self.prefetch.pop(rel, None)
            fresh = p and p[1] == self.gen.get(rel, 0) and time.monotonic() - p[0] < self.PREFETCH_MAX_AGE
        if fresh:
            try:
                return p[2].result()
            except LookerError:
                pass                 # read it again below; a real failure raises there
        return self._read_remote(rel)

    # ---- the core's push_one

    def push_one(self, rel, data):
        """None once Looker acknowledged the write; a reason to hold; raises to retry."""
        if self.sess.mode != 'dev':
            raise Refused('watch never writes in prod mode')
        with self.lock:
            paused = '; '.join(self.reasons.values())
        if paused:
            raise LookerError(f'not written while paused: {paused}')
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            return 'not UTF-8 text, which Looker cannot store'
        del text
        try:
            remote = self._conflict_read(rel)
            h, rh = sha(data), sha(remote)
            with self.lock:
                known, kept = self.known.get(rel), self.mismatch.get(rel)
            if rh == h:              # Looker already holds these bytes
                with self.lock:
                    self.known[rel] = h
                    self.mismatch.pop(rel, None)
                    self.verified[rel] = h
                self.dropped.discard(rel)
                self._record(rel, h)
                self.line('verified', f'{rel} (already in Looker)')
                return None
            if rh != known and not self.force:
                if kept is not None and rh == kept:
                    return ('Looker kept different bytes than were sent (MISMATCH); check it in '
                            'the Looker IDE, then pull, or push --force')
                if remote is None:
                    return 'deleted in Looker since last sync; pull, or push --force to write it again'
                return 'changed in Looker since last sync; pull it, or push --force to overwrite it'
            self.sess.write(rel, data, exists=remote is not None)
        except AuthExpired as e:
            self._pause('auth', f'Looker refused the session ({e}); paused, logging in again '
                        f'every {self.BRANCH_EVERY:.0f} s')
            self.kick.set()
            raise
        self.dropped.discard(rel)
        with self.lock:
            g = self.gen[rel] = self.gen.get(rel, 0) + 1
            self.known[rel] = h
            self.verified.pop(rel, None)
            self.writes_since_check += 1
            self.last_write, self.to_validate = time.monotonic(), True
        self.line('ack', rel)
        self.verify_q.put((rel, h, g))
        return None

    def delete_one(self, rel, new_path=None, new_sha=None):
        """None once Looker no longer holds rel; a reason to hold the delete; raises to retry.
        On a rename the new path is read back first, so a failed write can never lose both."""
        if self.sess.mode != 'dev':
            raise Refused('watch never deletes in prod mode')
        with self.lock:
            paused = '; '.join(self.reasons.values())
        if paused:
            raise LookerError(f'not deleted while paused: {paused}')
        try:
            with self.lock:
                checked = new_path is None or self.verified.get(new_path) == new_sha
            if not checked:          # no read-back has finished yet: this one is the proof
                if sha(self._read_remote(new_path)) != new_sha:
                    return f'{new_path} is not in Looker as written, so the old path stays'
                with self.lock:
                    self.verified[new_path] = new_sha
            remote = self._read_remote(rel)
            if remote is None:
                self._drop(rel)
                self.line('deleted', f'{rel} (already gone from Looker)')
                return None
            with self.lock:
                known = self.known.get(rel)
            if sha(remote) != known and not self.force:
                return ('changed in Looker since last sync, so not deleted; pull it, or use '
                        '`watch --force` or `push --force --delete`')
            with self.lock:
                self.gen[rel] = self.gen.get(rel, 0) + 1     # cancels a read-back still queued
            self.sess.delete(rel)
            if self._read_remote(rel) is not None:
                raise LookerError(f'Looker accepted the delete of {rel} but still has the file')
        except AuthExpired as e:
            self._pause('auth', f'Looker refused the session ({e}); paused, logging in again '
                        f'every {self.BRANCH_EVERY:.0f} s')
            self.kick.set()
            raise
        self._drop(rel)
        with self.lock:
            self.writes_since_check += 1
            self.last_write, self.to_validate = time.monotonic(), True
        self.line('deleted', f'{rel} (moved to {new_path})' if new_path else rel)
        return None

    def delete_folder(self, d):
        """None once Looker no longer has folder d; otherwise why it was left. Looker's call is
        recursive, so a listing taken just before it must show no file at all under d."""
        if self.sess.mode != 'dev':
            raise Refused('watch never deletes in prod mode')
        with self.lock:
            paused = '; '.join(self.reasons.values())
        if paused:
            return f'not removed while paused: {paused}'
        prefix, base = d + '/', f'/projects/{self.sess.project}/directories'
        try:
            files = self.L.call('GET', f'/projects/{self.sess.project}/files')
            if not isinstance(files, list) or not all(
                    isinstance(f, dict) and isinstance(f.get('path'), str) for f in files):
                return 'Looker answered the file listing in an unexpected shape, so it was not removed'
            count = sum(f['path'].startswith(prefix) for f in files)
            if count:
                return f'Looker has {count} file(s) there'
            st, payload = self.L.try_call('DELETE', base, {'path': d})
            if st >= 400:
                raise LookerError(f'DELETE folder -> HTTP {st}: {payload}')
            still = [x.get('path') if isinstance(x, dict) else x for x in self.L.call('GET', base) or []]
        except AuthExpired as e:
            self._pause('auth', f'Looker refused the session ({e}); paused, logging in again '
                        f'every {self.BRANCH_EVERY:.0f} s')
            self.kick.set()
            raise
        if d in still:
            return 'Looker accepted the removal but still lists the folder'
        self.line('deleted', f'{d}/ (folder)')
        return None

    def _drop(self, rel):
        """Looker no longer has rel: out of state.json and out of what we know, as push does."""
        with self.state_lock:
            record(self.proj, self.sess, {}, drops=[rel])
            self.dropped.add(rel)
        with self.lock:
            self.gen[rel] = self.gen.get(rel, 0) + 1
            self.known.pop(rel, None)
            self.verified.pop(rel, None)
            self.mismatch.pop(rel, None)
            self.prefetch.pop(rel, None)

    def _record(self, rel, h):
        with self.state_lock:
            record(self.proj, self.sess, {rel: h})

    def _record_unless_dropped(self, rel, h):
        """Record a verified write, unless the file was deleted while its read-back ran."""
        with self.state_lock:
            if rel in self.dropped:
                return False
            record(self.proj, self.sess, {rel: h})
            return True

    # ---- read-back and --validate

    def _verify_loop(self):
        while not self.stop_event.is_set():
            try:
                rel, h, g = self.verify_q.get(timeout=0.1)
            except queue.Empty:
                self._maybe_validate()
                continue
            with self.lock:
                if self.gen.get(rel) != g:
                    continue         # a newer write of rel is queued; verify that one
            try:
                back = self._read_remote(rel)
            except LookerError as e:
                self.line('error', f'{rel}: read-back failed ({e}); not recorded as synced')
                continue
            bh = sha(back)
            with self.lock:
                current = self.gen.get(rel) == g
            if bh == h:
                with self.lock:
                    if current:
                        self.verified[rel] = h
                if current and self._record_unless_dropped(rel, h):
                    self.line('verified', rel)
            elif current:
                with self.lock:
                    self.known[rel] = (self.proj.state.get('files') or {}).get(rel)
                    self.mismatch[rel] = bh
                self.core.set_remote(rel, bh)   # local differs again: the next push holds it
                self.line('MISMATCH', f'{rel}: Looker holds different bytes than were sent')

    def _maybe_validate(self):
        with self.lock:
            due = (self.validate and self.to_validate and self.verify_q.empty()
                   and time.monotonic() - self.last_write >= self.QUIET)
            if due:
                self.to_validate = False
        if not due:
            return
        self.validations += 1
        try:
            with self.print_lock:
                self.sess.validate(verbose=self.verbose)
        except LookerError as e:
            self.line('error', f'validation failed: {e}')

    # ---- branch and dev mode

    def check_branch(self, token=None):
        """None when Looker is in dev mode on the configured branch, else why not."""
        st, s = self.L.try_call('GET', '/session', token=token)
        if st == 401:                # only with `token`: a refused new session
            raise AuthExpired('GET /session -> HTTP 401 on a new login')
        if st >= 400:
            raise LookerError(f'GET /session -> HTTP {st}: {s}')
        ws = (s or {}).get('workspace_id')
        if ws != 'dev':
            return f'the Looker session is in {ws!r}, not dev mode'
        st, b = self.L.try_call('GET', f'/projects/{self.sess.project}/git_branch', token=token)
        if st == 401:
            raise AuthExpired('GET git_branch -> HTTP 401 on a new login')
        if st >= 400:
            raise LookerError(f'GET git_branch -> HTTP {st}: {b}')
        name = (b or {}).get('name')
        if name != self.branch:
            return f"Looker's dev workspace is on branch {name!r}, not {self.branch!r}"
        if (b or {}).get('readonly'):
            return f'Looker reports branch {name!r} as read-only for you'
        return None

    def branch_tick(self):
        with self.lock:
            writes, self.writes_since_check = self.writes_since_check, 0
            self.attempted_at = time.monotonic()
        try:
            reason = self.check_branch()
        except AuthExpired as e:
            self._pause('auth', f'Looker refused the session ({e}); paused, logging in again '
                        f'every {self.BRANCH_EVERY:.0f} s')
            return
        except LookerError as e:
            with self.lock:
                self.writes_since_check += writes
                text = f'branch check failed ({e}); trying again'
                repeat, self.check_notice = text == self.check_notice, text
            if not repeat:
                self.line('notice', text)
            return
        with self.lock:
            self.check_notice = None
        self._unpause('auth')
        with self.lock:
            self.checked_at = time.monotonic()
            was = 'branch' in self.reasons
        if reason:
            if writes and not was:
                self.line('error', f'{writes} write(s) since the previous check may have landed on '
                          'that branch; check it in the Looker IDE')
            self._pause('branch', f'{reason}; switch it back in the Looker IDE (or run '
                        '`looker-sync switch`)')
        else:
            self._unpause('branch')

    def _branch_loop(self):
        while not self.stop_event.is_set():
            self.kick.wait(self.BRANCH_EVERY)
            self.kick.clear()
            if self.stop_event.is_set():
                return
            self.branch_tick()

    def hold_check(self):
        """Before every batch: kick a check when the last one is old; hold when it is stale."""
        now = time.monotonic()
        age = now - self.checked_at
        if now - self.attempted_at > self.BRANCH_EVERY:
            self.kick.set()
        if age > self.BRANCH_STALE:
            return f'branch not confirmed for {age:.0f} s (is Looker reachable?)'
        return None

    def _before_swap(self, token):
        """A token refresh: the new session must be in dev mode on the branch, else keep the old."""
        reason = self.check_branch(token=token)
        if reason:
            self._pause('branch', f'{reason} (seen on a token refresh)')
            raise LookerError(reason)

    # ---- life cycle

    def start(self):
        self.L.start_auto_refresh()
        self.threads = [threading.Thread(target=self._verify_loop, daemon=True, name='watch-verify')
                        for _ in range(WatchCore.BATCH_WORKERS)]
        self.threads.append(threading.Thread(target=self._branch_loop, daemon=True, name='watch-branch'))
        for t in self.threads:
            t.start()
        self.core.start()

    def close(self):
        self.core.stop()
        self.stop_event.set()
        self.kick.set()
        for t in self.threads:
            t.join(5)
        self.reader.shutdown(wait=True, cancel_futures=True)
        self.L.close()

    def run(self):
        """Watch until Ctrl-C; returns OK."""
        self.line('notice', f'watching {self.proj.root} -> branch {self.branch}'
                  f'{" (--force: overwrites Looker edits)" if self.force else ""}'
                  f'{" (--no-delete: local deletes are only reported)" if not self.delete else ""}'
                  '; Ctrl-C stops')
        self.start()
        try:
            while not self.stop_event.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.close()
            self.line('notice', 'stopped')
        return OK


def cmd_watch(proj, args):
    cfg = proj.require_config()
    if cfg.get('mode') != 'dev':
        raise Refused('watch is refused in prod mode; switch --mode dev first')
    if args.poll < 10:
        raise Refused('--poll is in milliseconds and must be at least 10')
    sess = open_session(proj, args, fetch=False)
    drift_guard(proj, sess, 'watch', refuse=True)
    if (getattr(sess, 'branch_info', None) or {}).get('readonly'):
        raise Refused(f'Looker reports branch {sess.branch!r} as read-only for you; '
                      'switch to a branch of your own to watch')
    w = WatchNet(proj, sess, force=args.force, validate=args.validate, verbose=args.verbose,
                 poll=args.poll / 1000.0, delete=not args.no_delete)
    return w.run()


# ---------------------------------------------------------------- commands

def case_collisions(paths):
    """Groups of paths that differ only by case: one file on a case-insensitive volume."""
    groups = {}
    for p in paths:
        groups.setdefault(p.casefold(), []).append(p)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def do_pull(proj, sess, force=False, dry_run=False):
    local, remote = proj.local_files(), sess.remote_files()
    clashes = case_collisions(remote)
    if clashes:                         # refused on every OS: the clone may move to a Mac
        raise Refused('Looker has files whose paths differ only by case, which a Mac or '
                      'Windows folder cannot hold apart: '
                      + '; '.join(' and '.join(g) for g in sorted(clashes))
                      + '. Rename one of each in Looker, then pull again. Nothing written.')
    kinds = plan(local, remote, proj.state.get('files', {}))
    blocked = [p for p, k in kinds.items() if k == 'conflict' and p in remote]
    writes = [p for p, k in kinds.items() if k in ('remote-new', 'remote-modified')]
    show(kinds, 'pull plan')
    if blocked and not force:
        raise Refused('pull would overwrite unpushed local changes in: ' + ', '.join(blocked)
                      + '. Push them, back them up, or pull --force to take Looker\'s copy.')
    writes += blocked
    if dry_run:
        out(f'dry run: would write {len(writes)} file(s); nothing written')
        return OK
    for p in writes:
        proj.write_local(p, remote[p])
        out(f'  wrote {p}')
    updates = in_sync(local, remote)
    updates.update({p: sha(remote[p]) for p in writes})
    record(proj, sess, updates)
    out(f'pull: wrote {len(writes)} file(s)')
    return OK


ENV_TEMPLATE = """# Looker API credentials for looker-sync. Never commit this file.
# Create an API key in Looker: Admin > Users > your user > Edit Keys.
LOOKER_URL=https://<your-instance>.looker.com
LOOKER_CLIENT_ID=
LOOKER_CLIENT_SECRET=
"""


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def prompt_text(prompt, default=None):
    shown = f'{prompt} [{default}]: ' if default else f'{prompt}: '
    return input(shown).strip() or default


def ensure_gitignore(root, lines):
    path = os.path.join(root, '.gitignore')
    have = open(path).read().splitlines() if os.path.isfile(path) else []
    add = [l for l in lines if l not in have]
    if add:
        with open(path, 'a') as f:
            if have and not open(path).read().endswith('\n'):
                f.write('\n')
            f.write('\n'.join(add) + '\n')
        out(f'added {", ".join(add)} to .gitignore')


def env_ready(path):
    """True when the env file exists and holds real values, not the template blanks."""
    if not os.path.isfile(path):
        return False
    try:
        load_env(path)
    except LookerError:
        return False
    with open(path) as f:
        return '<your-instance>' not in f.read()


def cmd_init(proj, args):
    resume = bool(proj.config) and not proj.state.get('synced_at')
    if proj.config and not resume:
        raise Refused(f'{proj.config_path} already exists; edit it or remove it first')
    project = (proj.config or {}).get('project') or args.project
    if not project and interactive():
        project = prompt_text('Looker project id (the name in the Looker IDE)')
    if not project:
        raise Refused('init needs --project <looker project id>')
    env_path = proj.env_path(args.env_file)
    if not env_ready(env_path):
        if not os.path.isfile(env_path):
            os.makedirs(os.path.dirname(os.path.abspath(env_path)), exist_ok=True)
            fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(ENV_TEMPLATE)
            out(f'wrote {env_path} (a template; owner-only permissions)')
        if os.path.abspath(env_path).startswith(proj.root + os.sep):
            ensure_gitignore(proj.root, [os.path.relpath(env_path, proj.root), META_DIR + '/'])
        raise Refused(f'fill in LOOKER_URL, LOOKER_CLIENT_ID and LOOKER_CLIENT_SECRET in {env_path}, '
                      f'then run: looker-sync init --project {project}')
    mode = (proj.config or {}).get('mode') or args.mode
    sess = Session(proj, args.env_file, mode, project, args.branch, check_branch=False)
    branch = (proj.config or {}).get('branch') or args.branch or sess.branch
    if mode == 'dev' and branch != sess.branch:
        raise Refused(f'Looker dev workspace is on {sess.branch!r}, not {branch!r}. init never '
                      'switches; check out the branch in the Looker IDE first, or omit --branch.')
    if not proj.config:
        env_flag = args.env_file
        env_rel = os.path.relpath(os.path.abspath(env_flag), proj.root) if env_flag else '.env'
        proj.config = {'project': project, 'mode': mode, 'branch': branch if mode == 'dev' else None,
                       'env_file': env_rel, 'include': args.include or INIT_INCLUDE,
                       'exclude': args.exclude or INIT_EXCLUDE}
        proj.save_config()
        out(f'wrote {os.path.relpath(proj.config_path, proj.root)}')
    ensure_gitignore(proj.root, [META_DIR + '/'])
    init_git(proj, sess, args)
    if args.no_pull:
        out('skipped the first pull; run: looker-sync pull')
        return OK
    if interactive() and (prompt_text('Pull the project files into this folder now? (y/n)', 'y') or 'y').lower() != 'y':
        out('skipped the first pull; run: looker-sync pull')
        return OK
    drift_guard(proj, sess, 'pull', refuse=True)   # a clone off Looker's branch: link, then refuse
    return do_pull(proj, sess, args.force)


def init_git(proj, sess, args):
    """The link offer: a flag decides; else ask on a terminal; else try origin and say so."""
    later = 'run `looker-sync link [--remote NAME]` to link this folder to a git remote later'
    if args.no_git:
        out(f'git: not linked (--no-git); {later}')
        return
    if proj.config.get('git_remote'):
        return
    if args.git_remote:
        url = link_folder(proj, sess, args.git_remote)
        out(f'git: linked to git remote {args.git_remote!r} ({safe_url(url)})')
        return
    if not looks_like_git(proj.root) or not LocalGit(proj.root).is_work_tree():
        return
    if interactive():
        ans = prompt_text('This folder is a git clone. Link this folder to a git remote, so looker-sync '
                          'keeps the git branch and the Looker branch together? Remote name, or n to skip',
                          'origin')
        if ans.lower() in ('n', 'no'):
            out(f'git: not linked; {later}')
            return
        url = link_folder(proj, sess, ans)
        out(f'git: linked to git remote {ans!r} ({safe_url(url)})')
        return
    try:
        url = link_folder(proj, sess, 'origin')
    except Refused as e:
        out(f"git: not linked. The default without a terminal is to link remote 'origin', and that was "
            f'refused: {e}\n  Fix it and {later}, or pass --no-git to skip the attempt.')
        return
    out(f"git: linked to git remote 'origin' ({safe_url(url)}), the default without a terminal; "
        'pass --no-git to skip')


def cmd_link(proj, args):
    cfg = proj.require_config()
    sess = Session(proj, args.env_file, cfg['mode'], cfg['project'], cfg.get('branch'))
    url = link_folder(proj, sess, args.remote)
    out(f'linked to git remote {args.remote!r} ({safe_url(url)}); every command now checks that this '
        'clone holds the commit Looker is on')
    return OK


def cmd_pull(proj, args):
    sess = open_session(proj, args)
    drift_guard(proj, sess, 'pull', refuse=True)
    return do_pull(proj, sess, args.force, args.dry_run)


def cmd_status(proj, args):
    sess = open_session(proj, args)
    drift_guard(proj, sess, 'status', refuse=False)
    kinds = plan(proj.local_files(), sess.remote_files(), proj.state.get('files', {}))
    show(kinds, 'status')
    info = getattr(sess, 'branch_info', None) or {}
    if sess.mode == 'dev' and info.get('ahead_count') is not None:
        out(f'git: {info.get("ahead_count")} commit(s) ahead of and {info.get("behind_count")} behind '
            f'{info.get("remote_ref") or "the remote"}; commit and push with git, not looker-sync')
    if proj.state.get('synced_at'):
        out(f'last sync: {proj.state["synced_at"]} by {proj.state.get("user")}')
    return OK


def push_reads(local, listed, known):
    """The Looker files push must read: only those it may write or delete.

    A file is read when it changed locally since the last sync (its local hash differs from
    state.json, a new local file included) and Looker lists it, or when it was deleted locally
    and Looker still lists it. Every other listed file is unchanged locally, so push leaves it
    alone whatever Looker holds; reading it could only add a refusal about a file push never
    touches. Presence alone (listed or not) settles remote-new and remote-deleted.
    """
    return sorted(p for p in listed
                  if (sha(local[p]) != known.get(p) if p in local else p in known))


def do_push(proj, sess, force=False, delete=False, dry_run=False, validate=True, verbose=False,
            phases=None):
    phases = phases or Phases()
    if sess.mode != 'dev':
        raise Refused('push is refused in prod mode; switch --mode dev first')
    # Not verified live with two users (a non-admin cannot see a project with no model in
    # production, and the sandbox has none): the flag is what the branch resource reports.
    if (getattr(sess, 'branch_info', None) or {}).get('readonly'):
        raise Refused(f'Looker reports branch {sess.branch!r} as read-only for you, as a colleague\'s '
                      'personal branch is; review it, then switch to a branch of your own to push')
    known = proj.state.get('files', {})
    with phases('local scan'):
        local = proj.local_files()
    with phases('file list'):
        listed = {f['path'] for f in sess.file_list() if proj.synced(f['path'])}
    need = push_reads(local, listed, known)
    with phases('remote reads', f'{len(need)} of {len(listed)} file(s)'):
        remote = dict(zip(need, sess.L.read_files(sess.project, need))) if need else {}
    # a listed file left unread is unchanged locally: plan it as unchanged in Looker too, so it
    # is never written; one not in state and not local is remote-new, which needs no bytes
    remote_h = {p: sha(remote[p]) if p in remote else known.get(p) or 'listed, not read'
                for p in listed}
    kinds = plan_hashes({p: sha(d) for p, d in local.items()}, remote_h, known)
    show(kinds, 'push plan')
    guarded = [p for p, k in kinds.items() if k in REMOTE_CHANGED]
    if guarded and not force:
        raise Refused('changed in Looker since the last sync: ' + ', '.join(guarded)
                      + '. Pull first, or push --force to overwrite with the local copy.')
    writes = [p for p, k in kinds.items() if p in local and k in ('local-new', 'local-modified', 'conflict', 'remote-deleted')]
    deletes = [p for p, k in kinds.items() if p not in local and k in ('remote-new', 'local-deleted', 'conflict')]
    skipped = [p for p, k in kinds.items() if k == 'remote-modified']
    if skipped:
        out('left alone, unchanged locally (pull to get Looker\'s copy): ' + ', '.join(skipped))
    if deletes and not delete:
        out('not deleted without --delete: ' + ', '.join(deletes))
        deletes = []
    if dry_run:
        out(f'dry run: would write {len(writes)} and delete {len(deletes)} file(s); nothing written')
        return OK
    # New files first: a changed file (an include:) is more likely to reference a new one than the
    # reverse, so stopping halfway leaves Looker valid. Each write is read back and recorded at once,
    # so a failure later in the run never makes the next push see our own writes as Looker edits.
    writes.sort(key=lambda p: p in listed)
    done, verified, bad = [], {}, []
    started = time.perf_counter()
    try:
        for p in writes:
            sess.write(p, local[p], exists=p in listed)
            done.append(p)
            out(f'  wrote {p}')
            back = sess.read(p)
            if back == local[p]:
                verified[p] = sha(back)
                record(proj, sess, {p: verified[p]})
            else:
                bad.append(p)
        for p in deletes:
            sess.delete(p)
            done.append(p)
            out(f'  deleted {p}')
            record(proj, sess, {}, drops=[p])
    except LookerError:
        out('push stopped partway. Recorded as in sync, so the next push retries only the rest.')
        out('  written: ' + (', '.join(done) or '(none)'))
        out('  not written: ' + (', '.join(p for p in writes + deletes if p not in done) or '(none)'))
        raise
    finally:
        phases.note('write+read-back', time.perf_counter() - started,
                    f'{len(writes)} write(s), {len(deletes)} delete(s)')
    updates = in_sync(local, remote)
    updates.update(verified)
    record(proj, sess, updates, drops=deletes)
    out(f'read-back: {len(verified)}/{len(writes)} byte-equal')
    for p in bad:
        out(f'  MISMATCH {p}: Looker holds different bytes than were sent')
    errors = 0
    if (writes or deletes) and not validate:
        out('validation: skipped (--no-validate)')
    elif writes or deletes:
        with phases('validate'):
            errors = sess.validate(verbose=verbose)
    if bad:
        return LOOKER_ERROR
    return INVALID if errors else OK


def cmd_push(proj, args):
    phases = Phases(args.verbose)
    try:
        sess = open_session(proj, args, fetch=False, phases=phases)
        with phases('drift guard'):
            drift_guard(proj, sess, 'push', refuse=True)
        return do_push(proj, sess, args.force, args.delete, args.dry_run, validate=not args.no_validate,
                       verbose=args.verbose, phases=phases)
    finally:
        phases.total()


def ask(question, choices):
    while True:
        ans = input(f'{question} ').strip().lower()
        for c in choices:
            if ans and c.startswith(ans):
                return c


def backup_copy(proj, files, label):
    """Copy files ({path: bytes}) to .looker-sync/backups/<label>-<stamp>/."""
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    dest = os.path.join(proj.meta, 'backups', f'{re.sub(r"[^A-Za-z0-9._-]", "_", label)}-{stamp}')
    for p, data in files.items():
        full = os.path.join(dest, *p.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(data)
    out(f'backed up {len(files)} local file(s) to {os.path.relpath(dest, proj.root)}')


def git_can_follow(proj, target, in_looker):
    """Before anything moves: refuse if the local git branch could not follow Looker to target."""
    remote, g = proj.config['git_remote'], LocalGit(proj.root)
    local = g.has_ref(f'refs/heads/{target}')
    tracking = g.has_ref(f'refs/remotes/{remote}/{target}')
    if not in_looker and local and not tracking:
        raise Refused(f'branch {target!r} exists only in this clone; push it to the remote first '
                      f'(git push -u {remote} {target}), then run switch again')
    if in_looker and not (local or tracking):
        raise Refused(f'branch {target!r} is in Looker but not in this clone, even after fetching '
                      f'{remote!r}; push it from the Looker IDE, then run switch again')


def git_stash(proj, env_flag):
    """Stash a dirty work tree, never the tool's own files. Returns (from, stash name or None)."""
    g = LocalGit(proj.root)
    origin = g.branch() or g.short_head() or 'HEAD'
    # Name only what git does not already ignore: an exclude pathspec naming an ignored path
    # makes `git stash push` fail after saving, with the tree half cleaned (git 2.43).
    own = own_paths(proj, env_flag)
    own = [p for p in own if p not in g.ignored(own)]
    if not g.dirty(exclude=own):
        return origin, None
    name = f'looker-sync {origin} {datetime.datetime.now().strftime("%Y%m%dT%H%M%S")}'
    rc, _, err = g.run('stash', 'push', '--include-untracked', '-m', name, *pathspecs(own))
    if rc:
        raise Refused(f'git stash failed, so the local git branch was not moved: {err}')
    out(f'git: stashed the uncommitted changes as {name!r}')
    return origin, name


def git_checkout(proj, sess, target):
    """Check out target (from <remote>/target if new), then fast-forward to Looker's commit."""
    remote, g = proj.config['git_remote'], LocalGit(proj.root)
    if g.branch() != target:
        if g.has_ref(f'refs/heads/{target}'):
            rc, _, err = g.run('checkout', '--quiet', target)
        else:
            rc, _, err = g.run('checkout', '--quiet', '--track', '-b', target, f'{remote}/{target}')
        if rc:
            raise Refused(f'git checkout {target} failed: {err}')
        out(f'git: checked out {target}')
    info = getattr(sess, 'branch_info', None) or {}
    # Right after Looker checks out a branch that was only on the remote, it answers ref ''
    # (verified live); its commit is then the remote's.
    ref, head = info.get('ref') or info.get('remote_ref'), g.head()
    if not ref or ref == head:
        return
    if not g.has_commit(ref):
        out(f"warning: Looker's commit {ref[:12]} is not in this clone (not pushed yet?); "
            f'git branch {target!r} left where it is')
    elif g.is_ancestor(ref, head):
        out(f"warning: git branch {target!r} is ahead of {ref[:12]}, the commit Looker is on; left where it is")
    elif not g.is_ancestor(head, ref):
        out(f"warning: git branch {target!r} has diverged from {ref[:12]}, the commit Looker is on; "
            'left where it is. git diff shows how the mirrored files differ.')
    else:
        rc, _, err = g.run('merge', '--ff-only', '--quiet', ref)
        if rc:
            out(f'warning: git merge --ff-only {ref[:12]} failed ({err}); git branch {target!r} left where it is')
        else:
            out(f'git: fast-forwarded {target} to {ref[:12]}, the commit Looker is on')


def cmd_switch(proj, args):
    cfg = proj.require_config()
    interactive = sys.stdin.isatty()
    if args.force:
        args.local_changes, args.yes = 'backup', True
    sess = open_session(proj, args, check_branch=False)
    linked = bool(cfg.get('git_remote'))
    drift = git_drift(proj, sess) if linked else None
    target_mode = 'dev' if args.branch else args.mode
    if target_mode == 'dev' and sess.mode != 'dev':
        sess.set_mode('dev')
        sess.branch = sess.current_branch()
    target_branch = args.branch or (sess.branch if target_mode == 'dev' else None)
    git_to = git_target(sess, target_mode, target_branch) if linked else None
    if (target_mode == cfg['mode'] and (target_mode == 'prod' or target_branch == cfg.get('branch') == sess.branch)
            and (not linked or LocalGit(proj.root).branch() == git_to)):
        out(f'already on {target_mode} {target_branch or ""}; nothing to switch')
        return OK
    if args.branch:
        names = [b.get('name') for b in sess.L.call('GET', f'/projects/{cfg["project"]}/git_branches') or []]
        if args.branch not in names:
            if linked:
                git_can_follow(proj, args.branch, in_looker=False)
            raise Refused(f'branch {args.branch!r} does not exist in Looker; switch never creates branches')
    if linked:
        git_can_follow(proj, git_to, in_looker=True)

    def remote_guard():
        if cfg['mode'] != 'dev':
            return
        dirty = sess.uncommitted()
        if dirty:
            raise Refused('Looker dev workspace has uncommitted changes:\n'
                          + '\n'.join(f'    {p}  ({t})' for p, t in dirty)
                          + '\n  Commit or revert them in the Looker IDE, then run switch again.')

    remote_guard()
    local = proj.local_files()
    known = proj.state.get('files', {})
    label = (cfg.get('branch') if cfg['mode'] == 'dev' else 'production') or 'unknown'
    changed = [p for p in sorted(set(local) | set(known)) if sha(local.get(p)) != known.get(p)]
    if changed:
        out('local changes not pushed to Looker:')
        for p in changed:
            out(f'    {p}')
        choice = args.local_changes
        if not choice and interactive:
            choice = ask('[p]ush them first, [b]ack up and discard, or [c]ancel?', ['push', 'backup', 'cancel'])
        if not choice:
            raise Refused('no terminal to ask; pass --local-changes push|backup|cancel')
        if choice == 'cancel':
            raise Refused('cancelled; nothing changed')
        if choice == 'push':
            if cfg['mode'] != 'dev':
                raise Refused('cannot push from prod mode; use --local-changes backup or cancel')
            if drift:
                raise Refused(f'git is on {drift[1] or "a detached HEAD"}, not {drift[0]!r}, so these files '
                              'belong to another branch; use --local-changes backup (a git stash) or cancel')
            do_push(proj, sess)
            remote_guard()
        if choice == 'backup' and not linked:   # linked: the git stash below is the backup
            backup_copy(proj, local, label)

    where = f'branch {target_branch}' if target_mode == 'dev' else 'production'
    if not args.yes:
        if not interactive:
            raise Refused(f'no terminal to confirm; pass --yes to switch to {where}')
        also = ' and the local git branch' if linked else ''
        if ask(f'Switch to {where}? This moves your Looker IDE{also} too. [y/N]', ['yes', 'no']) != 'yes':
            raise Refused('cancelled; nothing changed')

    if target_mode == 'dev':
        if sess.branch != target_branch:
            sess.L.call('PUT', f'/projects/{cfg["project"]}/git_branch', body={'name': target_branch})
            now = sess.current_branch()
            if now != target_branch:
                raise LookerError(f'checked out {target_branch!r} but Looker reports {now!r}')
            sess.branch = now
            out(f'Looker dev workspace now on {target_branch}; a Looker IDE tab shows it after a refresh')
    else:
        sess.set_mode('prod')
        sess.branch = sess.current_branch()

    stash = None
    if linked:
        origin, stash = git_stash(proj, args.env_file)
        # what the stash could not take (git-ignored synced files) still gets the file copy
        left = {p: d for p, d in proj.local_files().items() if local.get(p) == d and sha(d) != known.get(p)}
        if left:
            backup_copy(proj, left, label)
        git_checkout(proj, sess, git_to)

    old = proj.local_files()   # after the git checkout, which may already have changed the tree
    for p in old:
        try:
            os.remove(os.path.join(proj.root, *p.split('/')))
        except FileNotFoundError:
            pass
    remote = sess.remote_files()
    for p, data in remote.items():
        proj.write_local(p, data)
    back = f'--branch {cfg["branch"]}' if cfg['mode'] == 'dev' and cfg.get('branch') else '--mode prod'
    cfg.update({'mode': target_mode, 'branch': target_branch})
    proj.save_config()
    proj.state = {'files': {}}
    record(proj, sess, {p: sha(d) for p, d in remote.items()})
    out(f'switched to {target_mode} ({where}); mirrored {len(remote)} file(s), removed {len(old)} old local file(s)')
    if stash:
        if drift:   # the stash came off a branch the config was not on: point at that git branch
            way = f'git checkout {origin} (pull and push refuse there until you switch back)'
        else:
            way = f'looker-sync switch {back} --force'
        out(f'git: your uncommitted changes from {origin} are in the stash {stash!r}; looker-sync never pops it. '
            f'To get them back: {way}, find it with git stash list, then git stash pop')
    return OK


def version():
    try:
        from importlib.metadata import version as dist_version
        return dist_version('looker-sync')
    except Exception:
        return 'unknown'


def skill_file():
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (('_skills_data', 'looker-sync', 'SKILL.md'),            # installed wheel
                ('..', '..', 'skills', 'looker-sync', 'SKILL.md')):      # source checkout
        path = os.path.normpath(os.path.join(here, *rel))
        if os.path.isfile(path):
            return path
    raise LookerError('SKILL.md is not packaged in this install')


def cmd_skill(args):
    try:
        path = skill_file()
    except LookerError as e:
        out(f'LOOKER ERROR: {e}')
        return LOOKER_ERROR
    if args.path:
        out(path)
    else:
        with open(path, encoding='utf-8') as f:
            sys.stdout.write(f.read())
    return OK


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='looker-sync', description=__doc__.split('\n\n')[0],
        epilog='Run `looker-sync skill` for the full guide (workflows, guards, traps) written for agents.')
    ap.add_argument('--version', action='version', version='looker-sync ' + version())
    ap.add_argument('-C', '--project-dir', default='.', help='Looker project folder (default: cwd)')
    ap.add_argument('--env-file', help='env file with LOOKER_URL, LOOKER_CLIENT_ID, LOOKER_CLIENT_SECRET')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('init', help='set up this folder (works when empty), then a first pull')
    p.add_argument('--project', help='Looker project id; asked for on a terminal if omitted')
    p.add_argument('--mode', choices=['dev', 'prod'], default='dev')
    p.add_argument('--branch', help="default: the branch Looker's dev workspace is on")
    p.add_argument('--include', nargs='+')
    p.add_argument('--exclude', nargs='+')
    p.add_argument('--no-pull', action='store_true', help='write the metadata only')
    p.add_argument('--force', action='store_true', help="take Looker's copy over files already in the folder")
    g = p.add_mutually_exclusive_group()
    g.add_argument('--git-remote', metavar='NAME', help='link to this git remote; refuse if it cannot be')
    g.add_argument('--no-git', action='store_true', help='do not link the folder to git')
    p = sub.add_parser('link', help='link this folder to a git remote of its clone')
    p.add_argument('--remote', default='origin', help='the git remote name (default: origin)')
    p = sub.add_parser('pull', help='copy Looker files to disk; never deletes local files')
    p.add_argument('--force', action='store_true', help="take Looker's copy over unpushed local changes")
    p.add_argument('--dry-run', action='store_true')
    sub.add_parser('status', help='show what differs; writes nothing')
    p = sub.add_parser('push', help='write local changes, read them back, validate')
    p.add_argument('--force', action='store_true', help='overwrite files changed in Looker since the last sync')
    p.add_argument('--delete', action='store_true', help='delete files that exist only in Looker')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--no-validate', action='store_true', help='skip Looker validation after writing')
    p.add_argument('--verbose', action='store_true',
                   help='time each step on stderr; show info-severity validation lines')
    p = sub.add_parser('watch', help='push each local save to Looker as it happens, and delete '
                                     'in Looker what you delete locally while it runs')
    p.add_argument('--validate', action='store_true', help='validate in Looker after writes go quiet')
    p.add_argument('--verbose', action='store_true', help='show info-severity validation lines')
    p.add_argument('--poll', type=int, default=50, metavar='MS', help='scan interval in ms (default 50)')
    p.add_argument('--force', action='store_true', help='overwrite or delete files changed in Looker since the last sync')
    p.add_argument('--no-delete', action='store_true', help='report local deletes and renames instead of applying them')
    p = sub.add_parser('switch', help='move Looker and the folder to another branch or mode')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--branch')
    g.add_argument('--mode', choices=['dev', 'prod'])
    g = p.add_mutually_exclusive_group()
    g.add_argument('--local-changes', choices=['push', 'backup', 'cancel'],
                   help='what to do with unpushed local changes when there is no terminal')
    g.add_argument('--force', action='store_true', help='= --local-changes backup --yes (a git stash when linked)')
    p.add_argument('--yes', action='store_true', help='confirm the switch without a terminal')
    p = sub.add_parser('skill', help='print the full usage guide written for agents')
    p.add_argument('--path', action='store_true', help='print where the guide file is instead')
    args = ap.parse_args(argv)
    if args.cmd == 'skill':
        return cmd_skill(args)
    proj = Project(args.project_dir)
    try:
        return {'init': cmd_init, 'link': cmd_link, 'pull': cmd_pull, 'status': cmd_status,
                'push': cmd_push, 'switch': cmd_switch,
                'watch': cmd_watch}[args.cmd](proj, args)
    except Refused as e:
        out(f'REFUSED: {e}')
        return REFUSED
    except LookerError as e:
        out(f'LOOKER ERROR: {e}')
        return LOOKER_ERROR


if __name__ == '__main__':
    sys.exit(main())
