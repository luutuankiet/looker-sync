"""Tests for looker_sync.py: the CLI runs as a subprocess against a fake Looker.

    python3 -m unittest discover -s scripts -p 'test_looker_sync.py'

The fake keeps files, branches, a workspace per token and per-file git status in memory,
and imitates the Looker behaviour proven live: tokens start in production, the dev
workspace is per user, branch checkout carries uncommitted files, POST/PUT/DELETE files.
It also serves the git fields of the project and branch resources as seen live (see
docs/reference/looker-file-api-behaviour.md). GitTestCase adds real git: a bare repository
as the remote and a clone of it as the project folder, kept in step by GitRepos.
"""
import hashlib
import json
import os
import pty
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'looker_sync.py')
PROJECT = 'demo'
SECRET = 'not-a-real-secret-9f8e7d'

# The three shapes of a project's git_remote_url seen live: (uses_git, git_remote_url,
# git_service_name). A real remote is SSH or HTTPS; a Looker-hosted bare repository shows as a
# relative path into Looker's own bare-model store; a project without git has null.
GIT_REMOTE_SHAPES = {
    'remote': (True, 'git@github.com:example/demo.git', 'github'),
    'https': (True, 'https://github.com/example/demo.git', 'github'),
    'bare': (True, '../../bare_models/demo.git', None),
    'none': (False, None, None),
}


class FakeLooker:
    def __init__(self):
        self.branches = {'master': {'manifest.lkml': 'project_name: "demo"\n'},
                         'feat': {'manifest.lkml': 'project_name: "demo"\n'}}
        self.production = dict(self.branches['master'])
        self.dev_branch = 'feat'
        self.uncommitted = {}        # path -> content, or None for a deleted file
        self.dirs = {'models', 'views'}
        self.tokens = {}             # token -> workspace
        self.log = []                # (method, path, workspace, body)
        self.validation_errors = []
        self.mangle_readback = set()  # Looker stores different bytes than were sent
        self.fail_writes = set()  # writes to these paths answer 500
        self.fail_deletes = set()  # deletes of these paths answer 500
        self.ignore_deletes = set()  # deletes of these paths answer 204 and keep the file
        self.fail_checkout = set()  # checking out these branches answers 500
        # git fields, shaped as on a live instance
        self.production_branch = 'master'
        self.set_git_remote('remote')
        self.branch_git = {}         # branch -> fields overriding the branch resource defaults
        self.remote_only = set()     # pushed with plain git, never checked out in Looker
        # live: right after checking out a remote-only branch, GET git_branch still answers the
        # remote-only shape (ref '', counts null) while the list already shows it local
        self.stale_checkout = set()
        self.personal = {'feat'}     # live: the user's own dev branch is personal
        self.readonly = {'master'}   # live: the production branch is readonly in dev mode
        # connection layer: HTTP/1.1 keep-alive, as measured live (idle connections survived 30,
        # 120 and 300 s); login answers expires_in like live (3599 s)
        self.expires_in = 3599
        self.logins = 0              # successful logins so far
        self.connections = 0         # TCP connections accepted
        self.sockets = []            # every accepted socket, for drop_connections
        self.force_401 = 0           # answer this many authenticated requests (not /session) with 401

    def set_git_remote(self, shape, url=None):
        """Serve one of GIT_REMOTE_SHAPES on GET /projects/{p}; url overrides the remote URL."""
        uses_git, default_url, service = GIT_REMOTE_SHAPES[shape]
        self.project_git = {'uses_git': uses_git, 'git_remote_url': url or default_url,
                            'git_service_name': service}

    def add_remote_only_branch(self, name, files):
        """A branch pushed with plain git: listed with is_local false until Looker checks it out."""
        self.branches[name] = dict(files)
        self.remote_only.add(name)

    def project_resource(self):
        return {'id': PROJECT, 'name': PROJECT, **self.project_git,
                'git_production_branch_name': self.production_branch,
                'pull_request_mode': 'off', 'validation_required': True, 'is_example': False}

    def branch_resource(self, name, single=False):
        files = self.branches.get(name, {})
        # without GitRepos there is no real commit; a stable 40-hex stand-in keeps the shape
        ref = hashlib.sha1(json.dumps([name, sorted(files.items())]).encode()).hexdigest()
        res = {'name': name, 'remote': 'origin', 'remote_name': name, 'error': '', 'message': '',
               'owner_name': 'Test User', 'readonly': name in self.readonly,
               'personal': name in self.personal, 'is_local': name not in self.remote_only,
               'is_remote': True, 'is_production': name == self.production_branch,
               'ahead_count': 0, 'behind_count': 0, 'commit_at': 1700000000,
               'ref': ref, 'remote_ref': ref, 'can': {}}
        res.update(self.branch_git.get(name, {}))
        if name in self.remote_only or (single and name in self.stale_checkout):
            # live: a remote-only branch has no Looker commit yet, only the remote's
            res.update(is_local=False, ref='', ahead_count=None, behind_count=None, personal=False)
        return res

    def dev_files(self):
        files = dict(self.branches[self.dev_branch])
        for p, c in self.uncommitted.items():
            if c is None:
                files.pop(p, None)
            else:
                files[p] = c
        return files

    def git_status(self, path):
        if path not in self.uncommitted:
            return None
        if self.uncommitted[path] is None:
            return {'action': 'delete', 'text': 'Deleted'}
        if path in self.branches[self.dev_branch]:
            return {'action': 'add', 'text': 'Modified'}
        return {'action': 'add', 'text': 'New file'}

    def dev_write(self, path, content):
        if content is not None and path in self.mangle_readback:
            content += 'X'
        if content is not None and self.branches[self.dev_branch].get(path) == content:
            self.uncommitted.pop(path, None)
        else:
            self.uncommitted[path] = content

    def drop_connections(self):
        """Close every open connection from the server side, as an idle timeout would."""
        for s in self.sockets:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def serve(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            # Live Looker keeps connections alive; HTTP/1.1 makes the fake do the same. Every
            # reply carries Content-Length, so the client can reuse the connection.
            protocol_version = 'HTTP/1.1'

            def setup(self):
                super().setup()
                # headers and body go out in two writes; without this, Nagle plus the client's
                # delayed ACK stalls every reply on a kept-alive connection by about 40 ms
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                fake.connections += 1
                fake.sockets.append(self.connection)

            def log_message(self, *a):
                pass

            def reply(self, status, payload=None, raw=None):
                body = raw.encode() if raw is not None else (json.dumps(payload).encode() if payload is not None else b'')
                self.send_response(status)
                self.send_header('Content-Type', 'text/plain' if raw is not None else 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self, method):
                url = urllib.parse.urlparse(self.path)
                path, q = url.path[len('/api/4.0'):], dict(urllib.parse.parse_qsl(url.query))
                n = int(self.headers.get('Content-Length') or 0)
                raw_body = self.rfile.read(n).decode() if n else ''
                if path == '/login':
                    form = dict(urllib.parse.parse_qsl(raw_body))
                    if form.get('client_secret') != SECRET:
                        return self.reply(404, {'message': 'Not found'})
                    tok = f'tok{fake.logins}'   # unique even after a test revokes a token
                    fake.logins += 1
                    fake.tokens[tok] = 'production'
                    return self.reply(200, {'access_token': tok, 'token_type': 'Bearer',
                                            'expires_in': fake.expires_in})
                tok = (self.headers.get('Authorization') or '').replace('token ', '')
                if tok not in fake.tokens:
                    return self.reply(401, {'message': 'Requires authentication.'})
                if fake.force_401 and path != '/session':
                    fake.force_401 -= 1
                    return self.reply(401, {'message': 'Requires authentication.'})
                ws = fake.tokens[tok]
                body = json.loads(raw_body) if raw_body else None
                fake.log.append((method, path, ws, body))
                dev = ws == 'dev'
                files = fake.dev_files() if dev else fake.production
                pre = f'/projects/{PROJECT}'
                if path == '/session':
                    if method == 'PATCH':
                        fake.tokens[tok] = body['workspace_id']
                    return self.reply(200, {'workspace_id': fake.tokens[tok]})
                if path == '/user':
                    return self.reply(200, {'id': 7, 'display_name': 'Test User', 'email': 't@example.com'})
                if path == pre and method == 'GET':
                    return self.reply(200, fake.project_resource())
                if path == pre + '/git_branch':
                    if method == 'GET':
                        return self.reply(200, fake.branch_resource(fake.dev_branch if dev else fake.production_branch,
                                                                    single=True))
                    if not dev:
                        return self.reply(403, {'message': 'dev mode only'})
                    if 'ref' in body:
                        return self.reply(500, {'message': 'test fake: ref is forbidden'})
                    if body['name'] not in fake.branches:
                        return self.reply(404, {'message': 'Not found'})
                    if body['name'] in fake.fail_checkout:
                        return self.reply(500, {'message': 'test fake: checkout failed'})
                    fake.dev_branch = body['name']
                    fake.stale_checkout = {fake.dev_branch} & fake.remote_only
                    fake.remote_only.discard(fake.dev_branch)   # live: checkout by name makes it local
                    return self.reply(200, fake.branch_resource(fake.dev_branch, single=True))
                if path == pre + '/git_branches':
                    return self.reply(200, [fake.branch_resource(b) for b in fake.branches])
                if path == pre + '/files' and method == 'GET':
                    return self.reply(200, [{'path': p, 'git_status': fake.git_status(p) if dev else None}
                                            for p in sorted(files)])
                if path == pre + '/file/content':
                    p = q['file_path']
                    if p not in files:
                        return self.reply(404, {'message': 'File not found'})
                    return self.reply(200, raw=files[p])
                if path == pre + '/directories':
                    fake.dirs.add(body['path'])
                    return self.reply(200, {'path': body['path']})
                if path == pre + '/files' and method in ('POST', 'PUT', 'DELETE'):
                    if not dev:
                        return self.reply(403, {'message': 'dev mode only'})
                    p = q.get('file_path') if method == 'DELETE' else body['path']
                    if method != 'DELETE' and p in fake.fail_writes:
                        return self.reply(500, {'message': 'test fake: write failed'})
                    if method == 'DELETE' and p in fake.fail_deletes:
                        return self.reply(500, {'message': 'test fake: delete failed'})
                    if method == 'DELETE' and p in fake.ignore_deletes:
                        return self.reply(204, None)    # says it worked, keeps the file
                    if method == 'POST':
                        if p in files:
                            return self.reply(400, {'message': 'File already exists'})
                        if '/' in p and p.rsplit('/', 1)[0] not in fake.dirs:
                            # real Looker answers 400, not 404, for a missing folder (see
                            # docs/reference/looker-file-api-behaviour.md); keep the fake as strict as live
                            return self.reply(400, {'message': 'No such file or directory - /looker_data/modelshare/models-user-20/' + p})
                    elif p not in files:
                        return self.reply(404, {'message': 'File not found'})
                    fake.dev_write(p, None if method == 'DELETE' else body['content'])
                    return self.reply(204 if method == 'DELETE' else 200, None if method == 'DELETE' else {'path': p})
                if path == pre + '/validate':
                    return self.reply(200, {'errors': fake.validation_errors})
                return self.reply(404, {'message': f'fake has no {method} {path}'})

            def do_GET(self):
                self.handle_any('GET')

            def do_POST(self):
                self.handle_any('POST')

            def do_PUT(self):
                self.handle_any('PUT')

            def do_PATCH(self):
                self.handle_any('PATCH')

            def do_DELETE(self):
                self.handle_any('DELETE')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{self.server.server_address[1]}'


def hermetic_git_env(root):
    """Environment for every git process in a test, the CLI's included.

    Nothing from the developer's machine leaks in: no system or global config, no signing,
    hooks or templates, a fixed identity (the CI runner has git but no identity) and an
    explicit initial branch. Inherited GIT_* variables (GIT_DIR and friends) are dropped.
    """
    empty = os.path.join(root, 'git-empty')
    os.makedirs(empty, exist_ok=True)
    cfg = os.path.join(root, 'gitconfig')
    with open(cfg, 'w') as f:
        f.write(f'[init]\n\tdefaultBranch = master\n\ttemplateDir = {empty}\n'
                f'[core]\n\thooksPath = {empty}\n\tautocrlf = false\n\tfsmonitor = false\n'
                '[commit]\n\tgpgSign = false\n[tag]\n\tgpgSign = false\n'
                '[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n'
                '[advice]\n\tdetachedHead = false\n[protocol "file"]\n\tallow = always\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=cfg, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
               GIT_AUTHOR_NAME='Test Author', GIT_AUTHOR_EMAIL='author@example.com',
               GIT_COMMITTER_NAME='Test Author', GIT_COMMITTER_EMAIL='author@example.com',
               GIT_AUTHOR_DATE='2026-01-01T00:00:00Z', GIT_COMMITTER_DATE='2026-01-01T00:00:00Z')
    return env


class GitRepos:
    """Real git behind the fake Looker, built in a test's temp dir.

    looker.git  Looker's own copy of the repository; every commit is made here first.
    remote.git  the bare repository the user clones from (`origin` in the clone).

    Each fake branch gets one commit whose tree is exactly the fake's files for it
    (the production branch is the root, every other branch a child of it), pushed to the
    remote. `commit()` keeps the fake's per-branch files, its `ref` / `remote_ref` /
    `ahead_count` / `behind_count`, and the two repositories in step.
    """

    def __init__(self, root, fake):
        self.fake = fake
        self.looker = os.path.join(root, 'looker.git')
        self.remote = os.path.join(root, 'remote.git')
        self.counter = 0
        for repo in (self.looker, self.remote):
            self.git('init', '-q', '--bare', '--initial-branch', fake.production_branch, repo)
        base = self.commit(fake.production_branch, fake.branches[fake.production_branch], parent=None)
        for name in fake.branches:
            if name != fake.production_branch:
                self.commit(name, fake.branches[name], parent=base)

    @staticmethod
    def git(*args, cwd=None, input=None):
        r = subprocess.run(['git', *args], cwd=cwd, input=input, capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise AssertionError(f'git {" ".join(args)} failed: {r.stderr}')
        return r.stdout.strip()

    def tip(self, repo, branch, ns='refs/heads'):
        r = subprocess.run(['git', '-C', repo, 'rev-parse', '-q', '--verify', f'{ns}/{branch}^{{commit}}'],
                           capture_output=True, text=True)
        return r.stdout.strip() or None

    def commit(self, branch, files, *, push=True, looker=True, parent='tip', message=None):
        """Commit `files` (the whole tree) on `branch` and return the SHA.

        push=False   Looker committed but has not pushed: the remote never sees the commit,
                     so `ref` is missing from a clone and `ahead_count` goes up.
        looker=False someone pushed with plain git: the remote moves, Looker's files and
                     `ref` stay, `behind_count` goes up.
        parent       'tip' (the newest commit made for the branch), None (a root) or a SHA.
        """
        if parent == 'tip':
            parent = self.tip(self.looker, branch, ns='refs/tips') or self.tip(self.looker, branch)
        self.counter += 1
        index = os.path.join(self.looker, f'index-{self.counter}')
        env = dict(os.environ, GIT_INDEX_FILE=index)
        for path, content in sorted(files.items()):
            blob = subprocess.run(['git', '-C', self.looker, 'hash-object', '-w', '--stdin'], input=content,
                                  capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(['git', '-C', self.looker, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{path}'],
                           env=env, check=True, capture_output=True)
        tree = subprocess.run(['git', '-C', self.looker, 'write-tree'], env=env, capture_output=True,
                              text=True, check=True).stdout.strip()
        if os.path.exists(index):
            os.remove(index)
        msg = message or f'{branch}: commit {self.counter}'
        sha = self.git('-C', self.looker, 'commit-tree', tree, *(['-p', parent] if parent else []), '-m', msg)
        # the newest commit made for the branch, whichever side it landed on
        self.git('-C', self.looker, 'update-ref', f'refs/tips/{branch}', sha)
        if looker:
            self.git('-C', self.looker, 'update-ref', f'refs/heads/{branch}', sha)
            self.fake.branches[branch] = dict(files)
            if branch == self.fake.production_branch:
                self.fake.production = dict(files)
        if push:
            self.git('-C', self.looker, 'push', '-q', '--force', self.remote, f'{sha}:refs/heads/{branch}')
        self.refresh(branch)
        return sha

    def refresh(self, branch):
        """Recompute the fake's git fields for `branch` from the two repositories."""
        ref, remote_ref = self.tip(self.looker, branch), self.tip(self.remote, branch)
        fields = {'ref': ref, 'remote_ref': remote_ref, 'is_remote': remote_ref is not None}
        if ref and remote_ref:
            behind, ahead = self.git('-C', self.looker, 'rev-list', '--left-right', '--count',
                                     f'{remote_ref}...{ref}').split()
            fields.update(ahead_count=int(ahead), behind_count=int(behind))
        self.fake.branch_git[branch] = fields

    def add_remote_only_branch(self, name, files, parent=None):
        """A branch someone made with plain git and pushed; Looker lists it as remote-only."""
        sha = self.commit(name, files, parent=parent or self.tip(self.looker, self.fake.production_branch))
        self.fake.remote_only.add(name)
        return sha

    def clone(self, dest, branch):
        self.git('clone', '-q', '--branch', branch, self.remote, dest)


class FakeLookerTestCase(unittest.TestCase):
    """A fake Looker and a project folder with a .env pointing at it; no tests of its own."""

    def setUp(self):
        self.fake = FakeLooker()
        self.fake.uncommitted = {'models/demo.model.lkml': 'connection: "db"\n',
                                 'views/orders.view.lkml': 'view: orders {}\n'}
        self.fake.branches['feat'].update(self.fake.uncommitted)
        self.fake.uncommitted = {}
        self.url = url = self.fake.serve()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, 'proj')
        self.make_project_dir()
        with open(os.path.join(self.dir, '.env'), 'w') as f:
            f.write(f'LOOKER_URL={url}\nLOOKER_CLIENT_ID=id\nLOOKER_CLIENT_SECRET={SECRET}\n')

    def make_project_dir(self):
        os.makedirs(self.dir)

    def tearDown(self):
        self.fake.server.shutdown()
        self.fake.server.server_close()
        self.tmp.cleanup()

    def run_cli(self, *args, env=None):
        r = subprocess.run([sys.executable, CLI, '-C', self.dir, *args], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=60, env=env)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        return r

    def init(self):
        r = self.run_cli('init', '--project', PROJECT, '--branch', 'feat')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def local(self, path):
        with open(os.path.join(self.dir, path)) as f:
            return f.read()

    def put_local(self, path, text):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(text)

    def writes(self):
        return [e for e in self.fake.log if e[0] in ('POST', 'PUT', 'DELETE') and '/files' in e[1]
                or e[1].endswith('/git_branch') and e[0] == 'PUT']


class CLITest(FakeLookerTestCase):
    # ---- blank folder, skill, help

    def test_init_in_blank_folder_writes_template_then_resumes(self):
        with open(os.path.join(self.dir, '.env')) as f:
            url = f.readline().split('=', 1)[1].strip()
        blank = os.path.join(self.tmp.name, 'blank')
        os.makedirs(blank)

        def run(*a):
            r = subprocess.run([sys.executable, CLI, '-C', blank, *a], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=60)
            self.assertNotIn(SECRET, r.stdout + r.stderr)
            return r
        r = run('init', '--project', PROJECT)
        self.assertEqual(r.returncode, 2, r.stdout)
        env = os.path.join(blank, '.env')
        self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)
        with open(os.path.join(blank, '.gitignore')) as f:
            ignored = f.read().split()
        self.assertIn('.env', ignored)
        self.assertIn('.looker-sync/', ignored)
        self.assertFalse(os.path.exists(os.path.join(blank, '.looker-sync', 'config.json')))
        with open(env, 'w') as f:
            f.write(f'LOOKER_URL={url}\nLOOKER_CLIENT_ID=id\nLOOKER_CLIENT_SECRET={SECRET}\n')
        r = run('init', '--project', PROJECT, '--branch', 'feat')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(blank, 'models', 'demo.model.lkml')))
        with open(env) as f:
            self.assertIn(SECRET, f.read())   # a filled .env is never overwritten
        self.assertEqual(run('init', '--project', PROJECT).returncode, 2)  # already set up

    def test_init_on_a_terminal_asks_for_project_and_pull(self):
        master, slave = pty.openpty()
        p = subprocess.Popen([sys.executable, CLI, '-C', self.dir, 'init', '--branch', 'feat'],
                             stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        os.write(master, f'{PROJECT}\ny\n'.encode())
        chunks = b''
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            chunks += data
        self.assertEqual(p.wait(timeout=60), 0, chunks.decode(errors='replace'))
        os.close(master)
        self.assertNotIn(SECRET, chunks.decode(errors='replace'))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'models', 'demo.model.lkml')))

    def test_init_without_project_and_no_terminal_is_refused(self):
        r = self.run_cli('init')
        self.assertEqual(r.returncode, 2)
        self.assertIn('--project', r.stdout)

    def test_skill_prints_the_authoritative_file(self):
        r = subprocess.run([sys.executable, CLI, 'skill'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        with open(os.path.join(os.path.dirname(CLI), '..', 'skills', 'looker-sync', 'SKILL.md')) as f:
            self.assertEqual(r.stdout, f.read())
        r = subprocess.run([sys.executable, CLI, 'skill', '--path'], capture_output=True, text=True)
        self.assertTrue(r.stdout.strip().endswith('SKILL.md'))

    def test_help_is_short_and_points_at_skill(self):
        r = subprocess.run([sys.executable, CLI, '--help'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertLess(len(r.stdout.splitlines()), 45)
        self.assertIn('looker-sync skill', r.stdout)

    # ---- pull

    def test_first_pull_writes_dev_files_and_never_touches_production(self):
        r = self.init()
        self.assertIn('as Test User', r.stdout)
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders {}\n')
        self.assertEqual(self.local('manifest.lkml'), 'project_name: "demo"\n')
        file_calls = [e for e in self.fake.log if '/file' in e[1]]
        self.assertTrue(file_calls)
        self.assertTrue(all(ws == 'dev' for _, _, ws, _ in file_calls))

    def test_pull_never_deletes_and_refuses_to_overwrite_local_work(self):
        self.init()
        self.put_local('views/extra.view.lkml', 'view: extra {}\n')
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        self.fake.dev_write('views/orders.view.lkml', 'view: orders { # ide\n}\n')
        r = self.run_cli('pull')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('views/orders.view.lkml', r.stdout)
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders { # mine\n}\n')
        r = self.run_cli('pull', '--force')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders { # ide\n}\n')
        self.assertEqual(self.local('views/extra.view.lkml'), 'view: extra {}\n')

    # ---- push

    def test_push_local_edit_is_read_back_and_validated(self):
        self.init()
        self.put_local('views/orders.view.lkml', 'view: orders { dimension: id {} }\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.fake.dev_files()['views/orders.view.lkml'], 'view: orders { dimension: id {} }\n')
        self.assertIn('read-back: 1/1 byte-equal', r.stdout)
        self.assertTrue(any(e[1].endswith('/validate') for e in self.fake.log))
        self.assertIn('status: nothing differs', self.run_cli('status').stdout)

    def test_push_refused_when_changed_in_looker_then_force_overwrites(self):
        self.init()
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        self.fake.dev_write('views/orders.view.lkml', 'view: orders { # ide\n}\n')
        before = len(self.writes())
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('changed in Looker since the last sync: views/orders.view.lkml', r.stdout)
        self.assertEqual(len(self.writes()), before)
        r = self.run_cli('push', '--force')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.fake.dev_files()['views/orders.view.lkml'], 'view: orders { # mine\n}\n')

    def test_push_creates_in_new_folder_and_deletes_only_with_flag(self):
        self.init()
        self.put_local('dashboards/kpi.dashboard.lookml', '- dashboard: kpi\n')
        os.remove(os.path.join(self.dir, 'views/orders.view.lkml'))
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.fake.dev_files()['dashboards/kpi.dashboard.lookml'], '- dashboard: kpi\n')
        self.assertIn('views/orders.view.lkml', self.fake.dev_files())
        self.assertIn('not deleted without --delete: views/orders.view.lkml', r.stdout)
        r = self.run_cli('push', '--delete')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('views/orders.view.lkml', self.fake.dev_files())

    def test_failed_push_records_what_was_written_and_next_push_does_not_refuse(self):
        self.init()
        self.put_local('views/orders.view.lkml', 'view: orders { # edit\n}\n')
        self.put_local('views/new.view.lkml', 'view: new {}\n')
        self.fake.fail_writes.add('views/orders.view.lkml')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 1, r.stdout)
        # new files go first, so the new file landed before the changed one failed
        self.assertEqual(self.fake.dev_files()['views/new.view.lkml'], 'view: new {}\n')
        self.assertIn('written: views/new.view.lkml', r.stdout)
        self.assertIn('not written: views/orders.view.lkml', r.stdout)
        self.assertIn('views/new.view.lkml', json.loads(self.local('.looker-sync/state.json'))['files'])
        self.fake.fail_writes.clear()
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('changed on both sides', r.stdout)
        self.assertEqual(self.fake.dev_files()['views/orders.view.lkml'], 'view: orders { # edit\n}\n')

    def test_env_and_non_lookml_files_are_never_sent(self):
        self.init()
        self.put_local('notes.md', '# notes\n')
        self.put_local('.claude/skill.lkml.bak', 'x')
        self.put_local('views/orders.view.lkml', 'view: orders { # edit\n}\n')
        self.run_cli('push')
        sent = [b['path'] for m, p, _, b in self.fake.log if m in ('POST', 'PUT') and p.endswith('/files')]
        self.assertEqual(sent, ['views/orders.view.lkml'])

    def test_dry_run_and_status_write_nothing(self):
        self.init()
        self.put_local('views/orders.view.lkml', 'view: orders { # edit\n}\n')
        self.put_local('views/new.view.lkml', 'view: new {}\n')
        state = self.local('.looker-sync/state.json')
        before = len(self.writes())
        for args in (['status'], ['push', '--dry-run'], ['pull', '--dry-run']):
            self.assertEqual(self.run_cli(*args).returncode, 0)
        self.assertEqual(len(self.writes()), before)
        self.assertEqual(self.local('.looker-sync/state.json'), state)

    def test_readback_mismatch_is_an_error(self):
        self.init()
        self.put_local('views/orders.view.lkml', 'view: orders { # edit\n}\n')
        self.fake.mangle_readback.add('views/orders.view.lkml')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('MISMATCH views/orders.view.lkml', r.stdout)

    def test_validation_errors_exit_3_with_file_and_line(self):
        self.init()
        self.fake.validation_errors = [{'severity': 'error', 'message': 'Unknown view', 'file_path': 'demo/models/demo.model.lkml', 'line_number': 3}]
        self.put_local('views/orders.view.lkml', 'view: orders { # edit\n}\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('demo/models/demo.model.lkml:3  Unknown view', r.stdout)

    def test_push_refused_in_prod_mode(self):
        r = self.run_cli('init', '--project', PROJECT, '--mode', 'prod')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.put_local('manifest.lkml', 'project_name: "edited"\n')
        before = len(self.writes())
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('push is refused in prod mode', r.stdout)
        self.assertEqual(len(self.writes()), before)

    # ---- branch guards and switch

    def test_branch_mismatch_aborts(self):
        self.init()
        self.fake.dev_branch = 'master'
        self.put_local('views/orders.view.lkml', 'view: orders { # edit\n}\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("config says 'feat', Looker dev workspace is on 'master'", r.stdout)
        self.assertEqual(self.writes(), [])

    def test_switch_refused_by_uncommitted_git_status(self):
        self.init()
        self.fake.dev_write('models/demo.model.lkml', 'connection: "other"\n')
        r = self.run_cli('switch', '--branch', 'master', '--yes')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('models/demo.model.lkml  (Modified)', r.stdout)
        self.assertEqual(self.fake.dev_branch, 'feat')

    def test_switch_without_terminal_or_flags_fails(self):
        self.init()
        self.put_local('views/orders.view.lkml', 'view: orders { # unpushed\n}\n')
        r = self.run_cli('switch', '--branch', 'master')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('--local-changes', r.stdout)
        self.put_local('views/orders.view.lkml', 'view: orders {}\n')
        r = self.run_cli('switch', '--branch', 'master')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('--yes', r.stdout)
        self.assertEqual(self.fake.dev_branch, 'feat')

    def test_switch_backs_up_unpushed_changes_and_mirrors_target(self):
        self.init()
        self.put_local('views/orders.view.lkml', 'view: orders { # unpushed\n}\n')
        r = self.run_cli('switch', '--branch', 'master', '--local-changes', 'backup', '--yes')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.fake.dev_branch, 'master')
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'views/orders.view.lkml')))
        backups = os.path.join(self.dir, '.looker-sync/backups')
        [b] = os.listdir(backups)
        self.assertTrue(b.startswith('feat-'))
        with open(os.path.join(backups, b, 'views/orders.view.lkml')) as f:
            self.assertEqual(f.read(), 'view: orders { # unpushed\n}\n')
        self.assertEqual(json.loads(self.local('.looker-sync/config.json'))['branch'], 'master')
        self.assertFalse(any(b and 'ref' in b for m, p, _, b in self.fake.log if p.endswith('/git_branch')))

    def test_switch_never_creates_a_branch(self):
        self.init()
        r = self.run_cli('switch', '--branch', 'nope', '--yes')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertNotIn('nope', self.fake.branches)

    def test_switch_to_prod_and_back(self):
        self.init()
        r = self.run_cli('switch', '--mode', 'prod', '--yes')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'views/orders.view.lkml')))
        self.assertEqual(self.run_cli('status').returncode, 0)
        r = self.run_cli('switch', '--mode', 'dev', '--yes')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders {}\n')


