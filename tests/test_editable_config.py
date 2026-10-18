from pathlib import Path

import pytest

from app.config import load_config
from app.errors import ConfigError


def test_editable_config_defaults_unknown_and_no_write(tmp_path):
    path = tmp_path / 'config.toml'
    content = '[project]\ntarget_language = "English"\nunknown = 3\n'
    path.write_text(content)
    warnings = []
    config = load_config(path, warnings=warnings)
    assert config['project']['target_language'] == 'English'
    assert config['retry']['http_max_attempts'] == 6
    assert 'unknown' not in config['project']
    assert warnings and 'unknown' in warnings[0]
    assert path.read_text() == content


def test_editable_config_invalid_present_value_fails(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[retry]\nhttp_max_attempts = false\n')
    with pytest.raises(ConfigError, match='http_max_attempts'):
        load_config(path)
