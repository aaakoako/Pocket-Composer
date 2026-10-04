import pytest
from doubao_typeless.services.v3_update import check_preview_update


@pytest.mark.parametrize('channel,current,latest,available',[
    ('release-candidate','0.5.2','0.5.0',True),
    ('release-candidate','0.5.0','0.5.0',True),
    ('release-candidate','0.6.0','0.5.0',False),
    ('stable','0.5.2','0.5.0',False),
    ('stable','0.5.0','0.5.0',False),
    ('stable','0.4.2','0.5.0',True),
])
def test_stable_transition_is_explicit_and_does_not_downgrade_stable(monkeypatch,channel,current,latest,available):
    monkeypatch.setattr('doubao_typeless.build_info.build_info',lambda:{'channel':channel,'version':current,'source_sha':'a'*40})
    result=check_preview_update(get_json=lambda _: {'tag_name':'v'+latest})
    assert result['update_available'] is available
    assert result['automatic_update_available'] is (available and tuple(map(int, latest.split('.'))) >= tuple(map(int, current.split('.'))))
    assert result['auto_replace'] is False