class GitTestCase(FakeLookerTestCase):
    """The project folder is a real clone of a bare remote, on the fake's dev branch.

    self.repos is the GitRepos behind the fake; every git process (the CLI's too) runs with
    hermetic_git_env through os.environ for the length of the test. The CLI appends one JSON
    line per git call to self.git_log_path (see run_git in cli.py); git_calls() reads it.
    """

    def setUp(self):
        super().setUp()
        self.git_log_path = os.path.join(self.tmp.name, 'git-calls.jsonl')
        os.environ['LOOKER_SYNC_GIT_LOG'] = self.git_log_path

    def git_calls(self):
        if not os.path.exists(self.git_log_path):
            return []
        with open(self.git_log_path) as f:
            return [json.loads(line)['args'] for line in f]

    def config(self):
        return json.loads(self.local('.looker-sync/config.json'))

    def make_project_dir(self):
        patcher = mock.patch.dict(os.environ, hermetic_git_env(self.tmp.name), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repos = GitRepos(self.tmp.name, self.fake)
        self.repos.clone(self.dir, self.fake.dev_branch)

    def git(self, *args):
        """Run git in the project folder; fail the test if it fails; return stdout."""
        return GitRepos.git(*args, cwd=self.dir)

    def git_try(self, *args):
        return subprocess.run(['git', *args], cwd=self.dir, capture_output=True, text=True, timeout=60)

    def looker_get(self, path, mode='dev'):
        """GET a fake Looker resource as a fresh dev-mode token (to read what the CLI would see)."""
        def call(method, p, data=None, form=False, token=None):
            body = (urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()) if data else None
            req = urllib.request.Request(self.url + '/api/4.0' + p, data=body, method=method)
            if token:
                req.add_header('Authorization', f'token {token}')
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b'null')
        tok = call('POST', '/login', {'client_id': 'id', 'client_secret': SECRET}, form=True)['access_token']
        call('PATCH', '/session', {'workspace_id': 'dev' if mode == 'dev' else 'production'}, token=tok)
        return call('GET', path, token=tok)


class GitHarnessTest(GitTestCase):
    def test_harness_clone_is_a_work_tree_and_looker_ref_resolves_after_fetch(self):
        self.assertEqual(self.git('rev-parse', '--is-inside-work-tree'), 'true')
        self.assertEqual(self.git('branch', '--show-current'), 'feat')
        info = self.looker_get(f'/projects/{PROJECT}/git_branch')
        self.assertEqual(info['name'], 'feat')
        self.assertEqual(self.git('rev-parse', 'HEAD'), info['ref'])
        # the commit's tree is exactly the fake's files for the branch
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', info['ref']).split(),
                         sorted(self.fake.branches['feat']))
        self.assertEqual(self.git('show', info['ref'] + ':views/orders.view.lkml') + '\n', 'view: orders {}\n')
        # Looker moves on and pushes; the new ref resolves in the clone only after a fetch
        files = dict(self.fake.branches['feat'], **{'views/users.view.lkml': 'view: users {}\n'})
        sha = self.repos.commit('feat', files)
        info = self.looker_get(f'/projects/{PROJECT}/git_branch')
        self.assertEqual((info['ref'], info['remote_ref'], info['ahead_count'], info['behind_count']), (sha, sha, 0, 0))
        self.assertNotEqual(self.git_try('cat-file', '-e', info['ref'] + '^{commit}').returncode, 0)
        self.git('fetch', '-q', 'origin')
        self.assertEqual(self.git_try('cat-file', '-e', info['ref'] + '^{commit}').returncode, 0)
        self.assertEqual(self.git('rev-parse', 'origin/feat'), sha)
        self.assertEqual(self.fake.dev_files()['views/users.view.lkml'], 'view: users {}\n')

    def test_harness_git_is_hermetic(self):
        self.assertEqual(os.environ['GIT_CONFIG_NOSYSTEM'], '1')
        self.assertTrue(os.environ['GIT_CONFIG_GLOBAL'].startswith(self.tmp.name))
        self.assertEqual(self.git('config', 'commit.gpgSign'), 'false')
        self.assertEqual(self.git('var', 'GIT_AUTHOR_IDENT').split('<')[0].strip(), 'Test Author')
        hooks = os.path.join(self.dir, '.git', 'hooks')
        self.assertFalse(os.path.isdir(hooks) and os.listdir(hooks))   # no templates copied in
        origins = {line.split('\t')[0] for line in self.git('config', '--show-origin', '--list').splitlines()}
        self.assertEqual(origins, {'file:' + os.environ['GIT_CONFIG_GLOBAL'], 'file:.git/config'})

    def test_fake_serves_live_git_fields_and_the_helper_keeps_them_in_step(self):
        proj = self.looker_get(f'/projects/{PROJECT}')
        self.assertEqual((proj['uses_git'], proj['git_service_name'], proj['git_production_branch_name']),
                         (True, 'github', 'master'))
        self.assertTrue(proj['git_remote_url'].startswith('git@'))
        # live: a Looker-hosted bare repository is a relative path two levels up, no service name
        self.fake.set_git_remote('bare')
        proj = self.looker_get(f'/projects/{PROJECT}')
        self.assertEqual((proj['uses_git'], proj['git_remote_url'], proj['git_service_name']),
                         (True, f'../../bare_models/{PROJECT}.git', None))
        self.fake.set_git_remote('none')
        proj = self.looker_get(f'/projects/{PROJECT}')
        self.assertEqual((proj['uses_git'], proj['git_remote_url'], proj['git_service_name']), (False, None, None))
        # Looker commits without pushing: ref is not in the remote, remote_ref is, ahead_count 1
        old = self.fake.branch_git['feat']['ref']
        sha = self.repos.commit('feat', dict(self.fake.branches['feat'], **{'x.view.lkml': 'view: x {}\n'}), push=False)
        info = self.looker_get(f'/projects/{PROJECT}/git_branch')
        self.assertEqual((info['ref'], info['remote_ref'], info['ahead_count'], info['behind_count']), (sha, old, 1, 0))
        self.git('fetch', '-q', 'origin')
        self.assertNotEqual(self.git_try('cat-file', '-e', sha + '^{commit}').returncode, 0)
        self.assertEqual(self.git_try('cat-file', '-e', old + '^{commit}').returncode, 0)
        # a branch pushed with plain git is listed remote-only, and checkout by name makes it local
        review = self.repos.add_remote_only_branch('review', {'manifest.lkml': 'project_name: "demo"\n'})
        listed = {b['name']: b for b in self.looker_get(f'/projects/{PROJECT}/git_branches')}
        self.assertEqual((listed['review']['is_local'], listed['review']['is_remote'], listed['review']['ref'],
                          listed['review']['remote_ref'], listed['review']['ahead_count']), (False, True, '', review, None))
        self.assertEqual((listed['master']['readonly'], listed['master']['is_production']), (True, True))
        self.assertEqual((listed['feat']['personal'], listed['feat']['readonly']), (True, False))
        self.git('fetch', '-q', 'origin')
        self.assertEqual(self.git('rev-parse', 'origin/review'), review)
        r = self.run_cli('init', '--project', PROJECT, '--branch', 'feat')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_cli('switch', '--branch', 'review', '--yes')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        listed = {b['name']: b for b in self.looker_get(f'/projects/{PROJECT}/git_branches')}
        self.assertEqual((listed['review']['is_local'], listed['review']['ref']), (True, review))
        # live: the current-branch resource lags behind the list right after that checkout
        info = self.looker_get(f'/projects/{PROJECT}/git_branch')
        self.assertEqual((info['name'], info['is_local'], info['ref'], info['remote_ref']), ('review', False, '', review))


