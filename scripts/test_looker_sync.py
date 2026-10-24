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
import subprocess
import sys
import tempfile
import threading
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
    'bare': (True, 'bare_models/demo.git', None),
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
        self.fail_checkout = set()  # checking out these branches answers 500
        # git fields, shaped as on a live instance
        self.production_branch = 'master'
        self.set_git_remote('remote')
        self.branch_git = {}         # branch -> fields overriding the branch resource defaults
        self.remote_only = set()     # pushed with plain git, never checked out in Looker
        self.personal = {'feat'}     # live: the user's own dev branch is personal
        self.readonly = {'master'}   # live: the production branch is readonly in dev mode

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

    def branch_resource(self, name):
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

    def serve(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
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
                    tok = f'tok{len(fake.tokens)}'
                    fake.tokens[tok] = 'production'
                    return self.reply(200, {'access_token': tok})
                tok = (self.headers.get('Authorization') or '').replace('token ', '')
                if tok not in fake.tokens:
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
                        return self.reply(200, fake.branch_resource(fake.dev_branch if dev else fake.production_branch))
                    if not dev:
                        return self.reply(403, {'message': 'dev mode only'})
                    if 'ref' in body:
                        return self.reply(500, {'message': 'test fake: ref is forbidden'})
                    if body['name'] not in fake.branches:
                        return self.reply(404, {'message': 'Not found'})
                    if body['name'] in fake.fail_checkout:
                        return self.reply(500, {'message': 'test fake: checkout failed'})
                    fake.dev_branch = body['name']
                    fake.remote_only.discard(fake.dev_branch)   # live: checkout by name makes it local
                    return self.reply(200, fake.branch_resource(fake.dev_branch))
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
        self.fake.set_git_remote('bare')
        self.assertFalse(self.looker_get(f'/projects/{PROJECT}')['git_remote_url'].startswith(('git@', 'https://', '/')))
        self.fake.set_git_remote('none')
        proj = self.looker_get(f'/projects/{PROJECT}')
        self.assertEqual((proj['uses_git'], proj['git_remote_url']), (False, None))
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
        self.assertEqual((listed['review']['is_local'], listed['review']['is_remote'], listed['review']['ref']),
                         (False, True, review))
        self.assertEqual((listed['master']['readonly'], listed['master']['is_production']), (True, True))
        self.assertEqual((listed['feat']['personal'], listed['feat']['readonly']), (True, False))
        self.git('fetch', '-q', 'origin')
        self.assertEqual(self.git('rev-parse', 'origin/review'), review)
        r = self.run_cli('init', '--project', PROJECT, '--branch', 'feat')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_cli('switch', '--branch', 'review', '--yes')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.looker_get(f'/projects/{PROJECT}/git_branch')['is_local'])


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
        reads = {'rev-parse', 'symbolic-ref', 'status', 'cat-file', 'merge-base', 'remote', 'fetch'}
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
                                                   'remote', 'fetch', 'stash', 'checkout', 'merge'}, set())
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


if __name__ == '__main__':
    unittest.main()
