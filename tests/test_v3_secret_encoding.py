"""真实凭据库写入格式与读取解码；使用隔离临时命名，不读取用户凭据。"""
from types import ModuleType, SimpleNamespace
from pathlib import Path
import sys
import pytest
from doubao_typeless.storage import secret_store as secrets


@pytest.mark.parametrize('platform', ['win32', 'linux', 'darwin'])
def test_cleanup_failure_is_reported_separately_and_retry_removes_file(tmp_path, monkeypatch, caplog, cred, platform):
    values = {}
    monkeypatch.setattr(secrets, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(secrets, '_native_keyring', lambda: SimpleNamespace(
        set_password=lambda service, key, value: values.__setitem__(key, value),
        get_password=lambda service, key: values.get(key, '')))
    path = tmp_path/'secrets'/'byok_api_key.txt'
    path.parent.mkdir();path.write_text('old-fixture-key')
    original = Path.unlink
    def fail_owned_file(self, *args, **kwargs):
        if self == path:raise PermissionError('simulated cleanup failure')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_owned_file)
    assert secrets.put_secret(tmp_path, 'byok_api_key', 'new-fixture-key') == 'os_cleanup_pending'
    assert secrets._target(tmp_path, 'byok_api_key') not in secrets._MEMORY
    assert secrets.get_secret(tmp_path, 'byok_api_key') == 'new-fixture-key'
    assert path.read_text() == 'old-fixture-key'
    assert secrets.CLEANUP_WARNING in caplog.text
    assert 'old-fixture-key' not in caplog.text and 'new-fixture-key' not in caplog.text
    monkeypatch.setattr(Path, 'unlink', original)
    assert secrets.put_secret(tmp_path, 'byok_api_key', 'new-fixture-key') == 'os'
    assert not path.exists()


def test_settings_report_pending_cleanup_without_serializing_keys(tmp_path, monkeypatch):
    import json
    from doubao_typeless.storage import settings_store as settings
    monkeypatch.setattr(settings, 'get_secret', lambda *args: '')
    monkeypatch.setattr(settings, 'put_secret', lambda directory, name, value: 'os_cleanup_pending' if value else 'cleared')
    result = settings.save_settings(tmp_path, {'jev_api_key': 'fixture-only-key'})
    assert result == [secrets.CLEANUP_WARNING]
    raw = settings.settings_path(tmp_path).read_text(encoding='utf-8')
    assert 'fixture-only-key' not in raw and json.loads(raw)['jev_api_key'] == ''


@pytest.fixture
def cred(tmp_path, monkeypatch):
    values={}
    module=ModuleType('win32cred')
    module.CRED_TYPE_GENERIC=1
    module.CRED_PERSIST_LOCAL_MACHINE=2
    def write(record, flags):
        assert record['Persist']==2
        values[record['TargetName']]=record['CredentialBlob'].encode('utf-16-le')
    def read(key, kind):
        return {'CredentialBlob':values[key]}
    module.CredWrite=write
    module.CredRead=read
    module.CredDelete=lambda key,kind:values.pop(key,None)
    monkeypatch.setitem(sys.modules,'win32cred',module)
    monkeypatch.setattr(secrets,'sys',SimpleNamespace(platform='win32'))
    monkeypatch.delenv('DT_V3_SECRET_FILE',raising=False)
    yield module,values
    secrets._MEMORY.pop(secrets._target(tmp_path,'byok_api_key'),None)


@pytest.mark.parametrize('value',['sk-test-123456789','Unicode测试-123'])
def test_windows_wchar_encoding_round_trip(tmp_path,cred,value):
    assert secrets.put_secret(tmp_path,'byok_api_key',value)=='os'
    assert secrets.get_secret(tmp_path,'byok_api_key')==value
    assert not (tmp_path/'secrets'/'byok_api_key.txt').exists()


def test_failed_new_write_does_not_resurrect_old_key(tmp_path,cred):
    module,_=cred
    secrets.put_secret(tmp_path,'byok_api_key','old-key')
    module.CredWrite=lambda *a:(_ for _ in ()).throw(OSError('unavailable'))
    assert secrets.put_secret(tmp_path,'byok_api_key','new-key')=='memory'
    assert secrets.get_secret(tmp_path,'byok_api_key')=='new-key'
    assert not (tmp_path/'secrets').exists()


def test_failed_delete_clears_this_session(tmp_path,cred):
    module,_=cred
    secrets.put_secret(tmp_path,'byok_api_key','old-key')
    module.CredDelete=lambda *a:(_ for _ in ()).throw(OSError('unavailable'))
    secrets.delete_secret(tmp_path,'byok_api_key')
    assert secrets.get_secret(tmp_path,'byok_api_key')==''


@pytest.mark.skipif(sys.platform!='win32',reason='Windows credential store')
def test_native_credential_store_round_trip(tmp_path,monkeypatch):
    pytest.importorskip('win32cred')
    monkeypatch.delenv('DT_V3_SECRET_FILE',raising=False)
    try:
        assert secrets.put_secret(tmp_path,'native-test','test-only-123')=='os'
        assert secrets.get_secret(tmp_path,'native-test')=='test-only-123'
    finally:
        secrets.delete_secret(tmp_path,'native-test')