class GitLinkTest(GitTestCase):
    """link, the init offer and the commit check: one user story per test."""

    LOOKER_URL = GIT_REMOTE_SHAPES['remote'][1]

    def init_unlinked(self):
        r = self.run_cli('init', '--project', PROJECT, '--branch', 'feat', '--no-git')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def link(self, *args):
        r = self.run_cli('link', *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def on_terminal(self, args, answers):
        master, slave = pty.openpty()
        p = subprocess.Popen([sys.executable, CLI, '-C', self.dir, *args],
                             stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        os.write(master, answers.encode())
        chunks = b''
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            chunks += data
        os.close(master)
        text = chunks.decode(errors='replace')
        self.assertEqual(p.wait(timeout=60), 0, text)
        self.assertNotIn(SECRET, text)
        return text

    def reclone_from_another_repository(self):
        """Replace the project folder with a clone of an unrelated repository (same .env)."""
        src, other = os.path.join(self.tmp.name, 'other-src'), os.path.join(self.tmp.name, 'other.git')
        GitRepos.git('init', '-q', src)
        with open(os.path.join(src, 'README.md'), 'w') as f:
            f.write('an unrelated repository\n')
        GitRepos.git('-C', src, 'add', '-A')
        GitRepos.git('-C', src, 'commit', '-q', '-m', 'unrelated')
        GitRepos.git('clone', '-q', '--bare', src, other)
        with open(os.path.join(self.dir, '.env')) as f:
            env = f.read()
        shutil.rmtree(self.dir)
        GitRepos.git('clone', '-q', other, self.dir)
        with open(os.path.join(self.dir, '.env'), 'w') as f:
            f.write(env)
        return other

    # ---- link

    def test_link_on_a_matching_clone_writes_git_remote(self):
        self.fake.set_git_remote('https')   # the protocol Looker uses does not matter
        self.init_unlinked()
        self.assertNotIn('git_remote', self.config())
        r = self.link()
        self.assertIn("linked to git remote 'origin'", r.stdout)
        self.assertEqual(self.config()['git_remote'], 'origin')
        self.assertIn(['fetch', '--quiet', 'origin'], self.git_calls())
        # every git call went through the one runner, and only reads and fetch were run
        reads = {'rev-parse', 'symbolic-ref', 'status', 'cat-file', 'merge-base', 'remote', 'fetch', 'for-each-ref'}
        self.assertTrue(self.git_calls())
        self.assertEqual({a[0] for a in self.git_calls()} - reads, set())
        self.assertEqual(self.run_cli('status').returncode, 0)

    def test_link_refuses_projects_with_nothing_to_clone(self):
        self.init_unlinked()
        cases = [('none', None, 'does not use git'),
                 ('bare', None, 'Looker-hosted bare repository'),
                 ('remote', 'null', 'no remote repository')]
        for shape, url, reason in cases:
            with self.subTest(shape=shape, url=url):
                self.fake.set_git_remote(shape)
                if url == 'null':
                    self.fake.project_git['git_remote_url'] = None
                r = self.run_cli('link')
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn(reason, r.stdout)
                self.assertNotIn('git_remote', self.config())

    def test_link_refuses_a_remote_name_that_does_not_exist(self):
        self.init_unlinked()
        r = self.run_cli('link', '--remote', 'upstream')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("no git remote named 'upstream'", r.stdout)
        self.assertNotIn('git_remote', self.config())

    def test_link_refuses_a_clone_of_another_repository_showing_both_urls(self):
        other = self.reclone_from_another_repository()
        self.init_unlinked()
        r = self.run_cli('link')
        self.assertEqual(r.returncode, 2, r.stdout)
        line_looker = next(l for l in r.stdout.splitlines() if self.LOOKER_URL in l)
        line_local = next(l for l in r.stdout.splitlines() if other in l)
        self.assertIn("Looker's remote", line_looker)
        self.assertIn("remote 'origin'", line_local)
        self.assertNotIn('git_remote', self.config())

    def test_link_refuses_outside_a_git_work_tree(self):
        self.init_unlinked()
        shutil.rmtree(os.path.join(self.dir, '.git'))
        r = self.run_cli('link')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('not a git work tree', r.stdout)

    # ---- the commit check

    def test_commit_check_falls_back_to_remote_ref_then_warns_when_looker_is_ahead(self):
        self.init_unlinked()
        # Looker committed without pushing: ref is not in the clone, remote_ref is
        files = dict(self.fake.branches['feat'], **{'views/a.view.lkml': 'view: a {}\n'})
        first = self.repos.commit('feat', files, push=False)
        r = self.link()
        self.assertNotIn('warning', r.stdout)
        # neither ref nor remote_ref is in the clone (Looker's idea of its remote is stale), and
        # Looker reports unpushed commits: a valid link, so warn instead of refusing
        self.repos.commit('feat', dict(files, **{'views/b.view.lkml': 'view: b {}\n'}), push=False)
        self.fake.branch_git['feat']['remote_ref'] = first
        self.assertEqual(self.fake.branch_resource('feat')['ahead_count'], 2)
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('warning', r.stdout)
        self.assertIn('2 commit(s) Looker has not pushed', r.stdout)

    def test_every_command_in_a_linked_folder_fetches_and_runs_the_commit_check(self):
        self.init_unlinked()
        self.link()
        # Looker moves on and pushes: the next command fetches, so the new commit is found
        sha = self.repos.commit('feat', dict(self.fake.branches['feat'], **{'views/n.view.lkml': 'view: n {}\n'}))
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.git_try('cat-file', '-e', sha + '^{commit}').returncode, 0)
        # Looker's commits are nowhere in the clone and Looker is not ahead: refuse everywhere
        stray = self.repos.commit('feat', dict(self.fake.branches['feat'], **{'x.view.lkml': 'view: x {}\n'}), push=False)
        self.fake.branch_git['feat'].update(remote_ref=stray, ahead_count=0)
        for args in (['status'], ['pull'], ['push'], ['switch', '--branch', 'master', '--yes']):
            with self.subTest(args=args):
                before = len(self.writes())
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn(stray[:12], r.stdout)
                self.assertIn(self.LOOKER_URL, r.stdout)
                self.assertIn(self.repos.remote, r.stdout)
                self.assertEqual(len(self.writes()), before)
        self.assertEqual(self.fake.dev_branch, 'feat')

    def test_fetch_failure_warns_and_the_command_continues(self):
        self.init_unlinked()
        self.link()
        self.git('remote', 'set-url', 'origin', os.path.join(self.tmp.name, 'gone.git'))
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('warning: git fetch origin failed', r.stdout)
        self.assertIn('status:', r.stdout)

    def test_a_second_remote_without_lookers_commit_refuses_even_though_the_clone_has_it(self):
        # a fork copied before Looker's next commit: same history, but it lacks that commit
        fork = os.path.join(self.tmp.name, 'fork.git')
        GitRepos.git('clone', '-q', '--bare', self.repos.remote, fork)
        self.init_unlinked()
        sha = self.repos.commit('feat', dict(self.fake.branches['feat'], **{'views/n.view.lkml': 'view: n {}\n'}))
        self.git('fetch', '-q', 'origin')
        self.git('remote', 'add', 'fork', fork)
        self.assertEqual(self.git_try('cat-file', '-e', sha + '^{commit}').returncode, 0)
        # the commit is in the clone (from origin), so only "which remote has it" can refuse
        r = self.run_cli('link', '--remote', 'fork')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn(sha[:12], r.stdout)
        line_looker = next(l for l in r.stdout.splitlines() if self.LOOKER_URL in l)
        line_local = next(l for l in r.stdout.splitlines() if fork in l)
        self.assertIn("Looker's remote", line_looker)
        self.assertIn("remote 'fork'", line_local)
        self.assertNotIn('git_remote', self.config())
        # the remote that has the commit still links, with a second remote present
        r = self.link('--remote', 'origin')
        self.assertNotIn('warning', r.stdout)
        self.assertEqual(self.config()['git_remote'], 'origin')
        calls = self.git_calls()
        self.assertTrue(any(a[0] == 'for-each-ref' and a[-1] == 'refs/remotes/origin/' for a in calls))
        self.assertTrue(any(a[0] == 'for-each-ref' and a[-1] == 'refs/remotes/fork/' for a in calls))
        self.assertEqual({a[0] for a in calls} - {'rev-parse', 'symbolic-ref', 'status', 'cat-file',
                                                   'merge-base', 'remote', 'fetch', 'for-each-ref'}, set())
        # a linked folder whose remote stops having Looker's commit refuses on every command
        self.git('remote', 'set-url', 'origin', fork)
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn(fork, r.stdout)

    def test_the_containment_read_is_the_only_new_git_form_and_write_forms_stay_refused(self):
        probe = ('import json, sys\n'
                 'from looker_sync.cli import run_git\n'
                 'for args in json.loads(sys.argv[2]):\n'
                 '    try:\n'
                 '        rc, _, _ = run_git(sys.argv[1], *args)\n'
                 '        print("ran", rc)\n'
                 '    except RuntimeError:\n'
                 '        print("refused")\n')
        forms = [['for-each-ref', '--count=1', '--format=%(refname)', '--contains', 'HEAD', 'refs/remotes/origin/'],
                 ['for-each-ref', '--format=%(refname)', 'refs/'],
                 ['branch', '-r', '--contains', 'HEAD'],
                 ['branch', '-D', 'feat'],
                 ['update-ref', 'refs/heads/x', 'HEAD'],
                 ['remote', 'add', 'x', 'y'],
                 ['tag', 'x']]
        env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(CLI), '..', 'src'))
        r = subprocess.run([sys.executable, '-c', probe, self.dir, json.dumps(forms)],
                           capture_output=True, text=True, timeout=60, env=env, cwd=self.tmp.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['ran 0'] + ['refused'] * 6)
        # a refused form never starts a git process: only the read is in the call log
        self.assertEqual(self.git_calls(), [forms[0]])
        self.assertEqual(self.git('branch', '--list', 'x'), '')

    # ---- no link, no git

    def test_without_git_remote_no_git_process_is_ever_spawned(self):
        shim = os.path.join(self.tmp.name, 'shim')
        os.makedirs(shim)
        spawned = os.path.join(self.tmp.name, 'shim-calls')
        with open(os.path.join(shim, 'git'), 'w') as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {spawned}\nexit 97\n')
        os.chmod(os.path.join(shim, 'git'), 0o755)
        env = dict(os.environ, PATH=shim + os.pathsep + os.environ['PATH'])
        steps = [['init', '--project', PROJECT, '--branch', 'feat', '--no-git'], ['status'], ['pull'],
                 ['switch', '--branch', 'master', '--yes'], ['switch', '--branch', 'feat', '--yes'],
                 'edit', ['push'], ['status']]
        for step in steps:
            if step == 'edit':
                self.put_local('views/orders.view.lkml', 'view: orders { # edit\n}\n')
                continue
            r = self.run_cli(*step, env=env)
            self.assertEqual(r.returncode, 0, step + [r.stdout])
        # a folder that is not a git work tree: init's default spawns no git either
        blank = os.path.join(self.tmp.name, 'blank')
        os.makedirs(blank)
        r = subprocess.run([sys.executable, CLI, '-C', blank, '--env-file', os.path.join(self.dir, '.env'),
                            'init', '--project', PROJECT], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('git', [l.split(':')[0] for l in r.stdout.splitlines()])
        # the read-only push guard reads Looker only
        self.fake.readonly.add('feat')
        self.put_local('views/orders.view.lkml', 'view: orders { # again\n}\n')
        self.assertEqual(self.run_cli('push', env=env).returncode, 2)
        self.assertFalse(os.path.exists(spawned), 'git was spawned')
        self.assertEqual(self.git_calls(), [])

    # ---- the init offer

    def test_init_in_a_git_clone_without_a_terminal_links_origin_and_says_so(self):
        r = self.init()
        self.assertIn("git: linked to git remote 'origin'", r.stdout)
        self.assertIn('--no-git', r.stdout)
        self.assertEqual(self.config()['git_remote'], 'origin')
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders {}\n')

    def test_init_on_a_clone_off_the_looker_branch_links_but_its_first_pull_hits_the_drift_guard(self):
        self.git('checkout', '-q', '-b', 'master', 'origin/master')   # the clone is not on Looker's branch
        r = self.run_cli('init', '--project', PROJECT, '--branch', 'feat')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.config()['git_remote'], 'origin')
        self.assertIn('looker-sync switch --branch feat --force', r.stdout)
        # nothing of feat was written onto master's checkout
        self.assertEqual(self.git('status', '--porcelain', '--', '.', ':(exclude).looker-sync', ':(exclude).env',
                                  ':(exclude).gitignore'), '')

    def test_init_no_git_says_it_did_not_link(self):
        r = self.init_unlinked()
        self.assertIn('git: not linked (--no-git)', r.stdout)
        self.assertIn('looker-sync link', r.stdout)
        self.assertEqual(self.git_calls(), [])

    def test_init_default_on_an_ineligible_project_continues_unlinked_with_the_reason(self):
        self.fake.set_git_remote('bare')
        r = self.init()
        self.assertIn('git: not linked', r.stdout)
        self.assertIn('Looker-hosted bare repository', r.stdout)
        self.assertNotIn('git_remote', self.config())
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders {}\n')

    def test_init_default_on_a_clone_of_another_repository_continues_unlinked_showing_both_urls(self):
        other = self.reclone_from_another_repository()
        r = self.init()
        self.assertIn('git: not linked', r.stdout)
        self.assertIn(self.LOOKER_URL, r.stdout)
        self.assertIn(other, r.stdout)
        self.assertNotIn('git_remote', self.config())

    def test_init_with_an_explicit_git_remote_refuses_when_it_cannot_link(self):
        r = self.run_cli('init', '--project', PROJECT, '--branch', 'feat', '--git-remote', 'upstream')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("no git remote named 'upstream'", r.stdout)
        self.assertNotIn('git_remote', self.config())
        self.git('remote', 'add', 'upstream', self.repos.remote)
        r = self.run_cli('init', '--project', PROJECT, '--branch', 'feat', '--git-remote', 'upstream')
        self.assertEqual(r.returncode, 0, r.stdout)   # init resumes and links
        self.assertEqual(self.config()['git_remote'], 'upstream')

    def test_init_on_a_terminal_offers_the_link(self):
        text = self.on_terminal(['init', '--project', PROJECT, '--branch', 'feat'], '\ny\n')
        self.assertIn('Link this folder', text)
        self.assertEqual(self.config()['git_remote'], 'origin')
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'models', 'demo.model.lkml')))

    def test_init_on_a_terminal_can_decline_the_link(self):
        text = self.on_terminal(['init', '--project', PROJECT, '--branch', 'feat'], 'n\ny\n')
        self.assertIn('git: not linked', text)
        self.assertNotIn('git_remote', self.config())
        self.assertNotIn(['fetch', '--quiet', 'origin'], self.git_calls())


