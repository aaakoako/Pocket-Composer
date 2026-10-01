"""Release discovery and explicit, verified full-package upgrades."""
from __future__ import annotations

from typing import Any, Callable
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

try:
    from app_version import APP_VERSION, GITHUB_REPO_NAME, GITHUB_REPO_OWNER
except ImportError:
    APP_VERSION = "0.4.2"
    GITHUB_REPO_OWNER = "aaakoako"
    GITHUB_REPO_NAME = "Pocket-Composer"

CHANNEL = "v3-preview"
DOWNLOAD_PAGE = f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases"
API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases/latest"


def preview_version_label() -> str:
    from doubao_typeless.build_info import build_info
    info = build_info()
    label = {"v3-private-trial":"隔离体验版", "release-candidate":"发布候选", "stable":"正式版"}[info["channel"]]
    sha = str(info.get("source_sha") or "")
    # 只有 40 位提交号才截短；源码运行的 "development" 原样显示为开发构建，不截成半个词。
    source = sha[:8] if len(sha) == 40 else "开发构建"
    return f"Pocket Composer {info['version']} · {label} · {source}"


def check_preview_update(*, get_json: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    from doubao_typeless.build_info import build_info
    info = build_info()
    release = info['channel'] in {'stable', 'release-candidate'}
    current = info['version'] if release else APP_VERSION
    out: dict[str, Any] = {
        "channel": info['channel'] if release else CHANNEL,
        "current": current,
        "label": preview_version_label(),
        "page": DOWNLOAD_PAGE,
        "latest": "",
        "auto_replace": False,
        "writes_daily_use": False,
        "message": "这是隔离预览。只打开公开下载页，不会覆盖日用安装或本预览目录。",
        "update_available": False,
    }
    if release:
        out['message'] = '可查询正式版本；下载后运行安装程序升级，设置和草稿会保留。不会自动替换。'
    if get_json is None:
        return out
    try:
        body = get_json(API_LATEST) or {}
        if not isinstance(body, dict):
            raise ValueError('release response must be an object')
    except Exception:
        out["message"] = "无法查询 GitHub，仍可打开公开下载页。不会自动替换。"
        return out
    tag = str(body.get("tag_name") or "").lstrip("v")
    out["latest"] = tag
    if release:
        import re
        def numeric(value):
            return tuple(map(int, value.split('.'))) if re.fullmatch(r'\d+\.\d+\.\d+', value) else None
        latest, installed = numeric(tag), numeric(current)
        if latest and installed and not body.get('prerelease') and not body.get('draft'):
            switch_to_stable=info['channel']=='release-candidate' and latest[:2]==installed[:2]
            out['update_available'] = latest > installed or switch_to_stable
            out['switch_to_stable']=switch_to_stable
            out['message'] = (f'发现正式版 {tag}，可打开下载页获取安装程序；不会自动替换。' if out['update_available']
                              else f'当前 {current} 已是最新可用版本。')
            if out['update_available']:
                out['package'] = release_package(body) if sys.platform == 'win32' else None
                if out['package']:
                    out['message'] = (f'可从候选版切换至正式版 {tag}。' if switch_to_stable else f'发现正式版 {tag}。')+'下载后退出并重启，保留当前工作区。'
        else:
            out['message'] = '没有查到可比较的正式版本；可打开下载页，当前安装不变。'
        return out
    if tag and tag != APP_VERSION:
        out["message"] = f"公开仓库最新标记 {tag}。预览不会自动下载或覆盖日用。"
    elif tag:
        out["message"] = "公开标记与当前日用版本号相同。预览仍不会自动替换。"
    return out


def release_package(release: dict) -> dict | None:
    tag = str(release.get('tag_name', ''))
    if not re.fullmatch(r'v?\d+\.\d+\.\d+', tag) or release.get('draft') or release.get('prerelease'):
        return None
    base = f'https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases/download/{tag}/'
    raw_assets = release.get('assets') or []
    if not isinstance(raw_assets,list):return None
    assets = [asset for asset in raw_assets if isinstance(asset,dict)]
    package = next((a for a in assets if a.get('name') == 'DoubaoTypeless.exe'
                    and a.get('browser_download_url') == base+'DoubaoTypeless.exe'), None)
    sums = next((a for a in assets if a.get('name') == 'SHA256SUMS.txt'
                 and a.get('browser_download_url') == base+'SHA256SUMS.txt'), None)
    if not package or not isinstance(package.get('size'), int) or not 64 <= package['size'] <= 512*1024*1024:
        return None
    digest = str(package.get('digest') or '')
    sha = digest[7:] if re.fullmatch(r'sha256:[0-9a-f]{64}', digest) else ''
    if not sha and not sums:
        return None
    return {'version':tag.lstrip('v'), 'url':base+'DoubaoTypeless.exe', 'size':package['size'],
            'sha256':sha, 'checksums_url':base+'SHA256SUMS.txt' if sums else ''}


STAGING_KEEP_S = 600


def prune_update_staging(data_dir: Path, *, keep_recent_s: float = STAGING_KEEP_S, now: float | None = None) -> int:
    """删除可重新下载的旧升级暂存目录。最近的、仍被占用的、链接或非本程序命名的都跳过。"""
    root = Path(data_dir) / 'updates'
    if not root.is_dir() or root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        return 0
    now = time.time() if now is None else now
    removed = 0
    for entry in root.iterdir():
        try:
            if not re.fullmatch(r'[0-9a-f]{32}', entry.name) or entry.is_symlink() or \
                    (hasattr(entry, 'is_junction') and entry.is_junction()) or not entry.is_dir():
                continue
            if now - entry.stat().st_mtime < keep_recent_s:
                continue
            for child in entry.iterdir():
                # 升级程序仍在运行时其文件被占用，删除失败即保留整个目录。
                child.unlink()
            entry.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def download_upgrade(package: dict, data_dir: Path, *, progress=None, cancelled=None, client=None) -> Path:
    """Only a complete hash-verified native package can leave the staging directory."""
    import httpx
    from contextlib import nullcontext
    version = str(package.get('version', ''))
    prefix = f'https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases/download/'
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or package.get('url') not in {
        prefix+version+'/DoubaoTypeless.exe', prefix+'v'+version+'/DoubaoTypeless.exe'}:
        raise ValueError('更新地址无效，请重新检查更新')
    prune_update_staging(data_dir)
    stage = Path(data_dir)/'updates'/uuid.uuid4().hex
    stage.mkdir(parents=True)
    part = stage/'download.part'
    target = stage/'DoubaoTypeless.exe'
    try:
        with (nullcontext(client) if client else httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, connect=15))) as http:
            expected = package.get('sha256') or ''
            if not expected:
                sums_url = str(package.get('checksums_url') or '')
                if sums_url != str(package['url']).rsplit('/',1)[0]+'/SHA256SUMS.txt':
                    raise ValueError('缺少更新校验信息')
                with http.stream('GET', sums_url) as response:
                    response.raise_for_status(); raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw)>32768: raise ValueError('更新校验信息过大')
                hashes = re.findall(r'^([0-9a-f]{64})  DoubaoTypeless\.exe\s*$',raw.decode('utf-8'),re.M)
                if len(hashes)!=1:raise ValueError('没有找到完整升级包的校验值')
                expected = hashes[0]
            if not re.fullmatch(r'[0-9a-f]{64}', expected):raise ValueError('更新校验值无效')
            total = int(package['size']); done = 0; digest = hashlib.sha256()
            with http.stream('GET', package['url']) as response:
                response.raise_for_status()
                with part.open('xb') as output:
                    for chunk in response.iter_bytes(128*1024):
                        if cancelled and cancelled():raise RuntimeError('下载已取消，当前版本未改变')
                        done += len(chunk)
                        if done>total:raise ValueError('下载大小与发布信息不一致')
                        output.write(chunk);digest.update(chunk)
                        if progress:progress(done,total)
                    output.flush();os.fsync(output.fileno())
            if done!=total or digest.hexdigest()!=expected:
                raise ValueError('下载不完整或校验失败，当前版本未改变，请重试')
            with part.open('rb') as executable:
                if executable.read(2)!=b'MZ':raise ValueError('下载内容不是 Windows 升级程序')
            if cancelled and cancelled():raise RuntimeError('下载已取消，当前版本未改变')
            part.replace(target)
        return target
    except BaseException:
        part.unlink(missing_ok=True)
        if not target.exists(): stage.rmdir()
        raise


