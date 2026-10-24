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

    def run_cli(self, *args):
        r = subprocess.run([sys.executable, CLI, '-C', self.dir, *args], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=60)
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
    hermetic_git_env through os.environ for the length of the test.
    """

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


if __name__ == '__main__':
    unittest.main()
