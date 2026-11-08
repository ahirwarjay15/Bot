import inspect

from app.handlers.settings import format_settings_page, render_home


def test_settings_home_description_is_italic_in_navigation_renderer():
    source = inspect.getsource(render_home)
    assert "<i>Configure Bot Settings Using the Buttons Below.</i>" in source


def test_settings_home_description_remains_italic_after_page_normalization():
    page = format_settings_page(
        "CUSTOMIZE\n<i>✰ Name: Bot</i>\n\n"
        "<i>Configure Bot Settings Using the Buttons Below.</i>"
    )
    assert "<i>Configure Bot Settings Using the Buttons Below.</i>" in page