class GitSwitchTest(GitTestCase):
    """switch in a linked folder: Looker and the local git branch move together."""

    NEVER = {'commit', 'push', 'pull', 'reset', 'rebase', 'clean'}

    def setUp(self):
        super().setUp()
        r = self.init()   # a clone without a terminal: init links origin
        self.assertEqual(self.config()['git_remote'], 'origin', r.stdout)

    def tearDown(self):
        calls = self.git_calls()
        try:
            self.assertEqual([a for a in calls if a[0] in self.NEVER], [])
            self.assertEqual([a for a in calls if a[0] == 'stash' and a[1:2] != ['push']], [])
        finally:
            super().tearDown()

    def switch(self, *args, code=0):
        r = self.run_cli('switch', *args)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return r

    def head(self):
        return self.git('branch', '--show-current')

    def stashes(self):
        # git prefixes a -m message with "On <branch>: " in the stash subject; return the message
        return [s.split(': ', 1)[1] if s.startswith('On ') else s
                for s in self.git('stash', 'list', '--format=%s').splitlines()]

    def test_forced_switch_stashes_dirty_tree_and_lands_both_sides_on_the_target(self):
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')   # modified, tracked
        self.put_local('views/draft.view.lkml', 'view: draft {}\n')               # untracked, synced
        self.put_local('notes.txt', 'my notes\n')                                # untracked, not synced
        os.makedirs(os.path.join(self.dir, '.git', 'info'), exist_ok=True)   # no templates copied in
        with open(os.path.join(self.dir, '.git', 'info', 'exclude'), 'a') as f:
            f.write('scratch.log\n')
        self.put_local('scratch.log', 'ignored\n')
        r = self.switch('--branch', 'master', '--force')
        self.assertEqual(self.fake.dev_branch, 'master')
        self.assertEqual(self.head(), 'master')
        self.assertEqual(self.config()['branch'], 'master')
        [stash] = self.stashes()
        self.assertRegex(stash, r'^looker-sync feat \d{8}T\d{6}$')
        self.assertIn(stash, r.stdout)
        self.assertIn('git stash pop', r.stdout)
        self.assertEqual(self.local('scratch.log'), 'ignored\n')                  # ignored: untouched
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'notes.txt')))    # stashed
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'views/draft.view.lkml')))
        self.assertFalse(os.path.exists(os.path.join(self.dir, '.looker-sync', 'backups')))
        self.assertEqual(self.local('manifest.lkml'), 'project_name: "demo"\n')
        self.assertEqual(self.git('status', '--porcelain', '--', '.', ':(exclude).looker-sync', ':(exclude).env'), '')
        # the stash holds the work: modified file in the stash commit, untracked ones in its third parent
        self.assertIn('# mine', self.git('show', 'stash@{0}:views/orders.view.lkml'))
        self.assertEqual(sorted(self.git('ls-tree', '-r', '--name-only', 'stash@{0}^3').split()),
                         ['.gitignore', 'notes.txt', 'views/draft.view.lkml'])
        # .looker-sync/ is in .gitignore, so only the env file is named: an exclude naming an
        # ignored path makes git stash fail halfway
        self.assertIn(['stash', 'push', '--include-untracked', '-m', stash, '--', '.',
                       ':(exclude,literal).env'], self.git_calls())
        self.assertFalse(any(b and 'ref' in b for m, p, _, b in self.fake.log if p.endswith('/git_branch')))

    def test_stash_never_takes_the_metadata_folder_or_the_env_file(self):
        os.remove(os.path.join(self.dir, '.gitignore'))   # nothing tells git to ignore them
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        self.switch('--branch', 'master', '--force')
        [stash] = self.stashes()
        self.assertIn(['stash', 'push', '--include-untracked', '-m', stash, '--', '.',
                       ':(exclude,literal).looker-sync', ':(exclude,literal).env'], self.git_calls())
        for p in ('.looker-sync/config.json', '.looker-sync/state.json', '.env'):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, p)), p)
        self.assertNotIn('.looker-sync', self.git('ls-tree', '-r', '--name-only', 'stash@{0}^3'))
        self.assertNotIn('.env', self.git('ls-tree', '-r', '--name-only', 'stash@{0}^3').split())
        self.assertEqual(self.run_cli('status').returncode, 0)

    def test_switch_to_a_branch_with_no_local_branch_creates_it_tracking_the_remote(self):
        self.assertNotEqual(self.git_try('rev-parse', '--verify', '-q', 'refs/heads/master').returncode, 0)
        self.switch('--branch', 'master', '--force')
        self.assertEqual(self.head(), 'master')
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', 'master@{upstream}'), 'origin/master')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.fake.branch_git['master']['ref'])

    def test_local_branch_behind_looker_is_fast_forwarded(self):
        self.switch('--branch', 'master', '--force')
        old = self.git('rev-parse', 'feat')
        sha = self.repos.commit('feat', dict(self.fake.branches['feat'], **{'views/n.view.lkml': 'view: n {}\n'}))
        self.switch('--branch', 'feat', '--force')
        self.assertEqual(self.head(), 'feat')
        self.assertEqual(self.git('rev-parse', 'HEAD'), sha)
        self.assertNotEqual(old, sha)
        self.assertEqual(self.git('status', '--porcelain', '--', '.', ':(exclude).looker-sync', ':(exclude).env'), '')
        self.assertIn(['merge', '--ff-only', '--quiet', sha], self.git_calls())

    def test_first_checkout_of_a_remote_only_branch_fast_forwards_a_local_branch_behind_it(self):
        # live: Looker answers ref '' for a branch it just checked out from the remote, so the
        # remote's commit is the one Looker is on
        old = self.repos.add_remote_only_branch('review', {'manifest.lkml': 'project_name: "demo"\n'})
        self.git('fetch', '-q', 'origin')
        self.git('branch', '-q', '--track', 'review', 'origin/review')
        sha = self.repos.commit('review', {'manifest.lkml': 'project_name: "demo"\n', 'views/r.view.lkml': 'view: r {}\n'})
        self.switch('--branch', 'review', '--force')
        self.assertEqual(self.head(), 'review')
        self.assertEqual(self.git('rev-parse', 'HEAD'), sha)
        self.assertNotEqual(old, sha)
        self.assertIn(['merge', '--ff-only', '--quiet', sha], self.git_calls())

    def test_diverged_local_branch_is_left_alone_with_a_warning(self):
        self.switch('--branch', 'master', '--force')
        # a local commit on feat that Looker does not have, and a Looker commit the clone lacks
        tree = self.git('rev-parse', 'feat^{tree}')
        mine = self.git('commit-tree', tree, '-p', 'feat', '-m', 'local work')
        self.git('update-ref', 'refs/heads/feat', mine)
        theirs = self.repos.commit('feat', dict(self.fake.branches['feat'], **{'views/t.view.lkml': 'view: t {}\n'}))
        r = self.switch('--branch', 'feat', '--force')
        self.assertEqual(self.head(), 'feat')
        self.assertEqual(self.git('rev-parse', 'HEAD'), mine)
        self.assertIn('warning', r.stdout)
        self.assertIn('diverged', r.stdout)
        self.assertIn(theirs[:12], r.stdout)
        self.assertFalse([a for a in self.git_calls() if a[0] == 'merge'])
        self.assertEqual(self.local('views/t.view.lkml'), 'view: t {}\n')    # Looker's files mirrored

    def test_uncommitted_files_in_looker_refuse_even_with_force_and_git_does_not_move(self):
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        self.fake.dev_write('models/demo.model.lkml', 'connection: "other"\n')
        r = self.switch('--branch', 'master', '--force', code=2)
        self.assertIn('models/demo.model.lkml  (Modified)', r.stdout)
        self.assertEqual((self.fake.dev_branch, self.head(), self.stashes()), ('feat', 'feat', []))
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders { # mine\n}\n')
        self.assertFalse([a for a in self.git_calls() if a[0] in ('stash', 'checkout', 'merge')])

    def test_failed_looker_checkout_leaves_the_git_branch_where_it_was(self):
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        self.fake.fail_checkout.add('master')
        r = self.switch('--branch', 'master', '--force', code=1)
        self.assertIn('checkout failed', r.stdout)
        self.assertEqual((self.fake.dev_branch, self.head(), self.stashes()), ('feat', 'feat', []))
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders { # mine\n}\n')
        self.assertEqual(self.config()['branch'], 'feat')
        self.assertFalse([a for a in self.git_calls() if a[0] in ('stash', 'checkout', 'merge')])

    def test_remote_only_branch_is_reachable_and_a_local_only_branch_refuses(self):
        review = self.repos.add_remote_only_branch('review', {'manifest.lkml': 'project_name: "demo"\n',
                                                              'views/r.view.lkml': 'view: r {}\n'})
        self.switch('--branch', 'review', '--force')
        self.assertEqual((self.fake.dev_branch, self.head()), ('review', 'review'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), review)
        self.assertEqual(self.local('views/r.view.lkml'), 'view: r {}\n')
        self.git('branch', 'mine')   # made with git, never pushed, unknown to Looker
        r = self.switch('--branch', 'mine', '--force', code=2)
        self.assertIn('push it', r.stdout)
        self.assertEqual((self.fake.dev_branch, self.head()), ('review', 'review'))
        self.assertNotIn('mine', self.fake.branches)

    def test_switch_to_prod_checks_out_the_production_branch_in_git(self):
        self.fake.production_branch = 'master'
        self.switch('--mode', 'prod', '--force')
        self.assertEqual(self.head(), 'master')
        self.assertEqual(self.config()['mode'], 'prod')
        self.assertEqual(self.git('status', '--porcelain', '--', '.', ':(exclude).looker-sync', ':(exclude).env'), '')
        self.assertEqual(self.run_cli('status').returncode, 0)
        r = self.switch('--mode', 'prod', '--force')
        self.assertIn('nothing to switch', r.stdout)
        self.switch('--mode', 'dev', '--force')
        self.assertEqual(self.head(), 'feat')
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders {}\n')

    def test_a_stash_taken_off_a_drifted_branch_points_back_to_that_git_branch(self):
        self.git('checkout', '-q', '-b', 'master', 'origin/master')   # by hand, behind looker-sync's back
        self.put_local('notes.txt', 'my notes\n')
        r = self.switch('--branch', 'feat', '--force')
        [stash] = self.stashes()
        self.assertRegex(stash, r'^looker-sync master ')
        self.assertIn('git checkout master', r.stdout)
        self.assertNotIn('looker-sync switch --branch feat --force, find', r.stdout)

    def test_git_drift_is_fixed_by_switch_instead_of_nothing_to_do(self):
        self.git('checkout', '-q', '-b', 'master', 'origin/master')   # by hand, behind looker-sync's back
        r = self.switch('--branch', 'feat', '--force')
        self.assertNotIn('nothing to switch', r.stdout)
        self.assertEqual((self.fake.dev_branch, self.head()), ('feat', 'feat'))
        self.assertEqual(self.local('views/orders.view.lkml'), 'view: orders {}\n')
        self.assertFalse([e for e in self.fake.log if e[0] == 'PUT' and e[1].endswith('/git_branch')])
        r = self.switch('--branch', 'feat', '--force')
        self.assertIn('nothing to switch', r.stdout)
        self.git('checkout', '-q', '--detach')   # a detached HEAD is drift too
        r = self.switch('--branch', 'feat', '--force')
        self.assertNotIn('nothing to switch', r.stdout)
        self.assertEqual(self.head(), 'feat')

    def test_force_is_backup_plus_yes_and_without_it_the_prompts_still_guard(self):
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        r = self.switch('--branch', 'master', code=2)
        self.assertIn('--local-changes', r.stdout)
        r = self.switch('--branch', 'master', '--local-changes', 'backup', code=2)
        self.assertIn('--yes', r.stdout)
        self.assertEqual((self.fake.dev_branch, self.head(), self.stashes()), ('feat', 'feat', []))
        self.switch('--branch', 'master', '--local-changes', 'backup', '--yes')
        self.assertEqual(self.head(), 'master')
        self.assertEqual(len(self.stashes()), 1)

    def test_mirror_relists_after_the_git_checkout_and_tolerates_files_it_removed(self):
        # pushed with plain git onto master: in the checkout, not in Looker's files
        self.repos.commit('master', dict(self.fake.branches['master'], **{'views/g.view.lkml': 'view: g {}\n'}),
                          looker=False)
        r = self.switch('--branch', 'master', '--force')
        # the checkout already removed feat's models and views; only what it left was listed and removed
        self.assertIn('removed 2 old local file(s)', r.stdout)
        for p in ('models/demo.model.lkml', 'views/orders.view.lkml', 'views/g.view.lkml'):
            self.assertFalse(os.path.exists(os.path.join(self.dir, p)), p)
        self.assertEqual(self.local('manifest.lkml'), 'project_name: "demo"\n')
        self.assertEqual(self.run_cli('status').returncode, 0)

    def test_push_choice_is_refused_while_git_has_drifted(self):
        self.git('checkout', '-q', '-b', 'master', 'origin/master')   # by hand: feat's files are gone
        r = self.switch('--branch', 'feat', '--local-changes', 'push', '--yes', code=2)
        self.assertIn('belong to another branch', r.stdout)
        self.assertFalse([e for e in self.fake.log if e[0] in ('POST', 'PUT', 'DELETE') and '/files' in e[1]])
        self.assertEqual(sorted(self.fake.dev_files()), sorted(self.fake.branches['feat']))
        self.assertEqual(self.head(), 'master')

    def test_no_stash_pop_commit_push_pull_reset_rebase_or_clean_across_a_review_round_trip(self):
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        self.repos.add_remote_only_branch('review', {'manifest.lkml': 'project_name: "demo"\n'})
        self.switch('--branch', 'review', '--force')
        self.switch('--mode', 'prod', '--force')
        self.switch('--mode', 'dev', '--force')
        self.switch('--branch', 'feat', '--force')
        self.assertEqual(self.head(), 'feat')
        calls = self.git_calls()
        self.assertTrue(any(a[0] == 'stash' for a in calls))
        self.assertTrue(any(a[0] == 'checkout' for a in calls))
        self.assertEqual({a[0] for a in calls} - {'rev-parse', 'symbolic-ref', 'status', 'cat-file', 'merge-base',
                                                   'remote', 'fetch', 'stash', 'checkout', 'merge',
                                                   'for-each-ref'}, set())
        self.assertEqual(len(self.stashes()), 1)   # never popped
        # the tearDown check asserts the never-list on every test of this class


class GitDriftTest(GitTestCase):
    """The drift guard: status warns, pull and push refuse while git is off the config branch."""

    FIX = 'looker-sync switch --branch feat --force'

    def setUp(self):
        super().setUp()
        r = self.init()   # a clone without a terminal: init links origin
        self.assertEqual(self.config()['git_remote'], 'origin', r.stdout)

    def tearDown(self):
        try:
            self.assertEqual([a for a in self.git_calls() if a[0] in GitSwitchTest.NEVER], [])
        finally:
            super().tearDown()

    def snapshot(self):
        """Every file in the folder outside .git, with its bytes."""
        files = {}
        for top, dirs, names in os.walk(self.dir):
            dirs[:] = [d for d in dirs if d != '.git']
            for n in names:
                with open(os.path.join(top, n), 'rb') as f:
                    files[os.path.relpath(os.path.join(top, n), self.dir)] = f.read()
        return files

    def make_work_on_both_sides(self):
        """A pull and a push that would each write something if they were let through."""
        self.repos.commit('feat', dict(self.fake.branches['feat'], **{'views/looker.view.lkml': 'view: looker {}\n'}))
        self.put_local('views/mine.view.lkml', 'view: mine {}\n')

    def git_moves(self):
        return [a for a in self.git_calls() if a[0] in ('checkout', 'stash', 'merge')]

    def assert_refused_untouched(self, args, fix):
        before, writes, moves = self.snapshot(), len(self.writes()), self.git_moves()
        r = self.run_cli(*args)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn(fix, [line.strip() for line in r.stdout.splitlines()])
        self.assertEqual((len(self.writes()), self.git_moves()), (writes, moves))
        self.assertEqual(self.snapshot(), before)
        return r

    def test_status_warns_after_a_hand_made_checkout_and_writes_nothing(self):
        self.git('checkout', '-q', '-b', 'master', 'origin/master')   # by hand
        before = self.snapshot()
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("warning: git is on 'master', not 'feat'", r.stdout)
        self.assertIn(self.FIX, [line.strip() for line in r.stdout.splitlines()])
        self.assertIn('status:', r.stdout)
        self.assertEqual((self.writes(), self.git_moves()), ([], []))
        self.assertEqual(self.snapshot(), before)

    def test_pull_and_push_refuse_after_a_hand_made_checkout_until_switch_fixes_it(self):
        self.git('checkout', '-q', '-b', 'master', 'origin/master')
        self.make_work_on_both_sides()
        for args in (['pull'], ['pull', '--force'], ['push'], ['push', '--force', '--delete']):
            with self.subTest(args=args):
                r = self.assert_refused_untouched(args, self.FIX)
                self.assertIn("git is on 'master', not 'feat'", r.stdout)
        self.assertEqual(self.git('branch', '--show-current'), 'master')
        # the fix line does what it says; afterwards pull goes through
        os.remove(os.path.join(self.dir, 'views/mine.view.lkml'))
        self.assertEqual(self.run_cli('switch', '--branch', 'feat', '--force').returncode, 0)
        r = self.run_cli('pull')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('warning: git', r.stdout)
        self.assertEqual(self.local('views/looker.view.lkml'), 'view: looker {}\n')

    def test_detached_head_is_drift(self):
        self.git('checkout', '-q', '--detach')   # same commit, but not a branch
        self.make_work_on_both_sides()
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("warning: git is on a detached HEAD, not 'feat'", r.stdout)
        for args in (['pull'], ['push']):
            with self.subTest(args=args):
                r = self.assert_refused_untouched(args, self.FIX)
                self.assertIn("git is on a detached HEAD, not 'feat'", r.stdout)

    def test_in_prod_mode_the_fix_line_names_prod(self):
        self.assertEqual(self.run_cli('switch', '--mode', 'prod', '--force').returncode, 0)
        self.git('checkout', '-q', 'feat')
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("warning: git is on 'feat', not 'master'", r.stdout)
        r = self.assert_refused_untouched(['pull'], 'looker-sync switch --mode prod --force')

    def test_no_drift_no_warning(self):
        for args in (['status'], ['pull'], ['push']):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn('warning: git', r.stdout)

    def test_push_on_a_readonly_branch_is_refused_while_status_and_pull_still_work(self):
        self.fake.readonly.add('feat')   # as a colleague's personal branch would be
        self.make_work_on_both_sides()
        before, writes = self.snapshot(), len(self.writes())
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("Looker reports branch 'feat' as read-only for you", r.stdout)
        self.assertEqual((len(self.writes()), self.snapshot()), (writes, before))
        self.assertEqual(self.run_cli('status').returncode, 0)
        self.assertEqual(self.run_cli('pull').returncode, 0)

    def test_unlinked_folder_never_checks_drift_but_still_refuses_a_readonly_push(self):
        cfg = self.config()
        del cfg['git_remote']
        self.put_local('.looker-sync/config.json', json.dumps(cfg))
        self.git('checkout', '-q', '-b', 'master', 'origin/master')   # drift, but nothing is linked
        os.remove(self.git_log_path)
        for args in (['status'], ['pull'], ['push']):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn('warning: git', r.stdout)
            self.assertNotIn('switch --branch', r.stdout)
        self.fake.readonly.add('feat')
        self.put_local('views/mine.view.lkml', 'view: mine {}\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('read-only', r.stdout)
        self.assertEqual(self.git_calls(), [])


def _import_cli():
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
    if src not in sys.path:
        sys.path.insert(0, src)
    from looker_sync import cli
    return cli


def _in_thread(fn, timeout=10):
    """Run fn in a daemon thread; return (finished, result or exception). A hang fails the test
    instead of hanging the suite."""
    box = {}

    def run():
        try:
            box['value'] = fn()
        except BaseException as e:  # noqa: BLE001
            box['value'] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive(), box.get('value')


def _unblock_fifo(path):
    """If something is stuck opening the FIFO for reading, a writer releases it."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


class FastPushTest(FakeLookerTestCase):
    """push reads only the files it may write or delete; --no-validate and --verbose."""

    def setUp(self):
        super().setUp()
        self.init()
        self.fake.log.clear()   # init read every file; count only what push does

    def reads(self, before_write=False):
        """GET file/content calls in the log; before_write stops at the first file write."""
        n = 0
        for method, path, _, _ in self.fake.log:
            if before_write and method in ('POST', 'PUT', 'DELETE') and path.endswith('/files'):
                break
            n += method == 'GET' and path.endswith('/file/content')
        return n

    def test_no_change_push_reads_no_file_and_one_change_reads_only_that_file(self):
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('push plan: nothing differs', r.stdout)
        self.assertEqual(self.reads(), 0)
        self.assertEqual(self.writes(), [])
        self.fake.log.clear()
        self.put_local('views/orders.view.lkml', 'view: orders { dimension: id {} }\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.reads(before_write=True), 1)   # the guard's read of the changed file
        self.assertEqual(self.reads(), 2)                    # plus its read-back
        self.assertIn('read-back: 1/1 byte-equal', r.stdout)

    def test_file_changed_only_in_looker_does_not_block_pushing_another(self):
        self.fake.dev_write('models/demo.model.lkml', 'connection: "ide"\n')
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fake.dev_files()['views/orders.view.lkml'], 'view: orders { # mine\n}\n')
        self.assertEqual(self.fake.dev_files()['models/demo.model.lkml'], 'connection: "ide"\n')   # left alone
        self.assertEqual(self.local('models/demo.model.lkml'), 'connection: "db"\n')
        # status still reads everything and reports Looker's change
        r = self.run_cli('status')
        self.assertIn('models/demo.model.lkml', r.stdout)

    def test_file_changed_on_both_sides_is_still_refused(self):
        self.fake.dev_write('views/orders.view.lkml', 'view: orders { # ide\n}\n')
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('changed in Looker since the last sync: views/orders.view.lkml', r.stdout)
        self.assertEqual(self.writes(), [])

    def test_file_deleted_in_looker_is_still_refused(self):
        self.fake.dev_write('views/orders.view.lkml', None)
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('changed in Looker since the last sync: views/orders.view.lkml', r.stdout)
        self.assertEqual(self.writes(), [])
        self.assertNotIn('views/orders.view.lkml', self.fake.dev_files())

    def test_no_validate_skips_validation(self):
        self.put_local('views/orders.view.lkml', 'view: orders { dimension: id {} }\n')
        r = self.run_cli('push', '--no-validate')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('validation: skipped (--no-validate)', r.stdout)
        self.assertFalse(any(p.endswith('/validate') for _, p, _, _ in self.fake.log))
        self.assertIn('read-back: 1/1 byte-equal', r.stdout)

    def test_verbose_prints_timings_and_info_lines_hidden_without_it(self):
        self.fake.validation_errors = [{'severity': 'info', 'message': 'a note from the validator',
                                        'file_path': 'views/orders.view.lkml', 'line_number': 1}]
        self.put_local('views/orders.view.lkml', 'view: orders { dimension: id {} }\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('a note from the validator', r.stdout)
        self.assertIn('validation: no errors (1 info line(s) hidden; --verbose shows them)', r.stdout)
        self.assertNotIn('timing:', r.stdout + r.stderr)
        self.put_local('views/orders.view.lkml', 'view: orders { dimension: id {} dimension: n {} }\n')
        r = self.run_cli('push', '--verbose')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('a note from the validator', r.stdout)
        self.assertNotIn('hidden', r.stdout)
        self.assertNotIn('timing:', r.stdout)
        timings = [line.split()[1] for line in r.stderr.splitlines() if line.startswith('timing:')]
        for label in ('session', 'file', 'remote', 'validate', 'total'):
            self.assertIn(label, timings, r.stderr)


class FastPushFetchTest(GitTestCase):
    """push skips git fetch when the clone's last-fetched refs already prove Looker's commit."""

    def setUp(self):
        super().setUp()
        r = self.init()   # a clone without a terminal: init links origin
        self.assertEqual(self.config()['git_remote'], 'origin', r.stdout)

    def fetches(self):
        return sum(a[0] == 'fetch' for a in self.git_calls())

    def run_ok(self, *args):
        r = self.run_cli(*args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_push_skips_fetch_when_the_commit_is_already_fetched(self):
        n = self.fetches()
        self.put_local('views/orders.view.lkml', 'view: orders { # mine\n}\n')
        self.run_ok('push')
        self.run_ok('push')
        self.assertEqual(self.fetches(), n)

    def test_push_fetches_when_looker_moved_and_still_passes(self):
        n = self.fetches()
        files = dict(self.fake.branches['feat'], **{'views/users.view.lkml': 'view: users {}\n'})
        sha = self.repos.commit('feat', files)    # Looker commits and pushes; the clone has not fetched
        self.assertNotEqual(self.git_try('cat-file', '-e', sha + '^{commit}').returncode, 0)
        self.run_ok('push')
        self.assertEqual(self.fetches(), n + 1)
        self.assertEqual(self.git('rev-parse', 'origin/feat'), sha)

    def test_status_and_switch_still_fetch_every_time(self):
        n = self.fetches()
        self.run_ok('status')
        self.assertEqual(self.fetches(), n + 1)
        self.run_ok('switch', '--branch', 'master', '--force')
        self.assertGreater(self.fetches(), n + 1)


class LocalScanTest(unittest.TestCase):
    """scan_tree / read_stable / Project.local_files on awkward folders, no Looker involved."""

    def setUp(self):
        self.cli = _import_cli()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.proj = self.cli.Project(self.root)
        self.put('views/real.view.lkml', 'view: real {}\n')

    def tearDown(self):
        for dirpath, dirnames, _ in os.walk(self.root):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        self.tmp.cleanup()

    def put(self, rel, text):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(text)
        return full

    def test_fifo_named_like_a_view_does_not_hang(self):
        fifo = os.path.join(self.root, 'views', 'x.view.lkml')
        os.mkfifo(fifo)
        self.addCleanup(_unblock_fifo, fifo)
        done, scan = _in_thread(self.proj.scan)
        self.assertTrue(done, 'scan hung on a FIFO')
        self.assertEqual(sorted(scan.entries), ['views/real.view.lkml'])
        done, r = _in_thread(lambda: self.cli.read_stable(fifo))
        self.assertTrue(done, 'read hung on a FIFO')
        self.assertEqual(r.status, 'not_regular')
        done, files = _in_thread(self.proj.local_files)
        self.assertTrue(done, 'local_files hung on a FIFO')
        self.assertEqual(files, {'views/real.view.lkml': b'view: real {}\n'})

    def test_dangling_symlink_is_skipped(self):
        os.symlink(os.path.join(self.root, 'nowhere.lkml'), os.path.join(self.root, 'y.view.lkml'))
        self.assertEqual(sorted(self.proj.scan().entries), ['views/real.view.lkml'])
        self.assertEqual(list(self.proj.local_files()), ['views/real.view.lkml'])
        self.assertEqual(self.proj.read_entry('y.view.lkml').status, 'vanished')

    def test_symlink_to_a_file_still_counts_as_that_file(self):
        os.symlink(os.path.join(self.root, 'views', 'real.view.lkml'),
                   os.path.join(self.root, 'link.view.lkml'))
        self.assertEqual(self.proj.local_files()['link.view.lkml'], b'view: real {}\n')

    def test_symlink_loops_are_skipped(self):
        a, b = os.path.join(self.root, 'a.view.lkml'), os.path.join(self.root, 'b.view.lkml')
        os.symlink(b, a)
        os.symlink(a, b)
        os.symlink(self.root, os.path.join(self.root, 'views', 'loopdir'))  # folder loop
        done, scan = _in_thread(self.proj.scan)
        self.assertTrue(done)
        self.assertEqual(sorted(scan.entries), ['views/real.view.lkml'])
        self.assertEqual(self.cli.read_stable(a).status, 'not_regular')
        self.assertEqual(list(self.proj.local_files()), ['views/real.view.lkml'])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can list any folder')
    def test_unreadable_folder_is_reported_not_treated_as_empty(self):
        self.put('locked/inner.view.lkml', 'view: inner {}\n')
        first = self.proj.scan()
        self.assertIn('locked/inner.view.lkml', first.entries)
        os.chmod(os.path.join(self.root, 'locked'), 0)
        second = self.proj.scan()
        self.assertEqual(second.unreadable, ['locked'])
        self.assertNotIn('locked/inner.view.lkml', second.entries)
        carried = self.proj.scan(previous=first)
        self.assertEqual(carried.carried, ['locked/inner.view.lkml'])
        self.assertEqual(carried.entries['locked/inner.view.lkml'],
                         first.entries['locked/inner.view.lkml'])
        with self.assertRaises(self.cli.Refused):
            self.proj.local_files()

    def test_file_vanishing_between_scan_and_read(self):
        scan = self.proj.scan()
        os.unlink(os.path.join(self.root, 'views', 'real.view.lkml'))
        self.assertEqual(self.proj.read_entry('views/real.view.lkml').status, 'vanished')
        self.assertIn('views/real.view.lkml', scan.entries)
        real_scan = self.proj.scan
        with mock.patch.object(self.proj, 'scan', lambda previous=None: scan):
            self.assertEqual(self.proj.local_files(), {})
        self.assertEqual(real_scan().entries, {})

    def test_file_changing_during_read_is_unstable(self):
        full = os.path.join(self.root, 'views', 'real.view.lkml')
        real_read = os.read
        state = {'n': 0}

        def growing_read(fd, n):
            state['n'] += 1
            if state['n'] == 1:
                with open(full, 'a') as f:
                    f.write('# appended mid-read\n' * 50)
            return real_read(fd, n)
        with mock.patch.object(self.cli.os, 'read', growing_read):
            r = self.cli.read_stable(full, 'views/real.view.lkml')
        self.assertEqual(r.status, 'unstable')
        self.assertIsNone(r.data)
        r = self.cli.read_stable(full, 'views/real.view.lkml')
        self.assertEqual(r.status, 'ok')
        self.assertEqual(r.entry.size, len(r.data))

    def test_file_replaced_during_read_is_unstable(self):
        full = os.path.join(self.root, 'views', 'real.view.lkml')
        real_read = os.read
        state = {'n': 0}

        def replacing_read(fd, n):
            state['n'] += 1
            if state['n'] == 1:
                tmp = self.put('views/.tmp-save', 'view: new {}\n')
                os.replace(tmp, full)
            return real_read(fd, n)
        with mock.patch.object(self.cli.os, 'read', replacing_read):
            self.assertEqual(self.cli.read_stable(full).status, 'unstable')

    def test_local_files_refuses_a_file_that_never_settles(self):
        unstable = self.cli.ReadResult('unstable', None, None)
        with mock.patch.object(self.proj, 'read_entry', lambda rel: unstable), \
                mock.patch.object(self.cli.time, 'sleep', lambda s: None):
            with self.assertRaises(self.cli.Refused):
                self.proj.local_files()

    def test_editor_temp_names_are_ignored(self):
        for name in ('.#x.lkml', '._x.lkml', '~x.lkml', '#x.lkml#', '4913', 'x.lkml~',
                     'x.lkml.swp', '.x.lkml.swp', '.hidden/y.view.lkml', '.git/z.lkml'):
            self.put(f'views/{name}', 'junk')
        self.put('views/x.lkml', 'view: x {}\n')
        self.assertEqual(sorted(self.proj.scan().entries), ['views/real.view.lkml', 'views/x.lkml'])
        self.assertEqual(sorted(self.proj.local_files()), ['views/real.view.lkml', 'views/x.lkml'])
        # Looker's side uses the same rule, so an ignored name is never taken as deleted locally
        self.assertFalse(self.proj.synced('views/.#x.lkml'))
        self.assertFalse(self.proj.synced('.hidden/y.view.lkml'))
        self.assertTrue(self.proj.synced('views/x.lkml'))

    def test_scan_entry_shape(self):
        full = os.path.join(self.root, 'views', 'real.view.lkml')
        st = os.stat(full)
        scan = self.proj.scan()
        self.assertEqual(scan.entries['views/real.view.lkml'],
                         self.cli.ScanEntry('views/real.view.lkml', st.st_mtime_ns, st.st_size,
                                            st.st_ino, st.st_dev))
        self.assertEqual((scan.unreadable, scan.carried), ([], []))
        self.assertLessEqual(scan.started_ns, self.cli.time.time_ns())


class LocalScanCLITest(FakeLookerTestCase):
    def test_status_and_dry_run_push_complete_with_a_fifo_in_the_folder(self):
        self.init()
        fifo = os.path.join(self.dir, 'views', 'x.view.lkml')
        os.mkfifo(fifo)
        self.addCleanup(_unblock_fifo, fifo)
        os.symlink(os.path.join(self.dir, 'gone.lkml'), os.path.join(self.dir, 'y.view.lkml'))
        for args in (['status'], ['push', '--dry-run']):
            try:
                r = subprocess.run([sys.executable, CLI, '-C', self.dir, *args], capture_output=True,
                                   text=True, stdin=subprocess.DEVNULL, timeout=20)
            except subprocess.TimeoutExpired:
                _unblock_fifo(fifo)
                self.fail(f'{args} hung on a FIFO')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn('x.view.lkml', r.stdout)
            self.assertNotIn('Traceback', r.stderr)


GATE_VIEW = '''# Orders view. Braces in a comment: { { } and a "quote
view: orders {
  sql_table_name: analytics.orders ;;
  drill_fields: [id, status]

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: status {
    label: "Status {raw}"
    description: "Says \\"done\\" when the order shipped"
    type: string
    sql: CASE WHEN ${TABLE}.s = '}' THEN '{' ELSE ${TABLE}.s END ;;
    html: <span style="color: {{ value }}">{{ rendered_value }}</span> ;;
  }

  dimension: is_big {
    type: yesno
    sql: ${TABLE}.amount > 100
      AND ${TABLE}.status <> '}' ;;
  }

  measure: total {
    type: sum
    sql: ${TABLE}.amount ;;
    value_format_name: usd
    link: {
      label: "Detail"
      url: "/dashboards/1?f={{ value }}"
    }
  }

  measure: big_total {
    type: number
    sql: ${total} ;;
    filters: [is_big: "yes"]
  }
}
'''

GATE_EXPLORE = '''include: "/views/*.view.lkml"

explore: orders {
  label: "Orders"
  sql_always_where: ${orders.status} <> 'test {x}' ;;
  join: customers {
    type: left_outer
    relationship: many_to_one
    sql_on: ${orders.customer_id} = ${customers.id} ;;
  }
  always_filter: {
    filters: [orders.status: "done"]
  }
}
'''

GATE_MODEL = '''connection: "warehouse"

include: "/views/*.view.lkml"
include: "/explores/*.explore.lkml"

datagroup: nightly {
  sql_trigger: SELECT CURRENT_DATE() ;;
  max_cache_age: "24 hours"
}

persist_with: nightly

explore: customers {
  # a comment with a lone } brace
  fields: [ALL_FIELDS*]
}
'''

GATE_DASHBOARD = '''- dashboard: sales
  title: Sales { not lkml
  layout: newspaper
  elements:
  - name: total
    type: single_value
'''

GATE_CORPUS = {
    'views/orders.view.lkml': GATE_VIEW,
    'explores/orders.explore.lkml': GATE_EXPLORE,
    'demo.model.lkml': GATE_MODEL,
}

GATE_VALUE_KEYS = ('sql:', 'html:', 'sql_on:', 'sql_always_where:', 'sql_trigger:',
                   'sql_table_name:')


def _gate_inside_value(text, i):
    """True when position i of a corpus file is inside a string, a comment or a `;;` block,
    where removing a brace leaves the file valid."""
    line = text[text.rfind('\n', 0, i) + 1:i]
    if '#' in line or line.count('"') % 2 == 1:
        return True
    head = text[:i]
    last_close = head.rfind(';;')
    return any(head.rfind(key) > last_close for key in GATE_VALUE_KEYS)


def gate_mutations(text):
    """Yield (name, broken_text) for one valid corpus file. Every yielded text is broken
    LookML that the gate must hold."""
    lines = text.split('\n')
    for i, ch in enumerate(text):
        if ch in '{}' and not _gate_inside_value(text, i):
            yield f'drop {ch} at offset {i}', text[:i] + text[i + 1:]
    ends = [len('\n'.join(lines[:n + 1])) for n in range(len(lines))]
    for n in range(len(lines)):
        if _gate_inside_value(text, ends[n]):
            continue  # inside a multi-line sql or html block, where anything goes

        def insert(extra):
            return '\n'.join(lines[:n + 1] + [extra] + lines[n + 1:])
        yield f'extra }} after line {n + 1}', insert('}')
        yield f'garbage after line {n + 1}', insert('@@ oops')
        yield f'colonless key after line {n + 1}', insert('hidden yes')
    start = 0
    while text.find(';;', start) >= 0:
        j = text.find(';;', start)
        yield f'drop ;; at offset {j}', text[:j] + text[j + 2:]
        start = j + 2
    for m, line in enumerate(lines):
        if (line.count('"') == 2 and '\\' not in line and not line.lstrip().startswith('#')
                and not any(key in line for key in GATE_VALUE_KEYS)):
            k = line.rindex('"')
            yield f'unterminated string line {m + 1}', '\n'.join(
                lines[:m] + [line[:k] + line[k + 1:]] + lines[m + 1:])


class GateTest(unittest.TestCase):
    """The syntax gate: valid LookML passes, broken copies are held with a one-line reason."""

    @classmethod
    def setUpClass(cls):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
        if src not in sys.path:
            sys.path.insert(0, src)
        from looker_sync import gate
        cls.gate = gate

    def check(self, path, text):
        return self.gate.check(path, text.encode('utf-8') if isinstance(text, str) else text)

    def test_valid_view_explore_model_pass(self):
        for path, text in GATE_CORPUS.items():
            with self.subTest(path=path):
                self.assertIsNone(self.check(path, text))
        self.assertIsNone(self.check('views/empty.view.lkml', ''))

    def test_dashboard_and_non_lkml_files_pass_unchecked(self):
        self.assertIsNone(self.check('dashboards/sales.dashboard.lookml', GATE_DASHBOARD))
        self.assertIsNone(self.check('docs/readme.md', '{{{ not lookml'))
        self.assertIsNone(self.check('manifest.json', b'\xff\xfe'))

    def test_non_utf8_fails(self):
        reason = self.check('views/orders.view.lkml', b'view: a {\n  label: "\xff"\n}\n')
        self.assertIn('UTF-8', reason)

    def test_named_breakages_fail_with_one_line_reason(self):
        cases = {
            'missing brace': (GATE_VIEW.replace('  dimension: id {', '  dimension: id', 1),
                              None),
            'extra closing brace': (GATE_VIEW.replace('    primary_key: yes\n',
                                                      '    primary_key: yes\n  }\n', 1),
                                    'extra closing brace on line 42'),
            'extra brace at end': (GATE_VIEW + '}\n', 'extra closing brace'),
            'unterminated string': (GATE_VIEW.replace('label: "Status {raw}"',
                                                      'label: "Status {raw}', 1), None),
            'missing ;;': (GATE_VIEW.replace('${TABLE}.amount ;;', '${TABLE}.amount', 1),
                           'sql: on line 28 is missing its ;; (it runs on into '
                           'value_format_name: on line 29)'),
            'last ;; missing': (GATE_VIEW.replace('sql: ${total} ;;', 'sql: ${total}', 1),
                                'never closed with ;;'),
            'garbage token': (GATE_VIEW.replace('    type: number\n',
                                                '    type: number\n    ]]\n', 1), None),
            'lone semicolon': (GATE_VIEW.replace('type: yesno', 'type: yesno ;', 1),
                               "Unexpected ';'"),
            'half written': (GATE_VIEW[:GATE_VIEW.index('measure: total') + 20], None),
            'cut after the view name': ('view: orders', 'has no { } block'),
            'cut after the explore name': (GATE_EXPLORE[:GATE_EXPLORE.index(' {')],
                                           'explore: orders has no { } block'),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name=name):
                reason = self.check('views/orders.view.lkml', text)
                self.assertIsNotNone(reason, 'gate let a broken file through')
                self.assertNotIn('\n', reason)
                if expected:
                    self.assertIn(expected, reason)

    def test_mutation_set_is_all_caught(self):
        total, missed = 0, []
        for path, text in GATE_CORPUS.items():
            for name, broken in gate_mutations(text):
                total += 1
                if self.check(path, broken) is None:
                    missed.append(f'{path}: {name}')
        self.assertGreater(total, 200)
        self.assertEqual(missed, [])

    def test_braces_in_sql_html_strings_comments_pass(self):
        text = ('view: v {\n'
                '  # } a closing brace in a comment\n'
                '  dimension: d {\n'
                '    label: "{ open"\n'
                '    sql: CASE WHEN x = \'}\' THEN \'}}\' END ;;\n'
                '    html: {% if value %}<b>{{ value }}</b>{% endif %} ;;\n'
                '  }\n'
                '  measure: m {\n'
                '    type: number\n'
                '    sql: ${d} ;;\n'
                '  }\n'
                '}\n')
        self.assertIsNone(self.gate._scan(text))
        self.assertIsNone(self.check('views/v.view.lkml', text))

    def test_sql_that_looks_like_keys_is_not_a_missing_semicolons(self):
        text = ('view: v {\n'
                '  derived_table: {\n'
                '    sql: SELECT x::int AS type, \'{"label": 1}\' AS j\n'
                '      FROM t -- type: comment\n'
                '      WHERE a = :param ;;\n'
                '  }\n'
                '  dimension: d {\n'
                '    sql: ${TABLE}.d ;;\n'
                '  }\n'
                '}\n')
        self.assertIsNone(self.check('views/v.view.lkml', text))

    def test_parser_logging_does_not_leak_to_stderr(self):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
        code = (
            'import logging\n'
            'logging.basicConfig(level=logging.DEBUG)\n'
            'root = logging.getLogger()\n'
            'before = (root.level, list(root.handlers))\n'
            'from looker_sync import gate\n'
            'assert (root.level, list(root.handlers)) == before\n'
            'gate.check("a.view.lkml", b"view: a { dimension: b { sql: x ;; } }")\n'
            'gate.check("a.view.lkml", b"view: a { dimension: b sql: x ;; } }")\n'
            'for name in ("parser", "simple", "visitors"):\n'
            '    logging.getLogger("looker_sync._vendor.lkml." + name).warning("leak")\n'
            'logging.getLogger("other").warning("root still works")\n'
        )
        env = dict(os.environ, PYTHONPATH=src)
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                           env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('leak', r.stderr)
        self.assertIn('root still works', r.stderr)


def import_cli():
    """The CLI module, imported in-process for the connection-layer tests."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
    if src not in sys.path:
        sys.path.insert(0, src)
    from looker_sync import cli
    return cli


class ConnectionLayerTestCase(FakeLookerTestCase):
    """A Looker client in this process against the fake, in dev mode, closed after each test."""

    def setUp(self):
        super().setUp()
        self.cli = import_cli()
        self.fake.branches['feat'].update({f'views/v{i:02}.view.lkml': f'view: v{i:02} {{}}\n' for i in range(20)})
        self.env = {'LOOKER_URL': self.url, 'LOOKER_CLIENT_ID': 'id', 'LOOKER_CLIENT_SECRET': SECRET}

    def client(self):
        L = self.cli.Looker(self.env)
        self.addCleanup(L.close)
        L.set_workspace('dev')
        return L

    def tearDown(self):
        self.fake.drop_connections()
        super().tearDown()

    def logins(self):
        return self.fake.logins

    def wait_for(self, cond, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return cond()


class KeepAliveTest(ConnectionLayerTestCase):
    def test_sequential_requests_share_one_connection(self):
        L = self.client()
        for i in range(10):
            self.assertEqual(L.read_file(PROJECT, f'views/v{i:02}.view.lkml'), f'view: v{i:02} {{}}\n'.encode())
        # login, PATCH and GET /session, 10 reads: one TCP connection on both sides
        self.assertEqual(self.fake.connections, 1)
        self.assertEqual(L.pool.opened, 1)

    def test_parallel_reads_are_correct_and_in_input_order(self):
        L = self.client()
        paths = [f'views/v{i:02}.view.lkml' for i in reversed(range(20))]
        got = L.read_files(PROJECT, paths, workers=8)
        self.assertEqual(got, [self.fake.branches['feat'][p].encode() for p in paths])
        self.assertLessEqual(self.fake.connections, 8)
        before = self.fake.connections
        L.read_files(PROJECT, paths, workers=8)
        self.assertEqual(self.fake.connections, before)   # the second round reuses the pool

    def test_parallel_read_of_a_missing_file_raises(self):
        L = self.client()
        with self.assertRaises(self.cli.LookerError) as e:
            L.read_files(PROJECT, ['views/v00.view.lkml', 'views/nope.view.lkml'], workers=4)
        self.assertIn('HTTP 404', str(e.exception))

    def test_status_reads_over_reused_connections(self):
        self.init()
        before = self.fake.connections
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # 23 files read in parallel by 8 workers, plus the calls before them: never one per request
        self.assertLessEqual(self.fake.connections - before, 8)


class ReconnectTest(ConnectionLayerTestCase):
    def test_read_after_server_dropped_idle_connection(self):
        L = self.client()
        L.read_file(PROJECT, 'views/v00.view.lkml')
        self.fake.drop_connections()
        time.sleep(0.1)
        self.assertEqual(L.read_file(PROJECT, 'views/v01.view.lkml'), b'view: v01 {}\n')
        self.assertEqual(self.fake.connections, 2)

    def test_write_after_server_dropped_idle_connection_is_sent_once(self):
        L = self.client()
        self.fake.drop_connections()
        time.sleep(0.1)
        L.call('PUT', f'/projects/{PROJECT}/files', body={'path': 'views/v00.view.lkml', 'content': 'view: new {}\n'})
        puts = [e for e in self.fake.log if e[0] == 'PUT']
        self.assertEqual(len(puts), 1)
        self.assertEqual(self.fake.dev_files()['views/v00.view.lkml'], 'view: new {}\n')

    def test_fresh_connection_refused_is_a_looker_error(self):
        L = self.client()
        L.close()
        self.fake.server.shutdown()
        self.fake.server.server_close()
        self.fake.drop_connections()
        with self.assertRaises(self.cli.LookerError):
            L.read_file(PROJECT, 'views/v00.view.lkml')
        self.fake.server = mock.Mock()   # tearDown already has nothing to stop


class TokenRefreshTest(ConnectionLayerTestCase):
    def test_401_logs_in_again_and_retries_once(self):
        L = self.client()
        old = L.token
        self.fake.tokens.pop(old)        # Looker forgets the token mid-run
        self.assertEqual(L.read_file(PROJECT, 'views/v00.view.lkml'), b'view: v00 {}\n')
        self.assertNotEqual(L.token, old)
        self.assertEqual(self.fake.tokens[L.token], 'dev')   # the new token is back in dev mode

    def test_second_401_raises_auth_expired(self):
        L = self.client()
        self.fake.force_401 = 2
        with self.assertRaises(self.cli.AuthExpired) as e:
            L.read_file(PROJECT, 'views/v00.view.lkml')
        self.assertIsInstance(e.exception, self.cli.LookerError)   # one-shot commands exit 1
        self.assertNotIn(L.token, str(e.exception))

    def test_cli_exits_1_when_looker_keeps_answering_401(self):
        self.init()
        self.fake.force_401 = 1000
        r = self.run_cli('status')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('HTTP 401 again after a fresh login', r.stdout)
        self.assertNotIn('tok', r.stdout)

    def test_lazy_refresh_after_80_percent_of_expires_in(self):
        self.fake.expires_in = 1
        L = self.client()
        old, logins = L.token, self.logins()
        L.read_file(PROJECT, 'views/v00.view.lkml')
        self.assertEqual(L.token, old)   # before 0.8 s: no refresh
        time.sleep(0.85)
        L.read_file(PROJECT, 'views/v00.view.lkml')
        self.assertNotEqual(L.token, old)
        self.assertEqual(self.logins(), logins + 1)
        self.assertEqual(self.fake.tokens[L.token], 'dev')

    def test_background_refresh_swaps_token_without_requests(self):
        self.fake.expires_in = 1
        L = self.client()
        seen = []
        L.before_token_swap = lambda tok: seen.append((tok, self.fake.tokens[tok]))
        old = L.token
        L.start_auto_refresh()
        self.assertTrue(self.wait_for(lambda: L.token != old, 3))
        L.stop_auto_refresh()
        # the hook ran before the swap, on the new token already in dev mode
        self.assertEqual(seen[0], (L.token, 'dev'))

    def test_hook_refusing_the_swap_keeps_the_old_token(self):
        self.fake.expires_in = 1
        L = self.client()
        errors = []
        old = L.token

        def refuse(tok):
            raise self.cli.Refused('branch moved')
        L.before_token_swap = refuse
        L.on_refresh_error = errors.append
        L.start_auto_refresh()
        self.assertTrue(self.wait_for(lambda: errors, 3))
        L.stop_auto_refresh()
        self.assertEqual(L.token, old)
        self.assertIsInstance(errors[0], self.cli.Refused)


class WatchCoreTestCase(unittest.TestCase):
    """WatchCore with a fake clock and an inline fake worker: no network, no threads."""

    def setUp(self):
        self.cli = _import_cli()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, 'proj')
        os.makedirs(os.path.join(self.root, '.looker-sync'))
        with open(os.path.join(self.root, '.looker-sync', 'config.json'), 'w') as f:
            f.write('{}')
        self.wall = time.time_ns()
        self.now = 0.0
        self.pushes, self.events = [], []
        self.remote = {}
        self.answer = None      # what the fake push_one returns or raises

    def tearDown(self):
        for dirpath, dirnames, _ in os.walk(self.tmp.name):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)
        self.tmp.cleanup()

    def put(self, rel, text, old=True, synced=False):
        full = os.path.join(self.root, *rel.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(text.encode())
        if old:
            t = self.wall - 100 * 10**9
            os.utime(full, ns=(t, t))
        if synced:
            self.remote[rel] = hashlib.sha256(text.encode()).hexdigest()
        return full

    def push_one(self, rel, data):
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is None:
            self.pushes.append((rel, data.decode()))
        return self.answer

    def core(self, **kw):
        kw.setdefault('gate', lambda rel, data: None)
        self.c = self.cli.WatchCore(
            self.cli.Project(self.root), self.push_one, remote_state=lambda: dict(self.remote),
            on_event=lambda kind, text, paths: self.events.append((kind, text, tuple(paths))),
            clock=lambda: self.now, wall_ns=lambda: self.wall, threaded=False, **kw)
        return self.c

    def at(self, t):
        self.now = t
        return self.c.tick()

    def kinds(self, kind):
        return [e for e in self.events if e[0] == kind]

    def held(self, word):
        return [e for e in self.kinds('held') if word in e[1]]


class WatchSettleTest(WatchCoreTestCase):

    def test_in_place_save_waits_150_ms(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.assertIsNone(self.at(0))
        with open(full, 'ab') as f:
            f.write(b'# more\n')
        self.assertIsNone(self.at(5.0))
        self.assertIsNone(self.at(5.1))
        self.assertEqual(self.at(5.16), {'a.view.lkml': b'view: a {}\n# more\n'})
        self.assertEqual(self.pushes, [('a.view.lkml', 'view: a {}\n# more\n')])
        self.assertIsNone(self.at(6))

    def test_atomic_rename_is_pushed_at_once(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        self.put('.a.view.lkml.swp', 'view: a { }\n', old=False)
        os.replace(os.path.join(self.root, '.a.view.lkml.swp'),
                   os.path.join(self.root, 'a.view.lkml'))
        self.assertEqual(self.at(5.0), {'a.view.lkml': b'view: a { }\n'})

    def test_five_paths_in_one_scan_is_a_burst(self):
        for i in range(5):
            self.put(f'v{i}.view.lkml', f'view: v{i} {{}}\n', synced=True)
        self.core()
        self.at(0)
        for i in range(5):
            self.put(f'v{i}.view.lkml', f'view: v{i} {{ }}\n', old=False)
        self.assertIsNone(self.at(5.0))
        self.assertIsNone(self.at(5.3))
        self.assertEqual(len(self.at(5.5)), 5)

    def test_repeat_writer_waits_two_and_a_half_gaps(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        for t in (5.0, 5.4):
            with open(full, 'ab') as f:
                f.write(b'#\n')
            self.assertIsNone(self.at(t))
        self.assertIsNone(self.at(6.3))      # 5.4 + clamp(2.5 x 0.4) = 6.4
        self.assertIsNotNone(self.at(6.41))

    def test_noisy_file_is_pushed_within_max_wait(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        t, got = 5.0, None
        while got is None and t < 9:
            with open(full, 'ab') as f:
                f.write(b'#\n')
            got = self.at(t)
            t += 0.3
        self.assertIsNotNone(got)
        self.assertLessEqual(t - 0.3, 5.0 + 3.0 + 0.3)   # max_wait, plus one tick

    def test_scan_interval_follows_scan_cost(self):
        self.core(poll=0.05)
        self.at(0)
        self.assertEqual(self.c.interval, max(0.05, 9 * self.c.scan_duration))

    def test_same_size_write_in_the_same_mtime_tick_is_rechecked(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        with open(full, 'wb') as f:
            f.write(b'view: b {}\n')
        os.utime(full, ns=(self.wall, self.wall))     # mtime == "now": one tick of the read
        self.at(5.0)
        self.assertEqual(self.at(5.2), {'a.view.lkml': b'view: b {}\n'})
        ino = os.stat(full).st_ino
        with open(full, 'r+b') as f:                  # same size, same inode, same mtime
            f.write(b'view: c {}\n')
        os.utime(full, ns=(self.wall, self.wall))
        self.assertEqual(os.stat(full).st_ino, ino)
        self.assertIsNone(self.at(5.25))             # seen; a repeat within 1 s settles
        self.assertEqual(self.at(5.9), {'a.view.lkml': b'view: c {}\n'})

    def test_touch_without_new_bytes_is_no_work(self):
        for i in range(4):
            self.put(f'v{i}.view.lkml', f'view: v{i} {{}}\n', synced=True)
        self.core(ceiling=2)
        self.at(0)
        for i in range(4):
            os.utime(os.path.join(self.root, f'v{i}.view.lkml'))
        for t in (5, 5.6, 6.2):
            self.assertIsNone(self.at(t))
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.kinds('held'), [])

    def test_ignored_names_are_never_pushed(self):
        self.core()
        self.at(0)
        for name in ('.#a.view.lkml', '~a.view.lkml', '#a.view.lkml#', '._a.view.lkml'):
            self.put(name, 'view: x {}\n', old=False)
        self.put('real.view.lkml', 'view: real {}\n')
        self.at(5)
        self.assertEqual(self.pushes, [('real.view.lkml', 'view: real {}\n')])


class WatchHoldTest(WatchCoreTestCase):

    def test_move_is_held_never_written_or_deleted(self):
        for i in range(3):
            self.put(f'old/v{i}.view.lkml', f'view: v{i} {{}}\n', synced=True)
        self.core()
        self.at(0)
        os.rename(os.path.join(self.root, 'old'), os.path.join(self.root, 'new'))
        for t in (5, 5.6, 6.2):
            self.assertIsNone(self.at(t))
        self.assertEqual(self.pushes, [])
        moves = self.held('moved')
        self.assertEqual(len(moves), 1)
        self.assertIn('old/v0.view.lkml -> new/v0.view.lkml', moves[0][2])
        self.assertEqual(self.held('deleted locally'), [])

    def test_delete_is_reported_once_never_synced(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.put('b.view.lkml', 'view: b {}\n', synced=True)
        self.core()
        self.at(0)
        os.remove(os.path.join(self.root, 'a.view.lkml'))
        for t in (5, 6, 7):
            self.at(t)
        self.assertEqual(self.pushes, [])
        self.assertEqual(len(self.held('deleted locally')), 1)

    def test_copy_of_a_live_file_is_a_suspect(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        self.put('copy.view.lkml', 'view: a {}\n', old=False)
        self.at(5)
        self.at(5.2)
        self.assertEqual(self.pushes, [])
        self.assertEqual(len(self.held('copy')), 1)

    def test_ceiling_holds_and_tells_the_user_to_push(self):
        for i in range(4):
            self.put(f'v{i}.view.lkml', f'view: v{i} {{}}\n', synced=True)
        self.core(ceiling=3)
        self.at(0)
        for i in range(4):
            self.put(f'v{i}.view.lkml', f'view: v{i} {{ }}\n', old=False)
        self.at(5)
        self.at(5.6)
        self.assertEqual(self.pushes, [])
        self.assertIn('looker-sync push', self.held('more than 3')[0][1])
        self.put('v0.view.lkml', 'view: v0 {}\n', old=False)   # back under the ceiling
        self.at(7)
        self.at(7.2)
        self.assertEqual(sorted(p for p, _ in self.pushes),
                         ['v1.view.lkml', 'v2.view.lkml', 'v3.view.lkml'])

    def test_one_syntax_error_holds_every_pending_file(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.put('b.view.lkml', 'view: b {}\n', synced=True)
        self.core(gate=lambda rel, data: 'broken' if b'BAD' in data else None)
        self.at(0)
        self.put('a.view.lkml', 'view: a { }\n', old=False)
        self.put('b.view.lkml', 'view: b { BAD\n', old=False)
        self.at(5)
        self.at(5.6)
        self.assertEqual(self.pushes, [])
        self.assertIn('b.view.lkml: broken', self.held('syntax')[0][2])
        self.put('b.view.lkml', 'view: b { }\n', old=False)
        self.assertIsNone(self.at(7))        # the fix is still settling: a stays held
        self.assertEqual(sorted(self.at(7.2)), ['a.view.lkml', 'b.view.lkml'])
        self.assertTrue(self.kinds('released'))

    def test_default_gate_holds_broken_lookml(self):
        self.core(gate=None)
        if self.c.gate('x.view.lkml', b'view: x {') is None:
            self.skipTest('gate module not importable')
        self.at(0)
        self.put('x.view.lkml', 'view: x {\n', old=False)
        self.at(5)
        self.at(5.2)
        self.assertEqual(self.pushes, [])

    def test_git_operation_holds_then_quiet_period(self):
        git = os.path.join(self.root, '.git')
        os.makedirs(git)
        with open(os.path.join(git, 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/main\n')
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        open(os.path.join(git, 'index.lock'), 'w').close()
        self.put('a.view.lkml', 'view: a { }\n', old=False)
        self.assertIsNone(self.at(5))
        self.assertIsNone(self.at(5.5))
        os.remove(os.path.join(git, 'index.lock'))
        self.assertIsNone(self.at(5.6))      # quiet 0.3 s after git stops
        self.assertIsNotNone(self.at(5.81))
        self.assertTrue(self.held('git is busy'))

    def test_head_moving_holds(self):
        git = os.path.join(self.root, '.git')
        os.makedirs(git)
        with open(os.path.join(git, 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/main\n')
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        with open(os.path.join(git, 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/other\n')
        self.put('a.view.lkml', 'view: a { }\n', old=False)
        self.assertIsNone(self.at(5.2))
        self.assertIn('HEAD moved', self.held('git')[0][1])

    def test_root_gone_pauses_and_resumes(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        away = self.root + '-away'
        os.rename(self.root, away)
        os.makedirs(self.root)                  # an empty mount point in its place
        self.assertIsNone(self.at(5))
        self.assertEqual(len(self.kinds('paused')), 1)
        os.rmdir(self.root)
        os.rename(away, self.root)
        self.put('a.view.lkml', 'view: a { }\n', old=False)
        self.at(6)
        self.assertIsNotNone(self.at(6.2))
        self.assertEqual(len(self.kinds('resumed')), 1)

    def test_missing_config_pauses(self):
        self.core()
        self.at(0)
        os.remove(os.path.join(self.root, '.looker-sync', 'config.json'))
        self.at(1)
        self.assertIn('config.json', self.kinds('paused')[0][1])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads anything')
    def test_unreadable_folder_is_not_deleted(self):
        self.put('sub/a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        os.chmod(os.path.join(self.root, 'sub'), 0)
        for t in (5, 6):
            self.at(t)
        self.assertEqual(self.held('deleted'), [])
        self.assertEqual(self.pushes, [])

    def test_external_pause_and_resume(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        self.c.pause('branch mismatch')
        self.put('a.view.lkml', 'view: a { }\n', old=False)
        self.at(5)
        self.assertIsNone(self.at(5.2))
        self.assertEqual(self.kinds('paused')[0][1], 'branch mismatch')
        self.c.resume()
        self.assertIsNotNone(self.at(5.4))
        self.assertTrue(self.kinds('resumed'))

    def test_hold_check_holds_every_push(self):
        reason = ['branch moved']
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core(hold_check=lambda: reason[0])
        self.at(0)
        self.put('a.view.lkml', 'view: a { }\n', old=False)
        self.at(5)
        self.assertIsNone(self.at(5.2))
        reason[0] = None
        self.assertIsNotNone(self.at(5.4))


class WatchWorkerTest(WatchCoreTestCase):

    def test_push_one_hold_waits_for_new_local_bytes(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        self.answer = 'changed in Looker since last sync'
        self.put('a.view.lkml', 'view: a { }\n', old=False)
        self.at(5)
        self.at(5.2)
        self.answer = None
        self.assertIsNone(self.at(6))         # same bytes: still held
        self.assertTrue(self.held('held by Looker'))
        self.put('a.view.lkml', 'view: a {  }\n', old=False)
        self.at(8)
        self.assertIsNotNone(self.at(8.2))

    def test_push_error_backs_off_then_retries(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        self.answer = LookupError('network down')
        self.put('a.view.lkml', 'view: a { }\n', old=False)
        self.at(5)
        self.at(5.2)
        self.assertEqual(len(self.kinds('error')), 1)
        self.answer = None
        self.assertIsNone(self.at(5.5))       # backoff 1 s
        self.assertIsNotNone(self.at(6.3))
        self.assertEqual(self.pushes, [('a.view.lkml', 'view: a { }\n')])
        self.assertIsNone(self.at(7))

    def test_hung_push_never_blocks_the_scanner_and_latest_bytes_win(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        gate_open = threading.Event()
        pushes = []

        def push_one(rel, data):
            pushes.append(data)
            gate_open.wait(10)
        core = self.cli.WatchCore(self.cli.Project(self.root), push_one,
                                  remote_state=lambda: dict(self.remote),
                                  gate=lambda rel, data: None, poll=0.01)
        scans = []
        real_scan = core.proj.scan

        def counting_scan(previous=None):
            scans.append(1)
            return real_scan(previous)
        core.proj.scan = counting_scan
        core.start()
        try:
            with open(full, 'ab') as f:
                f.write(b'# 1\n')
            deadline = time.time() + 5
            while not pushes and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(pushes), 1)
            before = len(scans)
            for i in range(2, 6):
                with open(full, 'ab') as f:
                    f.write(f'# {i}\n'.encode())
                time.sleep(0.05)
            self.assertGreater(len(scans) - before, 5)   # scanner kept running
            gate_open.set()
            deadline = time.time() + 5
            while len(pushes) < 2 and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.3)
        finally:
            gate_open.set()
            core.stop()
        with open(full, 'rb') as f:
            latest = f.read()
        self.assertEqual(len(pushes), 2)
        self.assertEqual(pushes[-1], latest)


class WatchMacSaveTest(WatchCoreTestCase):
    """Editor-style atomic saves (temp file, junk names, rename onto the target) as seen on
    macOS: every save must reach Looker, and nothing may be held or skipped in silence."""

    TARGET = 'base/b0.view.lkml'
    JUNK = ('.b0.view.lkml.swp', '4913', 'b0.view.lkml~', '#b0.view.lkml#', '._b0.view.lkml',
            'b0.view.lkml___jb_tmp___', '.DS_Store', 'b0.view.lkml.orig')

    def remap_inodes(self):
        """A new file takes the most recently freed inode, as APFS and others may do."""
        proj, real_scan, real_read = self.c.proj, self.c.proj.scan, self.c.proj.read_entry
        fake, freed, live = {}, [], set()

        def ino(real):
            if real not in fake:
                fake[real] = freed.pop() if freed else real
            return fake[real]

        def scan(previous=None):
            sc = real_scan(previous=previous)
            entries = {r: e._replace(ino=ino(e.ino)) for r, e in sc.entries.items()}
            now = set(e.ino for e in entries.values())
            freed.extend(sorted(live - now))
            for real in [k for k, v in fake.items() if v in live - now]:
                del fake[real]
            live.clear()
            live.update(now)
            return sc._replace(entries=entries)

        def read_entry(rel):
            r = real_read(rel)
            if r.entry is None:
                return r
            return r._replace(entry=r.entry._replace(ino=ino(r.entry.ino)))
        proj.scan, proj.read_entry = scan, read_entry

    def rounds(self, gap, step, see_tmp, reuse=False, n=10):
        """n saves `gap` s apart, a tick every `step` s. see_tmp(i): a tick lands between
        writing the temp file and renaming it. Returns the time of the last rename."""
        self.put(self.TARGET, 'view: b0 { # start\n}\n', synced=True)
        self.core()
        if reuse:
            self.remap_inodes()
        self.at(0)
        base = os.path.join(self.root, 'base')
        t, last, i = step, None, 0
        while i < n:
            if t >= 1.0 + i * gap:
                for name in self.JUNK:
                    self.put('base/' + name, f'junk {i}\n', old=False)
                self.put(f'base/tmpXY{i}.lkml', f'scratch {i}\n', old=False)
                self.put('base/tmpsave.lkml', f'view: b0 {{ # v{i:02d}\n}}\n', old=False)
                if see_tmp(i):
                    self.at(t)
                    t += step
                os.replace(os.path.join(base, 'tmpsave.lkml'), os.path.join(base, 'b0.view.lkml'))
                os.unlink(os.path.join(base, f'tmpXY{i}.lkml'))
                last, i = t, i + 1
            self.at(t)
            t += step
        return last, t

    def versions(self):
        self.assertEqual(set(r for r, _ in self.pushes), {self.TARGET})
        return [d.split('# ')[1].split('\n')[0] for _, d in self.pushes]

    def finish(self, t, until):
        while t <= until:
            self.at(t)
            t += 0.115

    def check_spaced(self, see_tmp, reuse=False):
        last, t = self.rounds(1.5, 0.1, see_tmp, reuse)
        self.finish(t, last + 1.0)
        self.assertEqual(self.versions(), [f'v{i:02d}' for i in range(10)])
        self.assertEqual(self.kinds('held'), [])

    def check_mac_paced(self, see_tmp, reuse=False):
        last, t = self.rounds(0.65, 0.115, see_tmp, reuse)
        during = len(self.pushes)
        self.finish(t, last + 3.2)
        self.assertGreaterEqual(during, 1)                 # a busy writer is never starved
        self.assertEqual(self.versions()[-1], 'v09')       # the last save always lands
        self.assertEqual(self.versions(), sorted(self.versions()))
        self.assertEqual(self.kinds('held'), [])

    def test_spaced_saves_temp_seen_every_round(self):
        self.check_spaced(lambda i: True)

    def test_spaced_saves_temp_never_seen(self):
        self.check_spaced(lambda i: False)

    def test_spaced_saves_temp_seen_every_other_round(self):
        self.check_spaced(lambda i: i % 2 == 0)

    def test_spaced_saves_with_inode_reuse(self):
        self.check_spaced(lambda i: True, reuse=True)

    def test_spaced_saves_with_inode_reuse_alternate(self):
        self.check_spaced(lambda i: i % 2 == 1, reuse=True)

    def test_mac_paced_saves_temp_seen_every_round(self):
        self.check_mac_paced(lambda i: True)

    def test_mac_paced_saves_temp_never_seen(self):
        self.check_mac_paced(lambda i: False)

    def test_mac_paced_saves_temp_seen_every_other_round(self):
        self.check_mac_paced(lambda i: i % 2 == 0)

    def test_mac_paced_saves_with_inode_reuse(self):
        self.check_mac_paced(lambda i: True, reuse=True)


class WatchReadRetryTest(WatchCoreTestCase):

    def flaky_read(self, rel, status, times):
        real, left = self.c.proj.read_entry, [times]

        def read_entry(r):
            if r == rel and left[0] != 0:
                left[0] -= 1
                return self.cli.ReadResult(status, None, None)
            return real(r)
        self.c.proj.read_entry = read_entry

    def test_listed_file_that_reads_as_vanished_is_retried(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        with open(full, 'ab') as f:
            f.write(b'# more\n')
        self.at(5.0)
        self.flaky_read('a.view.lkml', 'vanished', 1)     # mid-rename on a Mac volume
        self.assertIsNone(self.at(5.2))                   # settles again: 5.2 + 0.15
        self.assertEqual(self.at(5.4), {'a.view.lkml': b'view: a {}\n# more\n'})
        self.assertEqual(self.kinds('held'), [])

    def test_file_that_never_holds_still_is_reported_busy(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        with open(full, 'ab') as f:
            f.write(b'# more\n')
        self.at(5.0)
        self.flaky_read('a.view.lkml', 'unstable', -1)
        for t in (5.2, 6.0, 7.0, 7.9):
            self.assertIsNone(self.at(t))
        self.assertEqual(self.held('still being written'), [])
        self.assertIsNone(self.at(8.0))
        busy = self.held('still being written')
        self.assertEqual(len(busy), 1)
        self.assertEqual(busy[0][2], ('a.view.lkml',))
        self.assertIsNone(self.at(8.2))
        self.assertEqual(len(self.held('still being written')), 1)   # once, not every tick
        del self.c.proj.read_entry                          # the writer stops
        self.assertEqual(self.at(8.4), {'a.view.lkml': b'view: a {}\n# more\n'})
        self.assertEqual([e[1] for e in self.kinds('released')], [busy[0][1]])


class WatchForgetTest(WatchCoreTestCase):

    def test_vanished_temp_names_are_forgotten(self):
        self.core()
        self.at(0)
        t = 1.0
        for i in range(20):
            full = self.put(f'tmp{i}.lkml', f'x {i}\n', old=False)
            self.at(t)
            os.unlink(full)
            self.at(t + 0.05)
            t += 0.1
        self.assertEqual(self.c.gone, {})
        self.assertEqual([r for r in self.c.last_change if r.startswith('tmp')], [])
        self.assertEqual(self.pushes, [])

    def test_deleted_synced_file_keeps_its_inode_until_looker_drops_it(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        os.unlink(full)
        self.at(1.0)
        self.assertIn('a.view.lkml', self.c.gone)
        self.c.set_remote('a.view.lkml', None)
        self.at(1.1)
        self.assertEqual(self.c.gone, {})


class WatchClockTest(WatchCoreTestCase):

    def test_cycle_is_measured_and_sets_the_stall_test(self):
        self.core(poll=0.05)
        self.assertEqual(self.c.cycle, 0.05)
        t = 0.0
        for _ in range(30):
            self.at(t)
            t += 0.3
        self.assertAlmostEqual(self.c.cycle, 0.3, places=2)
        self.at(t + 2.2)                     # 10 cycles = 3 s: a slow laptop, not a stall
        self.assertEqual([e for e in self.kinds('notice') if 'no scan' in e[1]], [])
        cycle = self.c.cycle                 # one slow tick moves it by at most 40 %
        self.assertLess(cycle, 0.3 * 1.41)
        self.at(t + 2.2 + 6.0)
        self.assertEqual(len([e for e in self.kinds('notice') if 'no scan' in e[1]]), 1)
        self.assertEqual(self.c.cycle, cycle)   # a stall is not a cycle


class WatchMtimeTickTest(WatchCoreTestCase):

    def test_probe_measures_a_tick_and_cleans_up(self):
        meta = os.path.join(self.root, '.looker-sync')
        tick = self.cli.mtime_tick(meta)
        self.assertGreater(tick, 0)
        self.assertLess(tick, 2 * 10**9)
        self.assertEqual(os.listdir(meta), ['config.json'])
        self.assertEqual(self.cli.mtime_tick(meta, budget=0), 2 * 10**9)
        self.assertEqual(self.cli.mtime_tick(os.path.join(self.root, 'nope')), 2 * 10**9)

    def test_core_measures_once_and_window_follows_it(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        dev = os.stat(self.root).st_dev
        self.assertEqual(self.c.meta_dev, dev)
        self.assertEqual(self.c._racy[dev], max(2 * self.c.mtime_tick, 2_000_000))

    def test_whole_second_mtimes_widen_the_window(self):
        for i, step in enumerate((2, 1)):
            for j in range(3):
                full = self.put(f'v{i}{j}.view.lkml', 'view: v {}\n', synced=True)
                even = (self.wall // 10**9 - 100) // 2 * 2       # odd + 1 for the 1 s case
                t = (even - 2 * j + (step == 1)) * 10**9
                os.utime(full, ns=(t, t))
            self.core()
            self.at(i)
            self.assertEqual(self.c._racy[os.stat(self.root).st_dev], 2 * step * 10**9)
            for j in range(3):
                os.unlink(os.path.join(self.root, f'v{i}{j}.view.lkml'))


class WatchCaseRenameTest(WatchCoreTestCase):

    def test_case_only_rename_never_trusts_the_old_spelling(self):
        old = self.put('b0.view.lkml', 'view: b0 {}\n', synced=True)
        self.core()
        self.at(0)
        new = os.path.join(self.root, 'B0.view.lkml')
        os.replace(old, new)
        real_stat, real_lstat, real_exists = os.stat, os.lstat, os.path.exists

        def lie(fn):
            def wrapped(path, *a, **kw):
                if os.fspath(path) == old:          # a case-insensitive volume answers
                    return fn(new, *a, **kw)
                return fn(path, *a, **kw)
            return wrapped
        reads, real_read = [], self.c.proj.read_entry

        def read_entry(rel):
            reads.append(rel)
            return real_read(rel)
        self.c.proj.read_entry = read_entry
        with mock.patch('os.stat', lie(real_stat)), mock.patch('os.lstat', lie(real_lstat)), \
                mock.patch('os.path.exists', lie(real_exists)):
            self.at(5.0)
            self.at(5.2)
        self.assertEqual(self.pushes, [])
        self.assertNotIn('b0.view.lkml', reads)
        moves = self.held('moved')
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][2], ('b0.view.lkml -> B0.view.lkml',))


class PullCaseCollisionTest(FakeLookerTestCase):

    def test_paths_differing_only_by_case_are_refused_on_every_os(self):
        self.init()
        self.fake.dev_write('views/Users.view.lkml', 'view: users_a {}\n')
        self.fake.dev_write('views/users.view.lkml', 'view: users_b {}\n')
        self.fake.dev_write('views/new.view.lkml', 'view: new {}\n')
        for args in (['pull', '--dry-run'], ['pull'], ['pull', '--force']):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn('views/Users.view.lkml and views/users.view.lkml', r.stdout)
        for name in ('Users.view.lkml', 'users.view.lkml', 'new.view.lkml'):
            self.assertFalse(os.path.exists(os.path.join(self.dir, 'views', name)), name)

class WatchNetTestCase(FakeLookerTestCase):
    """WatchNet in this process against the fake: real threads, lines collected in a list."""

    def setUp(self):
        super().setUp()
        self.cli = import_cli()
        self.init()
        self.lines = []
        self.nets = []

    def tearDown(self):
        for w in self.nets:
            w.close()
        self.fake.drop_connections()
        super().tearDown()

    def watch(self, start=True, **kw):
        import argparse
        proj = self.cli.Project(self.dir)
        sess = self.cli.open_session(proj, argparse.Namespace(env_file=None), fetch=False)
        w = self.cli.WatchNet(proj, sess, write_line=self.lines.append, **kw)
        self.nets.append(w)
        if start:
            w.start()
            time.sleep(0.3)      # let the first scan take its baseline
        return w

    def wait_for(self, cond, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    def kinds(self, kind):
        return [ln for ln in self.lines if ln.split()[1:2] == [kind]]

    def has(self, kind, text=''):
        return any(text in ln for ln in self.kinds(kind))

    def state_sha(self, rel):
        return self.cli.Project(self.dir).state.get('files', {}).get(rel)

    def content_writes(self):
        return [e for e in self.writes() if e[1].endswith('/files')]


class WatchNetPushTest(WatchNetTestCase):
    """One save: ack, then verified, and only verified bytes reach state.json."""

    def test_save_prints_ack_then_verified_and_records(self):
        self.watch()
        rel, text = 'views/orders.view.lkml', 'view: orders { # watched\n}\n'
        self.put_local(rel, text)
        self.assertTrue(self.wait_for(lambda: self.has('verified', rel)), self.lines)
        ack = next(i for i, ln in enumerate(self.lines) if ln.split()[1:3] == ['ack', rel])
        ver = next(i for i, ln in enumerate(self.lines) if ln.split()[1:3] == ['verified', rel])
        self.assertLess(ack, ver, self.lines)
        self.assertRegex(self.lines[ack], r'^\d\d:\d\d:\d\d\.\d{3} ack      views/orders\.view\.lkml$')
        self.assertEqual(self.fake.dev_files()[rel], text)
        self.assertEqual(self.state_sha(rel), self.cli.sha(text.encode()))
        self.assertEqual(len(self.content_writes()), 1)

    def test_readback_mismatch_is_not_recorded_and_next_save_holds(self):
        self.watch()
        rel = 'views/orders.view.lkml'
        before = self.state_sha(rel)
        self.fake.mangle_readback.add(rel)
        self.put_local(rel, 'view: orders { # one\n}\n')
        self.assertTrue(self.wait_for(lambda: self.has('MISMATCH', rel)), self.lines)
        self.assertEqual(self.state_sha(rel), before)       # never recorded as synced
        n = len(self.content_writes())
        self.put_local(rel, 'view: orders { # two\n}\n')
        self.assertTrue(self.wait_for(lambda: self.has('held')), self.lines)
        self.assertTrue(any('MISMATCH' in ln for ln in self.kinds('held')), self.lines)
        self.assertEqual(len(self.content_writes()), n)      # held, not overwritten

    def test_watch_never_deletes(self):
        self.watch(delete=False)
        os.remove(os.path.join(self.dir, 'views', 'orders.view.lkml'))
        time.sleep(1.5)
        self.assertEqual([e for e in self.writes() if e[0] == 'DELETE'], [])
        self.assertIn('views/orders.view.lkml', self.fake.dev_files())


class WatchNetConflictTest(WatchNetTestCase):
    """A file changed in Looker since the last sync is held, never overwritten."""

    def test_file_changed_in_looker_is_held_not_written(self):
        self.watch()
        rel = 'views/orders.view.lkml'
        self.fake.uncommitted[rel] = 'view: orders { # edited in the IDE\n}\n'
        self.put_local(rel, 'view: orders { # edited here\n}\n')
        self.assertTrue(self.wait_for(lambda: self.has('held')), self.lines)
        self.assertTrue(any('changed in Looker' in ln for ln in self.kinds('held')), self.lines)
        self.assertTrue(self.has('held', rel), self.lines)
        self.assertEqual(self.content_writes(), [])
        self.assertEqual(self.fake.dev_files()[rel], 'view: orders { # edited in the IDE\n}\n')

    def test_force_overwrites_looker_change(self):
        self.watch(force=True)
        rel = 'views/orders.view.lkml'
        self.fake.uncommitted[rel] = 'view: orders { # edited in the IDE\n}\n'
        self.put_local(rel, 'view: orders { # edited here\n}\n')
        self.assertTrue(self.wait_for(lambda: self.has('verified', rel)), self.lines)
        self.assertEqual(self.fake.dev_files()[rel], 'view: orders { # edited here\n}\n')

    def test_syntax_error_hold_points_at_one_shot_push(self):
        self.watch()
        rel = 'views/orders.view.lkml'
        self.put_local(rel, 'view: orders {\n  dimension: id {\n')
        self.assertTrue(self.wait_for(lambda: self.has('held', rel)), self.lines)
        path_line = next(ln for ln in self.kinds('held') if rel in ln)
        self.assertIn(self.cli.GATE_HINT, path_line)
        self.assertIn('looker-sync push', path_line)
        self.assertEqual(self.content_writes(), [])


class WatchNetBranchTest(WatchNetTestCase):
    """A branch switch in Looker pauses the watcher; switching back resumes it."""

    def test_branch_switch_pauses_then_resumes(self):
        w = self.watch(start=False)
        w.BRANCH_EVERY = 0.2
        w.start()
        time.sleep(0.3)
        self.fake.dev_branch = 'master'
        self.assertTrue(self.wait_for(lambda: self.has('paused', "on branch 'master'")), self.lines)
        rel = 'views/orders.view.lkml'
        self.put_local(rel, 'view: orders { # while paused\n}\n')
        time.sleep(1.0)
        self.assertEqual(self.content_writes(), [])           # nothing written while paused
        self.fake.dev_branch = 'feat'
        self.assertTrue(self.wait_for(lambda: self.has('resumed')), self.lines)
        self.assertTrue(self.wait_for(lambda: self.has('verified', rel)), self.lines)
        self.assertEqual(self.fake.dev_files()[rel], 'view: orders { # while paused\n}\n')

    def test_token_swap_onto_other_branch_keeps_old_token(self):
        w = self.watch()
        self.fake.dev_branch = 'master'
        with self.assertRaises(self.cli.LookerError):
            w._before_swap(w.L.token)
        self.assertTrue(self.has('paused', 'token refresh'), self.lines)


class WatchNetAuthTest(WatchNetTestCase):
    """A 401 that survives the one retry pauses with a message; a good login resumes."""

    def test_401_after_retry_pauses_then_resumes(self):
        w = self.watch(start=False)
        w.BRANCH_EVERY = 0.2
        w.start()
        time.sleep(0.3)
        self.fake.force_401 = 10_000
        self.put_local('views/orders.view.lkml', 'view: orders { # auth\n}\n')
        self.assertTrue(self.wait_for(lambda: self.has('paused', 'refused the session')), self.lines)
        self.assertEqual(self.content_writes(), [])
        self.fake.force_401 = 0
        self.assertTrue(self.wait_for(lambda: self.has('resumed')), self.lines)
        self.assertTrue(self.wait_for(lambda: self.has('verified', 'views/orders.view.lkml')), self.lines)


class WatchNetValidateTest(WatchNetTestCase):
    """--validate runs Looker validation once after a quiet period; off by default."""

    def validations(self):
        return [e for e in self.fake.log if e[1].endswith('/validate')]

    def test_validate_runs_once_after_quiet(self):
        w = self.watch(start=False, validate=True)
        w.QUIET = 0.3
        w.start()
        time.sleep(0.3)
        self.put_local('views/orders.view.lkml', 'view: orders { # a\n}\n')
        self.put_local('models/demo.model.lkml', 'connection: "db2"\n')
        self.assertTrue(self.wait_for(lambda: w.validations == 1), self.lines)
        time.sleep(1.0)
        self.assertEqual(w.validations, 1)
        self.assertEqual(len(self.validations()), 1)

    def test_no_validation_by_default(self):
        self.watch()
        self.put_local('views/orders.view.lkml', 'view: orders { # a\n}\n')
        self.assertTrue(self.wait_for(lambda: self.has('verified', 'views/orders.view.lkml')))
        time.sleep(2.5)
        self.assertEqual(self.validations(), [])


class WatchNetLifecycleTest(WatchNetTestCase):
    """Prod mode refuses; close() leaves no thread; Ctrl-C exits 0."""

    def test_close_leaves_no_thread(self):
        w = self.watch()
        self.put_local('views/orders.view.lkml', 'view: orders { # a\n}\n')
        self.assertTrue(self.wait_for(lambda: self.has('verified', 'views/orders.view.lkml')))
        w.close()
        self.nets.remove(w)
        left = [t.name for t in threading.enumerate()
                if t.name.startswith(('watch-', 'looker-token-refresh'))]
        self.assertEqual(left, [])

    def test_ctrl_c_exits_zero(self):
        import signal
        p = subprocess.Popen([sys.executable, CLI, '-C', self.dir, 'watch'], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
        try:
            first = ''
            while 'watching' not in first:
                ln = p.stdout.readline()
                self.assertTrue(ln, first)
                first += ln
            time.sleep(0.5)
            p.send_signal(signal.SIGINT)
            rest, _ = p.communicate(timeout=20)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, first + rest)
        self.assertIn('stopped', rest)
        self.assertNotIn('Traceback', rest)
        self.assertNotIn(SECRET, first + rest)


class WatchNetProdTest(FakeLookerTestCase):
    def test_watch_refused_in_prod_mode(self):
        r = self.run_cli('init', '--project', PROJECT, '--mode', 'prod')
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.run_cli('watch')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('refused in prod mode', r.stdout + r.stderr)
        self.assertEqual(self.writes(), [])


class WatchSyncedMemoTest(WatchCoreTestCase):
    """Idle watch tests each path once, and the memo does not hide new or changed files."""

    def test_repeat_ticks_add_no_sync_set_work(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        first = self.c.proj.synced.cache_info().misses
        self.assertGreater(first, 0)
        self.at(1)
        self.at(2)
        self.assertEqual(self.c.proj.synced.cache_info().misses, first)

    def test_new_path_is_still_decided_and_seen(self):
        self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        self.put('b.view.lkml', 'view: b {}\n')
        self.put('notes.txt', 'not LookML\n')
        self.at(1)
        self.assertIn('b.view.lkml', self.c.last_scan.entries)
        self.assertNotIn('notes.txt', self.c.last_scan.entries)

    def test_same_path_edit_is_seen_through_the_memo(self):
        full = self.put('a.view.lkml', 'view: a {}\n', synced=True)
        self.core()
        self.at(0)
        with open(full, 'ab') as f:
            f.write(b'# more\n')
        self.at(5.0)
        self.assertEqual(self.at(5.16), {'a.view.lkml': b'view: a {}\n# more\n'})


class GlobRegexCacheTest(unittest.TestCase):
    """The sync-set check runs per path per scan: one compile per pattern keeps idle watch cheap."""

    def test_pattern_compiled_once(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
        from looker_sync import cli
        self.assertIs(cli.glob_regex('**/*.view.lkml'), cli.glob_regex('**/*.view.lkml'))
        self.assertTrue(cli.glob_regex('**/*.view.lkml').match('a/b.view.lkml'))
        self.assertFalse(cli.glob_regex('*.view.lkml').match('a/b.view.lkml'))


class WatchDeleteNetCase(WatchNetTestCase):
    ORDERS = 'views/orders.view.lkml'

    def deletes(self):
        return [e for e in self.fake.log if e[0] == 'DELETE']

    def path(self, rel):
        return os.path.join(self.dir, *rel.split('/'))

    def git(self, *args):
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
                       cwd=self.dir, env=hermetic_git_env(self.tmp.name), check=True,
                       capture_output=True)

    def git_repo(self):
        self.git('init', '-q')
        os.makedirs(os.path.join(self.dir, '.git', 'info'), exist_ok=True)
        with open(os.path.join(self.dir, '.git', 'info', 'exclude'), 'a') as f:
            f.write('.env\n.looker-sync/\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')

    def synced_extra(self):
        rel = 'views/extra.view.lkml'
        self.put_local(rel, 'view: extra {}\n')
        r = self.run_cli('push')
        self.assertEqual(r.returncode, 0, r.stdout)
        return rel


class WatchDeleteTest(WatchDeleteNetCase):
    """Default watch: a local delete, rename or copy reaches Looker."""

    def test_delete_prints_deleted_once_and_leaves_every_record(self):
        w = self.watch()
        w.core.DELETE_GRACE = 0.2
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS)), self.lines)
        self.assertNotIn(self.ORDERS, self.fake.dev_files())
        self.assertIsNone(self.state_sha(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: self.ORDERS not in w.core.gone), self.lines)
        self.assertNotIn(self.ORDERS, w.known)
        self.assertNotIn(self.ORDERS, w.core.synced)
        self.assertEqual(len(self.kinds('deleted')), 1, self.lines)
        self.assertEqual(len(self.deletes()), 1)
        r = self.run_cli('status')
        self.assertNotIn(self.ORDERS, r.stdout)

    def test_rename_writes_new_then_deletes_old(self):
        self.watch()
        new = 'views/renamed.view.lkml'
        os.rename(self.path(self.ORDERS), self.path(new))
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS)), self.lines)
        files = self.fake.dev_files()
        self.assertEqual(files[new], 'view: orders {}\n')
        self.assertNotIn(self.ORDERS, files)
        methods = [e[0] for e in self.fake.log if e[0] in ('POST', 'DELETE')
                   and e[1].endswith('/files')]
        self.assertEqual(methods, ['POST', 'DELETE'])
        self.assertTrue(self.has('deleted', f'moved to {new}'), self.lines)

    def test_rename_with_edit_writes_new_then_deletes_old(self):
        self.watch()
        new = 'views/renamed.view.lkml'
        os.rename(self.path(self.ORDERS), self.path(new))
        with open(self.path(new), 'a') as f:
            f.write('# edited\n')
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS)), self.lines)
        files = self.fake.dev_files()
        self.assertEqual(files[new], 'view: orders {}\n# edited\n')
        self.assertNotIn(self.ORDERS, files)

    def test_copy_is_written_with_no_suspects_hold(self):
        self.watch()
        new = 'views/copy.view.lkml'
        shutil.copy(self.path(self.ORDERS), self.path(new))
        self.assertTrue(self.wait_for(lambda: self.has('verified', new)), self.lines)
        self.assertEqual(self.kinds('held'), [])
        self.assertEqual(self.deletes(), [])
        self.assertIn(self.ORDERS, self.fake.dev_files())


class WatchNoDeleteTest(WatchDeleteNetCase):
    """--no-delete keeps the v0.3.0 holds."""

    def test_missing_file_is_held_not_deleted(self):
        self.watch(delete=False)
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: self.has('held', 'watch never deletes')), self.lines)
        self.assertEqual(self.deletes(), [])

    def test_move_is_held_and_nothing_is_written(self):
        self.watch(delete=False)
        new = 'views/renamed.view.lkml'
        os.rename(self.path(self.ORDERS), self.path(new))
        self.assertTrue(self.wait_for(lambda: self.has('held', 'moved file')), self.lines)
        self.assertTrue(self.has('held', f'{self.ORDERS} -> {new}') or
                        any(f'{self.ORDERS} -> {new}' in ln for ln in self.lines), self.lines)
        self.assertEqual(self.deletes(), [])
        self.assertNotIn(new, self.fake.dev_files())

    def test_copy_is_held_as_a_suspect(self):
        self.watch(delete=False)
        new = 'views/copy.view.lkml'
        shutil.copy(self.path(self.ORDERS), self.path(new))
        self.assertTrue(self.wait_for(lambda: self.has('held', 'look like a copy')), self.lines)
        self.assertNotIn(new, self.fake.dev_files())

    def test_no_delete_flag_reaches_watch_through_argparse(self):
        import signal
        p = subprocess.Popen([sys.executable, CLI, '-C', self.dir, 'watch', '--no-delete'],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True)
        try:
            first = ''
            while 'watching' not in first:
                ln = p.stdout.readline()
                self.assertTrue(ln, first)
                first += ln
            time.sleep(0.5)
            os.remove(self.path(self.ORDERS))
            time.sleep(2.5)
            p.send_signal(signal.SIGINT)
            rest, _ = p.communicate(timeout=20)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, first + rest)
        self.assertIn('watch never deletes', rest)
        self.assertEqual(self.deletes(), [])
        self.assertIn(self.ORDERS, self.fake.dev_files())


class WatchDeleteSafetyTest(WatchDeleteNetCase):
    """The ways a delete could reach Looker when nobody meant one."""

    def test_rule1_real_git_checkout_removing_files_sends_no_delete(self):
        self.git_repo()
        self.git('branch', 'old')
        extra = self.synced_extra()
        self.git('add', '-A')
        self.git('commit', '-qm', 'extra')
        self.watch()
        self.git('checkout', '-q', 'old')
        self.assertFalse(os.path.exists(self.path(extra)))
        self.assertTrue(self.wait_for(lambda: any('push --delete' in ln for ln in self.kinds('held'))
                                      and self.has('held', extra)), self.lines)
        time.sleep(2.0)
        self.assertEqual(self.deletes(), [])
        self.assertIn(extra, self.fake.dev_files())

    def test_rule1_real_git_reset_hard_on_one_branch_sends_no_delete(self):
        self.git_repo()
        extra = self.synced_extra()
        self.git('add', '-A')
        self.git('commit', '-qm', 'extra')
        self.watch()
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.assertTrue(self.wait_for(lambda: any('push --delete' in ln for ln in self.kinds('held'))
                                      and self.has('held', extra)), self.lines)
        time.sleep(2.0)
        self.assertEqual(self.deletes(), [])
        self.assertIn(extra, self.fake.dev_files())

    def test_rule2_unlink_then_recreate_under_grace_sends_no_delete(self):
        w = self.watch()
        self.assertGreaterEqual(w.core.DELETE_GRACE, 1.0)
        os.remove(self.path(self.ORDERS))
        time.sleep(0.5)
        self.put_local(self.ORDERS, 'view: orders {}\n')
        time.sleep(2.5)
        self.assertEqual(self.deletes(), [])
        self.assertIn(self.ORDERS, self.fake.dev_files())

    def test_rule7_rename_whose_write_fails_leaves_the_old_path(self):
        self.watch()
        new = 'views/renamed.view.lkml'
        self.fake.fail_writes.add(new)
        os.rename(self.path(self.ORDERS), self.path(new))
        self.assertTrue(self.wait_for(lambda: any(new in ln and 'error' in ln for ln in self.lines)),
                        self.lines)
        time.sleep(2.5)
        self.assertEqual(self.deletes(), [])
        self.assertIn(self.ORDERS, self.fake.dev_files())
        self.fake.fail_writes.clear()
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS), 20), self.lines)
        self.assertIn(new, self.fake.dev_files())
        self.assertNotIn(self.ORDERS, self.fake.dev_files())

    def test_delete_failure_retries_and_keeps_state(self):
        w = self.watch()
        w.core.DELETE_GRACE = 0.2
        self.fake.fail_deletes.add(self.ORDERS)
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: any(self.ORDERS in ln and 'error' in ln
                                                  for ln in self.lines)), self.lines)
        self.assertFalse(self.has('deleted', self.ORDERS), self.lines)
        self.assertIsNotNone(self.state_sha(self.ORDERS))
        self.fake.fail_deletes.clear()
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS), 20), self.lines)
        self.assertNotIn(self.ORDERS, self.fake.dev_files())


class WatchDeleteRulesNetTest(WatchDeleteNetCase):
    """Rules 3, 4, 5, 6 (prod) and 8 against the fake Looker."""

    def test_rule4_file_only_in_looker_is_untouched(self):
        only = 'views/only.view.lkml'
        self.fake.dev_write(only, 'view: only {}\n')
        w = self.watch()
        w.core.DELETE_GRACE = 0.2
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS)), self.lines)
        self.assertIn(only, self.fake.dev_files())
        self.assertEqual(len(self.deletes()), 1)

    def test_rule5_changed_in_looker_is_held_then_force_deletes(self):
        w = self.watch()
        w.core.DELETE_GRACE = 0.2
        self.fake.uncommitted[self.ORDERS] = 'view: orders { # edited in the IDE\n}\n'
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: any('changed in Looker' in ln for ln in self.kinds('held'))),
                        self.lines)
        self.assertEqual(self.deletes(), [])
        self.assertIn(self.ORDERS, self.fake.dev_files())

    def test_rule5_force_deletes_a_file_changed_in_looker(self):
        w = self.watch(force=True)
        w.core.DELETE_GRACE = 0.2
        self.fake.uncommitted[self.ORDERS] = 'view: orders { # edited in the IDE\n}\n'
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS)), self.lines)
        self.assertEqual(len(self.deletes()), 1)

    def test_rule5_already_gone_in_looker_drops_state_and_reports(self):
        w = self.watch()
        w.core.DELETE_GRACE = 0.2
        self.fake.dev_write(self.ORDERS, None)
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS)), self.lines)
        self.assertTrue(self.has('deleted', 'already gone'), self.lines)
        self.assertIsNone(self.state_sha(self.ORDERS))
        self.assertEqual(self.deletes(), [])

    def test_rule6_prod_mode_refuses_in_delete_one(self):
        w = self.watch(start=False)
        w.sess.mode = 'production'
        with self.assertRaises(self.cli.Refused):
            w.delete_one(self.ORDERS)
        self.assertEqual(self.deletes(), [])
        self.assertIn(self.ORDERS, self.fake.dev_files())

    def test_rule8_readback_still_present_prints_no_deleted(self):
        w = self.watch()
        w.core.DELETE_GRACE = 0.2
        self.fake.ignore_deletes.add(self.ORDERS)
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: any('still has the file' in ln for ln in self.lines)),
                        self.lines)
        self.assertFalse(self.has('deleted', self.ORDERS), self.lines)
        self.assertIsNotNone(self.state_sha(self.ORDERS))
        self.assertIn(self.ORDERS, w.core.synced)

    def test_rule8_queued_readback_cannot_restore_a_deleted_path(self):
        w = self.watch(start=False)
        w._drop(self.ORDERS)
        self.assertFalse(w._record_unless_dropped(self.ORDERS, 'abc'))
        self.assertIsNone(self.state_sha(self.ORDERS))
        self.assertNotIn(self.ORDERS, w.known)

    def test_rule9_no_ref_in_any_branch_body_during_a_delete_run(self):
        w = self.watch()
        w.core.DELETE_GRACE = 0.2
        os.remove(self.path(self.ORDERS))
        self.assertTrue(self.wait_for(lambda: self.has('deleted', self.ORDERS)), self.lines)
        bodies = [e[3] for e in self.fake.log if e[1].endswith('git_branch') and e[3]]
        self.assertFalse([b for b in bodies if 'ref' in b], bodies)


