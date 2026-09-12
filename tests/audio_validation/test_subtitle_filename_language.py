from api.subtitles.subtitles_info import _language_hint_from_filename


def test_subtitle_group_language_markers_are_detected():
    assert _language_hint_from_filename('show.[01].SC.ass') == 'zh'
    assert _language_hint_from_filename('show.S01E01.CHS.srt') == 'zh'
    assert _language_hint_from_filename('show.S01E01.CHT.ass') == 'zt'
    assert _language_hint_from_filename('show.S01E01.JPN.srt') == 'ja'
    assert _language_hint_from_filename('show.S01E01.ass') is None
