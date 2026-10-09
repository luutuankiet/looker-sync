#!/usr/bin/env python3
"""looker-sync: pull and push LookML between a local folder and a Looker dev workspace.

Talks to the Looker REST API (4.0) directly with the user's own API key. Standard library
only. Never commits, deploys, resets or creates branches; those stay clicks in the Looker IDE.

    looker_sync.py [-C PROJECT_DIR] [--env-file PATH] COMMAND ...

    init    --project P [--mode dev|prod] [--branch B]   write metadata, then a first pull
    pull    [--force] [--dry-run]                         copy Looker files to disk
    status                                                show what differs on each side
    push    [--force] [--delete] [--dry-run]              write local edits, read back, validate
    switch  (--branch B | --mode dev|prod) [--local-changes push|backup|cancel] [--yes]

Metadata lives in PROJECT_DIR/.looker-sync/ (config.json, state.json, backups/). Credentials
come only from the env file: LOOKER_URL, LOOKER_CLIENT_ID, LOOKER_CLIENT_SECRET.

Exit codes: 0 ok, 1 Looker or network error, 2 refused by a guard, 3 validation errors.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
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
        if flag:
            return flag
        if self.config and self.config.get('env_file'):
            return os.path.join(self.root, self.config['env_file'])
        return os.path.join(self.root, '.env')


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
            return None
        return b.get('name')

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
    cfg = proj.require_config()
    return Session(proj, args.env_file, cfg['mode'], cfg['project'], cfg.get('branch'), check_branch)


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


def cmd_init(proj, args):
    if proj.config:
        raise Refused(f'{proj.config_path} already exists; edit it or remove it first')
    mode = args.mode
    env_flag = args.env_file
    sess = Session(proj, env_flag, mode, args.project, args.branch, check_branch=False)
    branch = args.branch or sess.branch
    if mode == 'dev' and branch != sess.branch:
        raise Refused(f'Looker dev workspace is on {sess.branch!r}, not {branch!r}. init never '
                      'switches; check out the branch in the Looker IDE first, or omit --branch.')
    env_rel = os.path.relpath(os.path.abspath(env_flag), proj.root) if env_flag else '.env'
    proj.config = {'project': args.project, 'mode': mode, 'branch': branch if mode == 'dev' else None,
                   'env_file': env_rel, 'include': args.include or DEFAULT_INCLUDE,
                   'exclude': args.exclude or []}
    proj.save_config()
    out(f'wrote {os.path.relpath(proj.config_path, proj.root)}')
    return do_pull(proj, sess)


def cmd_pull(proj, args):
    return do_pull(proj, open_session(proj, args), args.force, args.dry_run)


def cmd_status(proj, args):
    sess = open_session(proj, args)
    kinds = plan(proj.local_files(), sess.remote_files(), proj.state.get('files', {}))
    show(kinds, 'status')
    if proj.state.get('synced_at'):
        out(f'last sync: {proj.state["synced_at"]} by {proj.state.get("user")}')
    return OK


def do_push(proj, sess, force=False, delete=False, dry_run=False):
    if sess.mode != 'dev':
        raise Refused('push is refused in prod mode; switch --mode dev first')
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
    return do_push(proj, open_session(proj, args), args.force, args.delete, args.dry_run)


def ask(question, choices):
    while True:
        ans = input(f'{question} ').strip().lower()
        for c in choices:
            if ans and c.startswith(ans):
                return c


def cmd_switch(proj, args):
    cfg = proj.require_config()
    interactive = sys.stdin.isatty()
    sess = open_session(proj, args, check_branch=False)
    target_mode = 'dev' if args.branch else args.mode
    if target_mode == 'dev' and sess.mode != 'dev':
        sess.set_mode('dev')
        sess.branch = sess.current_branch()
    target_branch = args.branch or (sess.branch if target_mode == 'dev' else None)
    if target_mode == cfg['mode'] and (target_mode == 'prod' or target_branch == cfg.get('branch') == sess.branch):
        out(f'already on {target_mode} {target_branch or ""}; nothing to switch')
        return OK
    if args.branch:
        names = [b.get('name') for b in sess.L.call('GET', f'/projects/{cfg["project"]}/git_branches') or []]
        if args.branch not in names:
            raise Refused(f'branch {args.branch!r} does not exist in Looker; switch never creates branches')

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
            do_push(proj, sess)
            remote_guard()
        if choice == 'backup':
            stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
            label = (cfg.get('branch') if cfg['mode'] == 'dev' else 'production') or 'unknown'
            dest = os.path.join(proj.meta, 'backups', f'{re.sub(r"[^A-Za-z0-9._-]", "_", label)}-{stamp}')
            for p, data in local.items():
                full = os.path.join(dest, *p.split('/'))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, 'wb') as f:
                    f.write(data)
            out(f'backed up {len(local)} local file(s) to {os.path.relpath(dest, proj.root)}')

    where = f'branch {target_branch}' if target_mode == 'dev' else 'production'
    if not args.yes:
        if not interactive:
            raise Refused(f'no terminal to confirm; pass --yes to switch to {where}')
        if ask(f'Switch to {where}? This moves your Looker IDE too. [y/N]', ['yes', 'no']) != 'yes':
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

    for p in local:
        os.remove(os.path.join(proj.root, *p.split('/')))
    remote = sess.remote_files()
    for p, data in remote.items():
        proj.write_local(p, data)
    cfg.update({'mode': target_mode, 'branch': target_branch})
    proj.save_config()
    proj.state = {'files': {}}
    record(proj, sess, {p: sha(d) for p, d in remote.items()})
    out(f'switched to {target_mode} ({where}); mirrored {len(remote)} file(s), removed {len(local)} old local file(s)')
    return OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog='looker-sync', description=__doc__.split('\n\n')[0])
    ap.add_argument('-C', '--project-dir', default='.', help='Looker project folder (default: cwd)')
    ap.add_argument('--env-file', help='env file with LOOKER_URL, LOOKER_CLIENT_ID, LOOKER_CLIENT_SECRET')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('init', help='write metadata and run a first pull')
    p.add_argument('--project', required=True)
    p.add_argument('--mode', choices=['dev', 'prod'], default='dev')
    p.add_argument('--branch', help="default: the branch Looker's dev workspace is on")
    p.add_argument('--include', nargs='+')
    p.add_argument('--exclude', nargs='+')
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
    p.add_argument('--local-changes', choices=['push', 'backup', 'cancel'],
                   help='what to do with unpushed local changes when there is no terminal')
    p.add_argument('--yes', action='store_true', help='confirm the switch without a terminal')
    args = ap.parse_args(argv)
    proj = Project(args.project_dir)
    try:
        return {'init': cmd_init, 'pull': cmd_pull, 'status': cmd_status,
                'push': cmd_push, 'switch': cmd_switch}[args.cmd](proj, args)
    except Refused as e:
        out(f'REFUSED: {e}')
        return REFUSED
    except LookerError as e:
        out(f'LOOKER ERROR: {e}')
        return LOOKER_ERROR


if __name__ == '__main__':
    sys.exit(main())