class WatchDeleteCoreTest(WatchCoreTestCase):
    """Rules 3, 5 and 6 on the core, with a fake clock and a fake delete_one."""
    A, B = 'views/a.view.lkml', 'views/b.view.lkml'

    def setUp(self):
        super().setUp()
        self.dels, self.del_answer = [], None

    def delete_one(self, rel, new_path, new_sha):
        self.dels.append((rel, new_path))
        return self.del_answer

    def start(self, **kw):
        self.a_full = self.put(self.A, 'view: a {}\n', synced=True)
        self.core(delete_one=self.delete_one, **kw)
        self.at(0)

    def run_to(self, a, b, step=0.25):
        t = a
        while t <= b:
            self.at(t)
            t += step

    def test_rule3_missing_at_start_is_not_deleted(self):
        full = self.put(self.A, 'view: a {}\n', synced=True)
        os.remove(full)
        self.core(delete_one=self.delete_one)
        self.run_to(0, 6)
        self.assertEqual(self.dels, [])
        self.assertTrue(self.held('already missing'), self.events)

    def test_rule5_held_delete_is_tried_once_until_the_file_returns(self):
        self.start()
        self.del_answer = 'changed in Looker'
        os.remove(self.a_full)
        self.run_to(1, 10)
        self.assertEqual(len(self.dels), 1, self.events)
        self.assertFalse(self.kinds('deleted'))
        self.put(self.A, 'view: a {}\n')
        self.run_to(10.25, 12)
        os.remove(self.a_full)
        self.run_to(12.25, 20)
        self.assertEqual(len(self.dels), 2, self.events)

    def test_rule2_lstat_finding_the_file_after_the_plan_keeps_it(self):
        self.start()
        os.remove(self.a_full)
        self.run_to(1, 2)
        real = self.c.proj.scan

        def scan(previous=None):
            s = real(previous=previous)
            self.put(self.A, 'view: a {}\n')
            return s
        self.c.proj.scan = scan
        self.at(3)
        self.c.proj.scan = real
        self.assertEqual(self.dels, [])

    def assert_held_delete(self):
        self.assertEqual(self.dels, [], self.events)
        self.assertEqual(self.pushes, [], self.events)

    def test_rule6_external_pause_holds_a_delete_only_tick(self):
        self.start()
        self.c.external_pause = True
        os.remove(self.a_full)
        self.run_to(1, 6)
        self.assert_held_delete()

    def test_rule6_hold_check_holds_a_delete_only_tick(self):
        self.start()
        self.c.hold_check = lambda: 'wrong branch'
        os.remove(self.a_full)
        self.run_to(1, 6)
        self.assert_held_delete()
        self.assertTrue(self.held('wrong branch'), self.events)

    def test_rule6_root_gone_holds_a_delete(self):
        self.start()
        shutil.move(self.root, self.root + '.away')
        self.run_to(1, 6)
        self.assert_held_delete()

    def test_rule6_unreadable_folder_holds_a_delete_elsewhere(self):
        self.start()
        locked = os.path.join(self.root, 'locked')
        os.makedirs(locked)
        os.remove(self.a_full)
        self.at(1)
        os.chmod(locked, 0)
        self.run_to(1.25, 6)
        self.assert_held_delete()
        self.assertTrue(self.held('cannot be read'), self.events)

    def test_rule6_git_busy_holds_a_delete(self):
        self.start()
        with mock.patch.object(self.c, '_git_busy', return_value='git is busy'):
            os.remove(self.a_full)
            self.run_to(1, 6)
        self.assert_held_delete()

    def test_rule6_ceiling_counts_writes_plus_deletes(self):
        b = self.put(self.B, 'view: b {}\n', synced=True)
        self.start(ceiling=2)
        os.remove(self.a_full)
        os.remove(b)
        self.put('views/c.view.lkml', 'view: c {}\n')
        self.at(1)
        self.run_to(1.25, 6)
        self.assert_held_delete()
        self.assertTrue(self.held('more than 2'), self.events)

    def test_rule6_syntax_gate_failure_holds_a_due_delete(self):
        self.start(gate=lambda rel, data: 'bad' if b'broken' in data else None)
        os.remove(self.a_full)
        self.put('views/c.view.lkml', 'view: c { broken\n')
        self.run_to(1, 6)
        self.assert_held_delete()
        self.assertTrue(self.held('syntax error'), self.events)


