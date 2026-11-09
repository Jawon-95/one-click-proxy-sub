"""Apply new GitHub main commits on each installed server."""
import argparse
import os
from pathlib import Path
import re
import tempfile

import provision


STATE = provision.ETC / 'auto-update.json'
DEFAULT_REPO = 'Jawon-95/one-click-proxy-sub'
TIMER = 'proxy-auto-update.timer'


def validate_repo(repo):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repo):
        raise ValueError('GitHub 仓库必须使用 owner/repo 格式。')
    return repo


def configure(repo, revision):
    previous = provision.read(STATE, {})
    repo = validate_repo(repo or previous.get('repo') or DEFAULT_REPO)
    if revision and not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('无法记录无效的 GitHub 提交。')
    provision.unit('proxy-auto-update.service', '''[Unit]
Description=Deploy new ProxySub commits from GitHub
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/proxy-web/bin/python /opt/proxy-manager/deploy/auto_update.py run
TimeoutStartSec=30min
Environment=GIT_TERMINAL_PROMPT=0
UMask=0027
''')
    provision.unit(TIMER, '''[Unit]
Description=Check GitHub for ProxySub updates

[Timer]
OnActiveSec=1min
OnUnitInactiveSec=1min
AccuracySec=5s
RandomizedDelaySec=15s

[Install]
WantedBy=timers.target
''')
    provision.run('systemctl', 'daemon-reload')
    if not previous:
        provision.run('systemctl', 'enable', '--now', TIMER)
    provision.write(STATE, {'repo': repo, 'revision': revision or (
        previous.get('revision') if previous.get('repo') == repo else None)}, 0o600)


def update():
    state = provision.read(STATE)
    repo = validate_repo(state['repo'])
    remote = provision.run('git', 'ls-remote', '--exit-code', f'https://github.com/{repo}.git',
                           'refs/heads/main', capture_output=True, text=True, timeout=45)
    revision = remote.stdout.split('\t', 1)[0]
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('GitHub 未返回有效的 main 提交。')
    if revision == state.get('revision'):
        return
    print(f'部署 {repo}@{revision}', flush=True)
    with tempfile.TemporaryDirectory(prefix='proxysub-update-') as directory:
        root = Path(directory)
        archive = root / 'source.tar.gz'
        provision.run('curl', '-fLsS', '--retry', '3', '--connect-timeout', '20', '--max-time', '300',
                      f'https://codeload.github.com/{repo}/tar.gz/{revision}', '-o', str(archive))
        provision.run('tar', '-xzf', str(archive), '--strip-components=1', '-C', directory)
        env = dict(os.environ, PROXYSUB_REPO=repo, PROXYSUB_REF='main', PROXYSUB_DEPLOY_REVISION=revision)
        provision.run('bash', str(root / 'install.sh'), '--upgrade', env=env)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('configure', 'run'))
    parser.add_argument('--repo')
    parser.add_argument('--revision')
    args = parser.parse_args()
    if args.action == 'configure':
        configure(args.repo, args.revision)
    else:
        update()
