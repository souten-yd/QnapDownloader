"""Explicit, single-flight QPKG updates that survive replacement of the Web app."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

import updater
from durability import write_json, snapshot

ACTIVE = {'queued', 'checking', 'downloading', 'backup', 'installing', 'verifying'}
GETCFG = '/sbin/getcfg'
QPKG_CONF = '/etc/config/qpkg.conf'
INSTALL_TIMEOUT = 600
VERIFY_TIMEOUT = 120


class UpdateConflict(ValueError):
    pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def process_identity(pid):
    """PID reuse and zombies must not leave an update permanently busy."""
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (OSError, ValueError, TypeError, IndexError):
        return None


def installer_alive(state):
    return bool(state.get('installer_start') and
                process_identity(state.get('installer_pid')) == state['installer_start'])


def getcfg(field):
    return subprocess.check_output([GETCFG, 'QnapDownloader', field, '-d', '', '-f', QPKG_CONF],
                                   text=True, timeout=5).strip()


def check_environment(install_root, current=None):
    if os.geteuid() != 0 or not Path(GETCFG).is_file() or not Path(QPKG_CONF).is_file():
        raise ValueError('自己更新はQNAPにインストールされたQPKGから実行してください')
    registered = getcfg('Install_Path')
    if not registered or Path(registered).resolve() != Path(install_root).resolve():
        raise ValueError('QPKG登録先と実行中のアプリが一致しません')
    if Path(registered).resolve().name != 'QnapDownloader':
        raise ValueError('標準のQnapDownloaderインストールディレクトリではありません')
    if not (Path(registered) / 'downloader.sh').is_file() or getcfg('Enable').upper() != 'TRUE':
        raise ValueError('有効なQnapDownloader QPKGを確認できません')
    if current and updater.version(getcfg('Version')) != updater.version(current):
        raise ValueError('QPKG登録バージョンと実行中のアプリが一致しません。App Centerの状態を確認してください')
    return registered


class UpdateManager:
    def __init__(self, data_root, install_root, current, arch, port=17864):
        self.data_root = Path(data_root).resolve()
        self.install_root = Path(install_root).resolve()
        self.current, self.arch, self.port = current, arch, port
        self.directory = self.data_root / 'updates'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_file = self.directory / 'status.json'

    def lock(self):
        fd = os.open(self.directory / 'update.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise UpdateConflict('更新処理が実行中です') from None
        return fd

    def status(self):
        try:
            fd = self.lock()
        except UpdateConflict:
            state = read_json(self.state_file)
            busy = True
        else:
            try:
                state = read_json(self.state_file)
                busy = installer_alive(state)
                if not busy and state.get('phase') in ACTIVE:
                    state.update(phase='failed', message='更新処理が中断されました。自動再実行はしません。バージョンと更新ログを確認してください', finished_at=time.time())
                    write_json(self.state_file, state)
            finally:
                os.close(fd)
        reason = None
        try:
            check_environment(self.install_root)
            if not self.arch:
                raise ValueError('未対応のCPU構成です')
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            reason = str(error)
        # Internal paths/PIDs remain private; the Web receives a fixed status contract.
        visible = {k: state[k] for k in ('id', 'phase', 'message', 'target', 'started_at', 'finished_at', 'backup') if k in state}
        return dict(visible, phase=state.get('phase', 'idle'), busy=busy, supported=reason is None,
                    unsupported_reason=reason, current_version=self.current)

    def log_tail(self):
        identifier = read_json(self.state_file).get('id', '')
        if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            return ''
        try:
            with (self.directory / identifier / 'update.log').open('rb') as log:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 16000))
                return log.read(16000).decode('utf-8', errors='replace')
        except FileNotFoundError:
            return ''

    def apply(self, expected):
        if not isinstance(expected, str):
            raise ValueError('更新対象のバージョンが必要です')
        updater.version(expected)
        if updater.version(expected) <= updater.version(self.current):
            raise ValueError('新しいバージョンだけに更新できます')
        check_environment(self.install_root, self.current)
        if not self.arch:
            raise ValueError('未対応のCPU構成です')
        fd = self.lock()
        try:
            if installer_alive(read_json(self.state_file)):
                raise UpdateConflict('QPKGインストーラーがまだ動いています。再実行できません')
            identifier = uuid.uuid4().hex
            job = self.directory / identifier
            job.mkdir(mode=0o700)
            # Snapshot executable code outside the installation being replaced.
            for name in ('self_update.py', 'updater.py', 'durability.py'):
                shutil.copyfile(self.install_root / name, job / name)
            request = dict(id=identifier, current=self.current, target=expected, arch=self.arch,
                           data_root=str(self.data_root), install_root=str(self.install_root), port=self.port)
            write_json(job / 'request.json', request)
            write_json(self.state_file, dict(id=identifier, phase='queued', target=expected,
                       message='更新を受け付けました', started_at=time.time()))
            try:
                with (job / 'update.log').open('ab') as log:
                    child = subprocess.Popen([sys.executable, str(job / 'self_update.py'), '--worker', str(job), '--lock-fd', str(fd)],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        cwd=job, start_new_session=True, close_fds=True, pass_fds=(fd,),
                        env=dict(os.environ, DOWNLOADER_DATA_DIR=str(self.data_root)))
                # The child owns the inherited flock until it exits; Web restarts cannot release it.
                import threading
                threading.Thread(target=child.wait, daemon=True).start()
            except Exception:
                state = read_json(self.state_file)
                state.update(phase='failed', message='更新ワーカーを起動できませんでした', finished_at=time.time())
                write_json(self.state_file, state)
                raise
            return {'id': identifier, 'phase': 'queued', 'busy': True, 'target': expected}
        finally:
            os.close(fd)


def backup_data(data_root, job):
    source = Path(data_root) / 'data/downloader.sqlite3'
    if not source.is_file():
        raise ValueError('測定DBが見つからないため更新を中止しました')
    backup = job / 'backup'
    backup.mkdir(mode=0o700)
    snapshot(source, backup / source.name)
    if (Path(data_root) / 'config').is_dir():
        shutil.copytree(Path(data_root) / 'config', backup / 'config')
    return str(backup)


def wait_for_version(target, port, timeout=VERIFY_TIMEOUT):
    deadline = time.monotonic() + timeout
    # Never use environment HTTP proxies for the NAS loopback health check.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            if updater.version(getcfg('Version')) == updater.version(target):
                with client.open(f'http://127.0.0.1:{port}/api/status', timeout=3) as response:
                    status = json.load(response)
                if updater.version(status.get('version', '')) == updater.version(target) and isinstance(status.get('settings_revision'), int):
                    return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(2)
    raise ValueError('QPKG適用後のWeb起動・バージョン確認がタイムアウトしました。更新ログを確認してください')


def run_job(job, lock_fd):
    job = Path(job).resolve()
    request = read_json(job / 'request.json')
    state_file = job.parent / 'status.json'
    state = read_json(state_file)
    if state.get('id') != request['id']:
        raise ValueError('更新ジョブが一致しません')
    # Validate that the inherited descriptor is the lock; descendants do not inherit it.
    if os.fstat(lock_fd).st_ino != os.stat(job.parent / 'update.lock').st_ino:
        raise ValueError('更新ロックが一致しません')
    os.set_inheritable(lock_fd, False)
    def phase(name, message, **extra):
        state.update(phase=name, message=message, **extra)
        write_json(state_file, state)
        print(message, flush=True)
    installer = None
    stopped = False
    try:
        check_environment(request['install_root'], request['current'])
        phase('checking', '更新対象を確認しています')
        asset = updater.latest(request['current'], request['arch'])
        if asset is None or updater.version(asset['version']) != updater.version(request['target']):
            raise ValueError('公開中の更新対象が変わりました。更新確認をやり直してください')
        for directory in (job, Path(request['install_root'])):
            if shutil.disk_usage(directory).free < asset['size'] * 4 + 64 * 1024 * 1024:
                raise ValueError('更新用の空き容量が不足しています')
        phase('downloading', 'QPKGを取得してSHA-256を検証しています')
        package = updater.stage(asset, job)
        phase('backup', '更新前の測定DBと設定をバックアップしています')
        backup = backup_data(request['data_root'], job)
        check_environment(request['install_root'], request['current'])
        phase('installing', 'QPKGを適用しています。Web接続が一時的に切れます', backup=backup)
        # QDK's generic stop hook does not propagate service-stop failures.
        # Stop explicitly first and abort before replacing files if it fails.
        stopped = True
        subprocess.run(['/bin/sh', str(Path(request['install_root']) / 'downloader.sh'), 'stop'],
                       stdin=subprocess.DEVNULL, timeout=25, check=True, close_fds=True)
        # Only a server-selected, digest-verified release is ever executable.
        installer = subprocess.Popen(['/bin/sh', package], stdin=subprocess.DEVNULL, cwd=job,
                                     close_fds=True, start_new_session=True,
                                     env=dict(os.environ, QINSTALL_PATH=str(Path(request['install_root']).parent),
                                              DOWNLOADER_DATA_DIR=request['data_root']))
        state.update(installer_pid=installer.pid, installer_start=process_identity(installer.pid))
        write_json(state_file, state)
        try:
            code = installer.wait(timeout=INSTALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Do not interrupt QDK while it is replacing files. The recorded process
            # identity keeps retries blocked until the installer really finishes.
            raise ValueError('QPKG適用が時間内に終了しませんでした。処理が終了するまで再実行できません。更新ログを確認してください') from None
        # QDK v2.5.3 qbuild/add_qpkg_header returns 10 to tell App Center it
        # may remove the package, even if its inner installer failed. Therefore
        # 0/10 are only candidates; registration AND HTTP health must agree.
        if code not in (0, 10):
            raise ValueError(f'QPKGインストーラーがエラーで終了しました（終了コード {code}）')
        phase('verifying', 'Webサービスの再起動と更新後バージョンを確認しています')
        wait_for_version(request['target'], request['port'])
        phase('success', f"{request['target']} への更新が完了しました", finished_at=time.time())
        try:
            Path(package).unlink(missing_ok=True)
        except OSError as cleanup:
            print(f'適用済みQPKGの削除を保留しました: {cleanup}', flush=True)
    except Exception as error:
        phase('failed', str(error) or type(error).__name__, finished_at=time.time())
        if stopped and (installer is None or installer.poll() is not None):
            try:
                if getcfg('Enable').upper() == 'TRUE':
                    subprocess.run(['/bin/sh', str(Path(request['install_root']) / 'downloader.sh'), 'start'],
                                   stdin=subprocess.DEVNULL, timeout=20, check=True, close_fds=True)
            except (OSError, subprocess.SubprocessError) as recovery:
                print(f'Web起動を回復できませんでした: {recovery}', flush=True)
        # No automatic reinstall or DB rollback: this could overwrite newer records.
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--lock-fd', type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        return run_job(args.worker, args.lock_fd)
    finally:
        os.close(args.lock_fd)


if __name__ == '__main__':
    sys.exit(main())
