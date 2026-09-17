# 目录插件第一阶段实施计划

## 已确定边界

- 用户插件手动解压到 `<用户数据目录>/plugins/<目录名>/`，重启后生效。
- 官方插件与用户插件统一使用目录 manifest 发现；第一阶段移除 entry point 发现。
- TXT、EPUB 和基础翻译校验器继续由宿主直接注册；插件 ID、Adapter ID、Validator ID、扩展名冲突都明确失败。
- 插件只依赖宿主公共 API 与标准库，不提供依赖安装、vendor、覆盖、热加载或管理页面。
- 插件以可信同进程代码运行；协议不符、声明损坏或加载失败时明确阻止启动并显示原因。

## 实施步骤与 Commit

1. **`refactor: expose minimal plugin public API`**
   新增 `app.plugin_api`，集中公开 `PluginDescriptor`、Document Adapter/导入输出/选项契约、翻译校验契约、严格解码 API 和官方插件所需错误类型。官方插件和测试改用该入口；协议保持 12，entry point 发现暂时保留。
2. **`feat: load external plugins from manifest directories`**
   实现官方/用户目录发现、manifest 校验、独立包加载、冲突检查和每进程复用的注册结果，接入 CLI/Web 启动；移除官方插件 entry point 配置。
3. **`build: bundle manifest plugins with the sidecar`**
   将官方插件目录作为资源打包，调整冻结收集与构建文档；使用冻结进程验证官方插件和新增用户插件。
4. **`fix: display plugin startup failures in desktop`**
   完善 sidecar 提前退出与 stderr 诊断，增加安全的本地启动错误页，并用浏览器实际验证正常与失败启动路径。

## 验收

- 有效外部插件能参与实际导入或校验流程；插件内部相对导入可用且命名空间互不干扰。
- 协议不符、manifest/入口损坏、依赖缺失、描述符不一致和各类标识冲突均明确失败，不静默回落。
- 同一进程重复查询复用已加载结果；CLI 返回非零，Web 不启动服务，桌面显示具体错误。
- 相关 Python 测试、冻结 sidecar 验证、前端检查和 `git diff --check` 按变更范围执行；不调用真实模型。

## 默认假设

- 不增加权限/capability 元数据、宿主版本范围、依赖安装、兼容转换层或运行时重载接口。
- 先验证当前 macOS 冻结流程；未实际构建验证前不宣称其他平台通过。
