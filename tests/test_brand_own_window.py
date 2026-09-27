from doubao_typeless.core.policy import is_own_window


def test_renamed_native_window_is_not_an_external_input_target():
    assert is_own_window('QtQWindowIcon', 'Pocket Composer')
    assert is_own_window('QtQWindowIcon', 'Pocket Composer 0.5.5')
    assert not is_own_window('Chrome_WidgetWin_1', 'Pocket Composer — Cursor')
