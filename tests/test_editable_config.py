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


def test_sparse_stage_budget_roundtrip(tmp_path):
    from app.config import dump_config
    from app.request_budget import resolve_request_budget, request_input_limit
    path = tmp_path / 'config.toml'
    path.write_text('[chunking.stage_overrides.translation]\ntarget_content_tokens = 17\n')
    config = load_config(path)
    assert config['chunking']['stage_overrides'] == {'translation': {'target_content_tokens': 17}}
    budget = resolve_request_budget(config, 'translation')
    assert (budget.target_content_tokens, budget.max_input_tokens, budget.context_reserve_tokens) == (17, 65536, 16384)
    config['llm']['context_window_tokens'] = 100000
    config['execution']['input_tokens_per_minute'] = 8000
    assert request_input_limit(config, budget) == 8000
    config['llm'].pop('context_window_tokens')
    config['execution'].pop('input_tokens_per_minute')
    path.write_text(dump_config(config))
    assert load_config(path) == config


def test_editable_preset_incomplete_and_snapshot_strict(tmp_path):
    import json
    from app.llm_preset import load_llm_preset
    path = tmp_path / 'draft.json'
    path.write_text(json.dumps({'schema_version': 8, 'target_chunk_input_tokens': 9}))
    warnings = []
    preset = load_llm_preset(path, warnings=warnings)
    assert preset.definition['schema_version'] == 9
    assert preset.definition['model'] == ''
    assert 'target_chunk_input_tokens' not in preset.definition
    assert warnings
    with pytest.raises(ConfigError):
        load_llm_preset(path, snapshot=True)


def test_project_missing_settings_use_current_global_values(tmp_path):
    from app.config import load_project_settings
    from app.user_config import user_root
    global_path = user_root() / 'config/config.toml'
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[project]\ntarget_language = "English"\n[retry]\nhttp_max_attempts = 3\n')
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'config.toml').write_text('[retry]\nhttp_max_attempts = 7\n')
    config = load_project_settings(project)
    assert config['project']['target_language'] == 'English'
    assert config['retry']['http_max_attempts'] == 7
    assert (project / 'config.toml').read_text() == '[retry]\nhttp_max_attempts = 7\n'


def test_server_unknown_warning_and_invalid_table(tmp_path):
    from app.server_config import server_config_path, load_server_config, save_server_config
    path = server_config_path()
    path.parent.mkdir(parents=True)
    path.write_text('[tasks]\nmax_active_projects = 5\nunknown = 1\n')
    warnings = []
    config = load_server_config(warnings=warnings)
    assert warnings and config['tasks']['max_active_projects'] == 5
    save_server_config(config)
    assert load_server_config(warnings=[]) == config
    path.write_text('tasks = false\n')
    with pytest.raises(ConfigError, match='server.tasks'):
        load_server_config()


def test_incomplete_preset_edit_and_runtime_refusal(tmp_path):
    from app.llm_preset import normalize_llm_preset, validate_executable_preset
    preset = normalize_llm_preset({'extra_body': {'vendor': {'arbitrary': 3}}}, preset_id='draft')
    assert preset['extra_body'] == {'vendor': {'arbitrary': 3}}
    with pytest.raises(ConfigError, match='model'):
        validate_executable_preset(preset)


def test_decision_editable_missing_protocol_and_strict_snapshot(tmp_path):
    from app.decision import load_decision_preset
    path = tmp_path / 'draft.json'
    path.write_text('{"unknown": 1}')
    warnings = []
    assert load_decision_preset(path, warnings=warnings) == {'preset_id': 'draft', 'protocol': ''}
    assert warnings
    with pytest.raises(ConfigError):
        load_decision_preset(path, snapshot=True)


def test_complete_project_does_not_depend_on_invalid_global(tmp_path):
    from app.config import load_project_settings, dump_config
    from app.user_config import user_root
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'config.toml').write_text('')
    config = load_config(project / 'config.toml')
    (project / 'config.toml').write_text(dump_config(config))
    global_path = user_root() / 'config/config.toml'
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[retry]\nhttp_max_attempts = false\n')
    assert load_project_settings(project) == config
    from app.config import normalize_project_settings
    del config['chunking']['stage_overrides']
    with pytest.raises(ConfigError, match='http_max_attempts'):
        normalize_project_settings(config)
    (project / 'config.toml').write_text('[project]\ntarget_language = "English"\n')
    with pytest.raises(ConfigError, match='http_max_attempts'):
        load_project_settings(project)


@pytest.mark.parametrize('missing_ordinary_field', [False, True])
def test_missing_override_container_uses_global_independently_of_other_missing_fields(missing_ordinary_field):
    from app.config import builtin_settings, normalize_project_settings
    from app.user_config import user_root
    global_path = user_root() / 'config/config.toml'
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[chunking.stage_overrides.translation]\ntarget_content_tokens = 17\nmax_input_tokens = 111\n')
    value = builtin_settings()
    del value['chunking']['stage_overrides']
    if missing_ordinary_field:
        del value['retry']['http_max_attempts']
    normalized = normalize_project_settings(value)
    assert normalized['chunking']['stage_overrides'] == {
        'translation': {'target_content_tokens': 17, 'max_input_tokens': 111}}


def test_present_sparse_override_table_inherits_project_common():
    from app.config import builtin_settings, normalize_project_settings
    from app.request_budget import resolve_request_budget
    from app.user_config import user_root
    global_path = user_root() / 'config/config.toml'
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[chunking.stage_overrides.translation]\ntarget_content_tokens = 17\nmax_input_tokens = 111\n')
    value = builtin_settings()
    value['chunking']['stage_overrides'] = {'translation': {'target_content_tokens': 9}}
    del value['retry']['http_max_attempts']
    normalized = normalize_project_settings(value)
    assert normalized['chunking']['stage_overrides'] == {'translation': {'target_content_tokens': 9}}
    assert resolve_request_budget(normalized, 'translation').max_input_tokens == value['chunking']['max_input_tokens']
