"""Back up persisted V3 state while holding its instance lock, before opening DBs."""
from __future__ import annotations
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid


def prepare_upgrade(data_dir: Path, *, version: str) -> Path | None:
    data_dir = Path(data_dir)
    marker = data_dir / 'workspace-version.json'
    previous = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    if not isinstance(previous, dict) or int(previous.get('schema', 1)) > 1:
        raise RuntimeError('这份数据由更新版本创建或版本记录损坏，请使用原版本打开；数据未修改。')
    if previous.get('version') == version:
        return None
    def linked(path):
        return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
    backup_root = data_dir / 'upgrade-backups'
    if linked(backup_root):
        raise RuntimeError('备份位置是外部链接，升级已停止；原数据未修改。')
    # Includes encrypted secrets, recovery and consumed send actions. Logs,
    # transient connection/lock/WAL files and re-downloadable update packages
    # are not independent persisted records.
    entries = [p for p in data_dir.iterdir()
               if p.name not in {'upgrade-backups', 'logs', 'instance.lock', 'pair.txt', 'updates'}
               and not p.name.endswith(('-wal', '-shm', '.tmp'))]
    for entry in entries:
        if linked(entry):
            raise RuntimeError('数据包含外部链接，升级已停止；原数据未修改。')
        if entry.is_dir():
            for parent, dirs, names in os.walk(entry, followlinks=False):
                if any(linked(Path(parent) / name) for name in dirs + names):
                    raise RuntimeError('数据包含外部链接，升级已停止；原数据未修改。')
    backup = None
    if entries:
        backup_root.mkdir(exist_ok=True)
        backup = backup_root / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        backup.mkdir()
        def copy_file(src, dst):
            source_path = Path(src)
            if source_path.suffix == '.sqlite':
                with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(dst)) as dest:
                    source.backup(dest)
            elif source_path.is_relative_to(data_dir / 'assets') and source_path.suffix == '.bin':
                try: os.link(src, dst)
                except OSError: shutil.copy2(src, dst)
            else:
                shutil.copy2(src, dst)
            return dst
        try:
            for entry in entries:
                target = backup / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, target, copy_function=copy_file)
                else:
                    copy_file(entry, target)
            (backup / 'backup-complete.json').write_text(json.dumps({
                'schema':1, 'from':previous.get('version', 'unversioned'), 'to':version,
                'entries':[p.name for p in entries]}, ensure_ascii=False), encoding='utf-8')
        except BaseException:
            # 只删除本次新建、尚未完成的备份目录；原数据与以往完整备份不动。
            shutil.rmtree(backup, ignore_errors=True)
            raise
    temp = marker.with_suffix('.tmp')
    temp.write_text(json.dumps({'schema':1, 'version':version, 'backup':backup.name if backup else None}), encoding='utf-8')
    temp.replace(marker)
    return backup
