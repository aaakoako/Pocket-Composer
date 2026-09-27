"""Regression boundaries for independent references and responsive product surfaces."""
import asyncio
import os
from pathlib import Path
import pytest
from doubao_typeless.services.input_check import make_request, parse_response, REFERENCE_DIMENSIONS
from tests.test_v3_input_check import response
from tests.test_v3_unified_ui import pair


def test_reference_uncertainty_does_not_change_emotion_or_enable_voice_note():
    request, spans = make_request('请继续处理。')
    body = response(request, choice='clean')
    for name in REFERENCE_DIMENSIONS:
        answer = body['answers']['reference_' + name]
        answer['confidence'] = .2
    result = parse_response(body, request, spans)
    assert len(result['references']) == 4
    assert all(row['state'] == 'unknown' for row in result['references'])
    assert result['tone'] == '急切' and not result['issues']
    assert not result['suspected_transcription']
    assert result['reference_scope'] == 'current_text'


@pytest.mark.parametrize('font_size', [13, 18, 24])
def test_hud_full_feedback_keeps_action_text_and_rows_separate(pair, font_size):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    from doubao_typeless.ui.theme import QSS
    a, window, _ = pair
    window.input_check_timer.stop()
    from PySide6.QtGui import QFontDatabase
    from doubao_typeless.ui.desktop import apply_ui_font
    if not QFontDatabase.families() and os.name == 'nt':
        QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR']) / 'Fonts' / 'msyh.ttc'))
    apply_ui_font()
    h = a.hud
    h.start()
    try:
        h._widget.setStyleSheet(QSS.replace('font-size:13px', f'font-size:{font_size}px'))
        h.show_receiving('这是一段较长的文字。' * 35)
        request, spans = make_request('测试')
        result = parse_response(response(request), request, spans)
        h.set_input_check({**result, 'tone':'彻底怒了', 'tone_kind':'furious', 'note':True})
        QTest.qWait(80)
        assert h._references.isVisible() and h._check_row.isVisible()
        assert not h._body.geometry().intersects(h._references.geometry())
        assert h._card.rect().contains(h._references.geometry())
        for container in (h._check_row, h._bar):
            buttons = [b for b in container.findChildren(QPushButton) if b.isVisible()]
            for button in buttons:
                assert button.width() >= button.sizeHint().width(), (font_size, button.text(), button.size())
                assert container.rect().contains(button.geometry()), (font_size, button.text(), container.size(), button.geometry())
            for i, button in enumerate(buttons):
                assert all(not button.geometry().intersects(other.geometry()) for other in buttons[i+1:])
        assert h._feedback is not None and h._tone_badge.label.text() == '彻底怒了'
        assert h._body.height() >= 40
        folder = os.environ.get('DT_REDESIGN_EVIDENCE')
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            h._widget.grab().save(str(Path(folder) / f'hud-{font_size}.png'))
    finally:
        h.dismiss();h._widget.deleteLater()


@pytest.mark.parametrize('width,height', [(320,360), (360,640), (430,932)])
def test_mobile_editor_tools_remain_scroll_reachable(tmp_path, width, height):
    from tests.test_v3_editor_transactions import product, editing
    async def run():
        async with product(tmp_path, touch=True) as (page, app, uploads):
            await page.set_viewport_size({'width':width, 'height':height})
            await page.click('#boardBtn');await editing(page)
            await page.click('#moreBtn')
            await page.click('#captionToggle')
            await page.locator('#captionInput').fill('低高度时仍能编辑图注')
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.locator('#captionInput').scroll_into_view_if_needed()
            box = await page.locator('#captionInput').bounding_box()
            assert box['y'] >= 0 and box['y'] + box['height'] <= height + 1
            await page.locator('#back').click(trial=True)
            await page.locator('#done').click(trial=True)
            folder = os.environ.get('DT_REDESIGN_EVIDENCE')
            if folder:
                await page.screenshot(path=str(Path(folder) / f'phone-editor-{width}-{height}.png'))
    asyncio.run(run())


def test_surface_animation_follows_resize_without_covering_old_content(pair):
    from PySide6.QtTest import QTest
    from doubao_typeless.ui.motion import SurfacePulse
    _, window, _ = pair
    target = window.widget
    target.show()
    pulse = SurfacePulse(target)
    target.resize(680, 700)
    QTest.qWait(10)
    assert pulse.geometry() == target.rect()
    pulse.finish()


def test_review_large_text_actions_stay_reachable_in_small_window(pair):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton, QScrollArea
    from doubao_typeless.ui.theme import QSS
    a, _, review = pair
    a.update_pc_text('保留原稿，分别观察参考维度。' * 50)
    review.widget.setStyleSheet(QSS.replace('font-size:13px', 'font-size:24px'))
    review.show();review.widget.resize(360, 320)
    QTest.qWait(30)
    assert review.widget.width() <= 360
    for label in ('返回浮窗','复制','插入并复制'):
        button = next(b for b in review.widget.findChildren(QPushButton) if b.text() == label)
        assert button.isVisible() and review.widget.rect().contains(button.geometry())
        assert button.width() >= button.sizeHint().width()
    scroll = review.widget.findChild(QScrollArea)
    assert scroll.verticalScrollBar().maximum() > 0


