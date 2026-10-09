# Another LLM Translator Terminology Validation Plugin

This package is the reference external Translation Validator for Another LLM
Translator. It provides the optional `preferred_term_usage` validator.

The validator reports an advisory finding when a published terminology match has a recommended translation that does not occur in the candidate translation. It does not require a term to be used, and the host allows at most one targeted repair before accepting the candidate with a warning.

An optional experimental host Decision service can review missing terms in context before repair. Enable it in translation validation settings. The protocol is defined in [Adapter contracts](../../docs/ADAPTERS.md#5-decision-preset-与校验服务实验).

The host decides which terms matched; this plugin does not read project files or the terminology database.

The official desktop build includes this directory plugin. A user plugin can be unpacked into
the host user's `plugins/` directory and is loaded after the application restarts. The plugin
must use the manifest and public `app.plugin_api` contract documented in `docs/ADAPTERS.md`.