class WatchDeleteBugTest(WatchDeleteCoreTest):
    """Each of these was found by review; the test came first."""

    def test_overwrite_rename_keeps_old_path_while_new_is_held(self):
        b = self.put(self.B, 'view: b {}\n', synced=True)
        self.start()
        os.replace(self.a_full, b)      # b now holds a's bytes: it changed, and a is gone
        self.answer = 'changed in Looker'
        self.run_to(1, 8)
        self.assertEqual(self.dels, [], self.events)
        self.assertTrue(self.held('new path is held'), self.events)

    def test_overwrite_rename_deletes_old_path_after_new_is_written(self):
        b = self.put(self.B, 'view: b {}\n', synced=True)
        self.start()
        os.replace(self.a_full, b)
        self.run_to(1, 8)
        self.assertEqual([p[0] for p in self.pushes], [self.B], self.events)
        self.assertEqual(self.dels, [(self.A, self.B)], self.events)

    def test_old_path_waits_for_a_renamed_file_still_being_written(self):
        self.start()
        new = os.path.join(self.root, *self.B.split('/'))
        os.rename(self.a_full, new)         # same inode: this is a rename, not a new file
        t, n = 1.0, 0
        while t <= 9:
            n += 1
            if t < 5:
                with open(new, 'a') as f:   # churning past the grace period and past MAX_WAIT
                    f.write(f'# {n}\n')
            self.at(t)
            if self.dels:
                self.assertTrue(self.pushes, f'old path deleted before the new one at {t}')
            t += 0.25
        self.assertEqual(self.dels, [(self.A, self.B)], self.events)

    def test_ref_moved_just_before_the_files_vanish_sends_no_delete(self):
        self.start()
        with mock.patch.object(self.c, '_git_refs_moved', return_value=True):
            self.at(1)
        os.remove(self.a_full)                  # next scan: refs quiet, no lock, file gone
        self.run_to(1.25, 6)
        self.assertEqual(self.dels, [], self.events)
        self.assertTrue(self.held('vanished during a git operation'), self.events)

    def test_case_only_rename_deletes_the_old_spelling_simulated(self):
        """Simulated: lstat is patched. Linux has no case-insensitive volume to run this on."""
        low, up = 'b0.view.lkml', 'B0.view.lkml'
        old = self.put(low, 'view: b0 {}\n', synced=True)
        self.core(delete_one=self.delete_one)
        self.at(0)
        new = os.path.join(self.root, up)
        os.replace(old, new)
        real = os.lstat

        def lstat(path, *a, **kw):
            return real(new if os.fspath(path) == old else path, *a, **kw)
        with mock.patch('os.lstat', lstat):
            self.run_to(1, 8)
        self.assertEqual([p[0] for p in self.pushes], [up], self.events)
        self.assertEqual(self.dels, [(low, up)], self.events)


