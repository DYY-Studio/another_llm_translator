from dataclasses import replace

import pytest

from app.errors import ConfigError
from app.plugins import _builtin_plugins, _validate_plugins, load_plugins


def test_plugins_declare_optional_decision_prompts():
    plugins = load_plugins()
    declarations = [prompt for plugin in plugins for prompt in plugin.decision_prompts]
    assert {prompt.validator_id for prompt in declarations} == {"preferred_term_usage", "segment_alignment"}
    for prompt in declarations:
        assert set(prompt.defaults) == {"en", "zh-CN"}
        assert all(set(content["criteria"]) == set(prompt.choice_ids) for content in prompt.defaults.values())


def test_prompt_declaration_must_belong_to_its_plugin():
    plugin = _builtin_plugins()[1]
    foreign = replace(plugin.decision_prompts[0], validator_id="foreign")
    with pytest.raises(ConfigError, match="Decision"):
        _validate_plugins([replace(plugin, decision_prompts=(foreign,))])