def test_mobile_composer_and_settings_at_large_text(tmp_path):
    from tests.test_v3_editor_transactions import product
    async def run():
        async with product(tmp_path, touch=True) as (page, app, uploads):
            await page.set_viewport_size({'width':320,'height':640})
            await page.add_style_tag(content='html,body,#app{font-size:22px}button{font-size:22px}')
            await page.fill('#text','这一段需要保留完整，按钮仍能操作。')
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.locator('#clearDraft').click(trial=True)
            await page.locator('#sendBtn').click(trial=True)
            await page.locator('#settingsBtn').click()
            await page.locator('#closeSheet').click(trial=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.locator('#closeSheet').click()
            folder = os.environ.get('DT_REDESIGN_EVIDENCE')
            if folder:
                await page.set_viewport_size({'width':390,'height':844})
                await page.reload()
                await page.locator('#text').fill('图文一起表达，电脑接着做。')
                await page.screenshot(path=str(Path(folder) / 'phone-composer.png'), full_page=True)
    asyncio.run(run())



def test_reference_chart_unknown_has_no_score_and_animation_settles(pair):
    from PySide6.QtTest import QTest
    from doubao_typeless.ui.reference_chart import ReferenceChart
    chart = ReferenceChart()
    try:
        chart.set_result({'status':'ready','references':[{'id':'goal','state':'unknown'}]})
        chart.show();QTest.qWait(30)
        assert '目标：暂无法判断' in chart.accessibleDescription()
        assert chart.states['goal'] == 'unknown'
        assert chart.timer.isActive()
        started = chart.started
        chart.set_result({'status':'ready','references':[{'id':'goal','state':'unknown'}]})
        assert chart.started == started, 'unchanged results must not restart animation'
        QTest.qWait(650)
        assert not chart.timer.isActive() and chart.phase == 1.
        chart.set_result({'status':'ready','references':[{'id':'goal','state':'clear'}]})
        chart.set_motion(False)
        assert not chart.timer.isActive() and chart.phase == 1.
        chart.hide()
        assert not chart.timer.isActive()
    finally:chart.deleteLater()


def test_sync_graph_never_marks_failed_image_as_received(tmp_path):
    from tests.test_v3_editor_transactions import product, synced, photo, editing
    async def run():
        async with product(tmp_path, touch=True) as (page, app, uploads):
            await page.fill('#text','文字已同步，图片传输独立判断')
            await synced(page)
            assert await page.locator('#syncMap').get_attribute('data-state') == 'synced'
            assert not await page.locator('#connectBtn').is_visible()
            await page.route('**/v3/assets/init', lambda route:route.fulfill(status=503,body='unavailable'))
            await page.locator('#file').set_input_files(photo())
            await editing(page); await page.click('#done')
            await page.wait_for_function("document.querySelector('#syncMap').dataset.state === 'asset_failed'")
            assert '图片待重试' in await page.locator('#transferStatus').inner_text()
            assert await page.locator('.attach-card[data-state="ready"]').count() == 0
            assert await page.locator('.asset-progress').count() == 1
            assert await page.locator('#text').input_value() == '文字已同步，图片传输独立判断'
    asyncio.run(run())


def test_optional_jev_has_no_space_when_disabled_and_chart_opens_on_request(pair, monkeypatch):
    from PySide6.QtTest import QTest
    from tests.test_v3_input_check import OPTIONS
    a, _, review = pair
    a.hud.start()
    try:
        a.update_pc_text('输入始终是主角')
        a.hud.show_receiving(a.review_text())
        a.hud.set_input_check({'status':'disabled'})
        QTest.qWait(20)
        assert a.hud._check_row.isHidden() and a.hud._references.isHidden()
        assert a.hud._insert.isEnabled()
        a.input_check.configure(OPTIONS)
        a.input_check.observe(a.input_check_identity(),a.review_text())
        request, spans = make_request(a.review_text())
        result = parse_response(response(request),request,spans)
        a.input_check.result = result
        a.hud.set_input_check(result)
        QTest.qWait(50)
        assert a.hud._references.x() >= a.hud._body.geometry().right()
        assert a.hud._body.width() > a.hud._references.width() * 1.8
        assert a.hud._references.height() >= 100
        a.hud.set_input_check({'status':'checking'})
        assert not a.hud._references.isHidden()
        assert set(a.hud._references.states.values()) == {'unknown'}
        a.hud.set_input_check({'status':'disabled'})
        QTest.qWait(30)
        assert a.hud._references.isHidden()
        assert a.hud._body.width() > 300
        a.hud.set_input_check(result)
        events = []
        monkeypatch.setattr(a, '_notify_ui', lambda event, **payload: events.append(event))
        a.hud._check_button.click()
        assert events[-1] == 'expand_reference'
        a.hud._expand.click()
        assert events[-1] == 'expand'
        review.show();QTest.qWait(300)
        assert review.input_check_details.references.isHidden()
        review.input_check_details.expand.click();QTest.qWait(300)
        assert not review.input_check_details.references.isHidden()
        review.input_check_details.expand.click();QTest.qWait(300)
        assert review.input_check_details.references.isHidden()
        a.input_check.configure({**OPTIONS,'jev_enabled':False})
        QTest.qWait(300)
        assert review.input_check_details.isHidden()
    finally:a.hud.dismiss();a.hud._widget.deleteLater()
