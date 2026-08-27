# Another LLM Translator Terminology Validation Plugin

This package is the reference external Translation Validator for Another LLM
Translator. It provides the optional `preferred_term_usage` validator.

The validator reports an advisory finding when a published terminology match
has a recommended translation that does not occur in the candidate translation.
It does not require a term to be used, and the host allows at most one targeted
repair before accepting the candidate with a warning. The host decides which
terms matched; this plugin does not read project files or the terminology
database.

Install it in a Python environment with the host package:

```bash
python -m pip install another-llm-translator-term-validation
```

The official desktop build includes the plugin, but disables it by default.
The desktop application does not support runtime plugin installation or upgrades.
