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
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

META_DIR = '.looker-sync'
DEFAULT_INCLUDE = ['**/*.lkml', '**/*.dashboard.lookml']
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


class Looker:
    """One login per command run. The token lives in memory only."""

    def __init__(self, env):
        url = env['LOOKER_URL'].rstrip('/')
        self.base = url if url.endswith('/api/4.0') else url + '/api/4.0'
        body = urllib.parse.urlencode({'client_id': env['LOOKER_CLIENT_ID'],
                                       'client_secret': env['LOOKER_CLIENT_SECRET']}).encode()
        status, payload = self._send('POST', '/login', body, {'Content-Type': 'application/x-www-form-urlencoded'})
        if status != 200:
            # The request carried the secret; the response never does, but keep it terse anyway.
            raise LookerError(f'login failed: HTTP {status}')
        self.token = json.loads(payload)['access_token']

    def _send(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            raise LookerError(f'{method} {path}: {e}')

    def try_call(self, method, path, query=None, body=None, raw=False):
        if query:
            path += '?' + urllib.parse.urlencode(query)
        headers = {'Authorization': 'token ' + self.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        status, payload = self._send(method, path, data, headers)
        if status >= 400:
            try:
                msg = json.loads(payload).get('message') or payload.decode(errors='replace')
            except (ValueError, AttributeError):
                msg = payload.decode(errors='replace')
            return status, msg[:300]
        if raw:
            return status, payload
        return status, json.loads(payload) if payload else None

    def call(self, method, path, query=None, body=None, raw=False):
        status, payload = self.try_call(method, path, query, body, raw)
        if status >= 400:
            raise LookerError(f'{method} {path.split("?")[0]} -> HTTP {status}: {payload}')
        return payload


# ---------------------------------------------------------------- project folder

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
        """True if the path is in the sync set. The metadata folder never is."""
        if path.split('/')[0] == META_DIR:
            return False
        cfg = self.config or {}
        inc = [glob_regex(p) for p in cfg.get('include', DEFAULT_INCLUDE)]
        exc = [glob_regex(p) for p in cfg.get('exclude', [])]
        return any(r.match(path) for r in inc) and not any(r.match(path) for r in exc)

    def local_files(self):
        found = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root)
            if rel_dir == '.':
                dirnames[:] = [d for d in dirnames if d != META_DIR]
            for name in filenames:
                rel = name if rel_dir == '.' else f'{rel_dir}/{name}'.replace(os.sep, '/')
                if self.synced(rel):
                    with open(os.path.join(dirpath, name), 'rb') as f:
                        found[rel] = f.read()
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

GIT_READS = {'rev-parse', 'symbolic-ref', 'status', 'cat-file', 'merge-base', 'remote'}
GIT_ALLOWED = GIT_READS | {'fetch', 'stash', 'checkout', 'merge'}


def git_in_scope(args):
    cmd = args[0] if args else None
    if cmd not in GIT_ALLOWED:
        return False
    if cmd == 'remote':
        return args[1:2] == ['get-url']
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


def commit_check(proj, sess, remote):
    """Prove the clone and the Looker project are one repository: Looker's commit is local.

    Fetch first (a failure only warns). Then Looker's `ref`, else `remote_ref`, must exist in
    the clone. If neither does and Looker reports unpushed commits, warn; otherwise refuse,
    printing both remote URLs. No URL is ever compared. Returns the local remote's URL.
    """
    g = LocalGit(proj.root)
    if not g.is_work_tree():
        raise Refused(f'{proj.root} is not a git work tree, so it cannot be linked to remote {remote!r}')
    local_url = g.remote_url(remote)
    if local_url is None:
        raise Refused(f'no git remote named {remote!r} in {proj.root}; '
                      'list them with `git remote -v`, then: looker-sync link --remote NAME')
    err = g.fetch(remote)
    if err:
        out(f'warning: git fetch {remote} failed ({err}); checking against what was fetched before')
    info = getattr(sess, 'branch_info', None) or {}
    name, ref, remote_ref = info.get('name') or sess.branch, info.get('ref'), info.get('remote_ref')
    if not (ref or remote_ref):
        out(f'warning: Looker reported no commit for branch {name!r}; the clone was not checked')
        return local_url
    if g.has_commit(ref) or g.has_commit(remote_ref):
        return local_url
    shown = (ref or remote_ref)[:12]
    ahead = info.get('ahead_count') or 0
    if ahead > 0:
        out(f"warning: Looker's branch {name!r} is on {shown}, which this clone does not have; it has "
            f'{ahead} commit(s) Looker has not pushed to the remote yet. Push them from the Looker IDE.')
        return local_url
    looker_url = (sess.project_info() or {}).get('git_remote_url')
    raise Refused(f"this clone does not have commit {shown}, which Looker's branch {name!r} is on, even "
                  f'after fetching {remote!r}. It looks like a clone of a different repository.\n'
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
        want = WORKSPACE[mode]
        self.L.call('PATCH', '/session', body={'workspace_id': want})
        got = (self.L.call('GET', '/session') or {}).get('workspace_id')
        if got != want:
            raise LookerError(f'session workspace is {got!r} after asking for {want!r}')
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
        files = {}
        for f in self.file_list():
            if self.proj.synced(f['path']):
                files[f['path']] = self.read(f['path'])
        return files

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

    def validate(self):
        res = self.L.call('POST', f'/projects/{self.project}/validate') or {}
        errors = 0
        for e in res.get('errors') or []:
            sev = e.get('severity') or 'error'
            where = e.get('source_file') or e.get('file_path') or f'(no file; model {e.get("model_id") or "?"})'
            if e.get('line_number') or e.get('line'):
                where += f':{e.get("line_number") or e.get("line")}'
            out(f'  {sev:7} {where}  {e.get("message")}')
            if sev in ('error', 'fatal'):
                errors += 1
        out(f'validation: {errors} error(s)' if errors else 'validation: no errors')
        return errors

    def uncommitted(self):
        return [(f['path'], f['git_status'].get('text') or f['git_status'].get('action'))
                for f in self.file_list() if f.get('git_status')]


def open_session(proj, args, check_branch=True):
    """The Looker session every command starts with; in a linked folder, the commit check too."""
    cfg = proj.require_config()
    sess = Session(proj, args.env_file, cfg['mode'], cfg['project'], cfg.get('branch'), check_branch)
    if cfg.get('git_remote'):
        commit_check(proj, sess, cfg['git_remote'])
    return sess


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
    kinds = {}
    for path in sorted(set(local) | set(remote) | set(known)):
        lh, rh, s = sha(local.get(path)), sha(remote.get(path)), known.get(path)
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


# ---------------------------------------------------------------- commands

def do_pull(proj, sess, force=False, dry_run=False):
    local, remote = proj.local_files(), sess.remote_files()
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
                       'env_file': env_rel, 'include': args.include or DEFAULT_INCLUDE,
                       'exclude': args.exclude or []}
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


def do_push(proj, sess, force=False, delete=False, dry_run=False):
    if sess.mode != 'dev':
        raise Refused('push is refused in prod mode; switch --mode dev first')
    # Not verified live with two users: the flag is what the branch resource reports.
    if (getattr(sess, 'branch_info', None) or {}).get('readonly'):
        raise Refused(f'Looker reports branch {sess.branch!r} as read-only for you, as a colleague\'s '
                      'personal branch is; review it, then switch to a branch of your own to push')
    local, remote = proj.local_files(), sess.remote_files()
    kinds = plan(local, remote, proj.state.get('files', {}))
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
    writes.sort(key=lambda p: p in remote)
    done, verified, bad = [], {}, []
    try:
        for p in writes:
            sess.write(p, local[p], exists=p in remote)
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
    updates = in_sync(local, remote)
    updates.update(verified)
    record(proj, sess, updates, drops=deletes)
    out(f'read-back: {len(verified)}/{len(writes)} byte-equal')
    for p in bad:
        out(f'  MISMATCH {p}: Looker holds different bytes than were sent')
    errors = sess.validate() if (writes or deletes) else 0
    if bad:
        return LOOKER_ERROR
    return INVALID if errors else OK


def cmd_push(proj, args):
    sess = open_session(proj, args)
    drift_guard(proj, sess, 'push', refuse=True)
    return do_push(proj, sess, args.force, args.delete, args.dry_run)


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
                'push': cmd_push, 'switch': cmd_switch}[args.cmd](proj, args)
    except Refused as e:
        out(f'REFUSED: {e}')
        return REFUSED
    except LookerError as e:
        out(f'LOOKER ERROR: {e}')
        return LOOKER_ERROR


if __name__ == '__main__':
    sys.exit(main())
