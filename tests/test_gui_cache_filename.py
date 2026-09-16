from agentbridge.gui.api_files import cache_filename


def test_open_cache_keeps_readable_prefix_and_extension():
    one = cache_filename('Project notes.pdf', 'f-123.pdf')
    assert one.startswith('Project notes_') and one.endswith('.pdf')
    assert one == cache_filename('Project notes.pdf', 'f-123.pdf')
    assert one != cache_filename('Project notes.pdf', 'f-456.pdf')
    assert '/' not in cache_filename('../../notes.pdf', 'same')
    assert '\\' not in cache_filename(r'..\notes.pdf', 'same')
    assert len(cache_filename('文' * 119 + '.pdf', 'same').encode()) < 255