class WatchDeleteGitStashTest(WatchDeleteNetCase):

    def test_real_git_stash_u_removing_a_synced_file_sends_no_delete(self):
        self.git_repo()
        extra = self.synced_extra()         # synced but untracked: only -u removes it
        self.watch()
        self.git('stash', '-u', '-q')
        self.assertFalse(os.path.exists(self.path(extra)))
        self.assertTrue(self.wait_for(lambda: self.has('held', extra)), self.lines)
        time.sleep(2.0)
        self.assertEqual(self.deletes(), [])
        self.assertIn(extra, self.fake.dev_files())


class WatchDeleteSettlingOverwriteTest(WatchDeleteCoreTest):

    def test_old_path_stays_while_an_overwritten_synced_target_never_settles(self):
        b = self.put(self.B, 'view: b {}\n', synced=True)
        self.start()
        os.replace(self.a_full, b)
        t, n = 1.0, 0
        while t <= 9:
            n += 1
            with open(b, 'a') as f:
                f.write(f'# {n}\n')
            self.at(t)
            t += 0.25
        self.assertEqual(self.dels, [], self.events)


class WatchDeleteStuckTargetTest(WatchDeleteCoreTest):

    def test_old_path_stays_while_the_new_path_can_never_be_read_stably(self):
        self.start()
        new = os.path.join(self.root, *self.B.split('/'))
        os.rename(self.a_full, new)
        real = self.c.proj.read_entry

        def read_entry(rel):
            r = real(rel)
            return self.cli.ReadResult('unstable', None, r.entry) if rel == self.B else r
        self.c.proj.read_entry = read_entry
        self.run_to(1, 9)
        self.assertEqual(self.dels, [], self.events)
        self.assertEqual(self.pushes, [], self.events)
        self.assertTrue(self.held('still being written'), self.events)


if __name__ == '__main__':
    unittest.main()