def start_upgrade(package: Path, data_dir: Path, *, pipe: str) -> Path:
    """Handoff must be acknowledged before asking the current application to quit."""
    if sys.platform!='win32' or not getattr(sys,'frozen',False):
        raise RuntimeError('请在 Windows 打包版本中执行更新')
    package = package.resolve(strict=True)
    handoff = package.parent/('handoff-'+uuid.uuid4().hex+'.ready')
    env = {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT':'1', 'DT_V3_DATA_DIR':str(data_dir.resolve()),
           'DT_V3_PIPE':pipe, 'DT_UPGRADE_HANDOFF':str(handoff),
           'DT_UPGRADE_PREVIOUS':str(Path(sys.executable).resolve())}
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    child = subprocess.Popen([str(package),'/UPDATE',f'/WAITPID={os.getpid()}'],env=env,
                             close_fds=True,creationflags=flags)
    deadline = time.monotonic()+15
    while time.monotonic()<deadline:
        if handoff.is_file() and handoff.read_text(encoding='ascii').strip()==str(os.getpid()):
            # The Qt callback must authorize this specific attempt before quit.
            # If it never runs, the helper expires without modifying anything.
            return handoff.with_suffix('.accept')
        if child.poll() is not None:break
        time.sleep(.1)
    handoff.with_suffix('.cancel').write_text('cancel',encoding='ascii')
    raise RuntimeError('升级程序未就绪，本次升级已撤销，当前版本继续运行；请稍后重试')


def acknowledge_update_launch() -> None:
    """Called only after HTTP, native control and the Qt event loop are ready."""
    raw = os.environ.pop('DT_UPDATE_READY_FILE','')
    if not raw:return
    from doubao_typeless.build_info import build_info
    path = Path(raw)
    # Receipts are installer-owned unique files, never arbitrary user documents.
    if not re.fullmatch(r'\.update-ready-[0-9a-f]{32}',path.name):return
    temporary = path.with_suffix('.tmp')
    temporary.write_text(build_info()['source_sha'],encoding='ascii')
    temporary.replace(path)
