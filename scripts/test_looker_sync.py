"""Tests for looker_sync.py: the CLI runs as a subprocess against a fake Looker.

    python3 -m unittest discover -s scripts -p 'test_looker_sync.py'

The fake keeps files, branches, a workspace per token and per-file git status in memory,
and imitates the Looker behaviour proven live: tokens start in production, the dev
workspace is per user, branch checkout carries uncommitted files, POST/PUT/DELETE files.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'looker_sync.py')
PROJECT = 'demo'
SECRET = 'not-a-real-secret-9f8e7d'


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
                if path == pre + '/git_branch':
                    if method == 'GET':
                        return self.reply(200, {'name': fake.dev_branch if dev else 'master'})
                    if not dev:
                        return self.reply(403, {'message': 'dev mode only'})
                    if 'ref' in body:
                        return self.reply(500, {'message': 'test fake: ref is forbidden'})
                    if body['name'] not in fake.branches:
                        return self.reply(404, {'message': 'Not found'})
                    fake.dev_branch = body['name']
                    return self.reply(200, {'name': fake.dev_branch})
                if path == pre + '/git_branches':
                    return self.reply(200, [{'name': b} for b in fake.branches])
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


class CLITest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLooker()
        self.fake.uncommitted = {'models/demo.model.lkml': 'connection: "db"\n',
                                 'views/orders.view.lkml': 'view: orders {}\n'}
        self.fake.branches['feat'].update(self.fake.uncommitted)
        self.fake.uncommitted = {}
        url = self.fake.serve()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, 'proj')
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, '.env'), 'w') as f:
            f.write(f'LOOKER_URL={url}\nLOOKER_CLIENT_ID=id\nLOOKER_CLIENT_SECRET={SECRET}\n')

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


if __name__ == '__main__':
    unittest.main()
