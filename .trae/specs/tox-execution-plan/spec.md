# tox 可保存与重放执行计划 - 产品需求文档

## Overview
- **Summary**: 为 tox 增加可选的执行计划（execution plan）能力，在创建或修改任何目标环境、执行命令前生成可审查、可持久化的冻结计划，并在后续重放时严格验证计划身份与外部依赖一致性。
- **Purpose**: 解决 `tox config` / 固定版本输出只能展示当前进程即时解析结果，之后配置文件、环境变量、factor、插件、解释器或外部包发生变化时，实际 provision 与执行内容可能偏离审查结果的问题。
- **Target Users**: 需要在 CI 中先审批再执行的平台维护者、测试工程师、插件开发者，以及需要可审计 tox 运行内容的项目团队。

## Goals
- 提供可保存的 JSON 执行计划，内容足以人工审查和机器重放。
- 在计划中冻结选中的运行/包装环境、依赖图、解析后的核心与环境配置、解释器/包装约束、命令及其来源。
- 用内容哈希关联主配置、引用文件、内联/外部插件和关键运行时组件。
- 重放时以计划为唯一执行依据；任何不兼容都在 provision 或目标环境创建/修改前报告差异并拒绝执行。
- 让 provision 后新 tox 进程、并行调度、包装环境和普通顺序运行共享同一计划身份。
- 保证未使用计划时现有直接运行、`config`、factor 选择、`package=skip`、editable、editable legacy 与插件行为兼容。
- 计划、日志和 result-json 均标明实际计划身份，且不泄漏敏感环境变量明文。

## Non-Goals
- 不把普通 PyPI 依赖解析成完整供应链锁文件；已有 `pylock` 仍负责锁定可锁定依赖。计划冻结 tox 解析出的直接依赖、约束/锁文件身份与安装输入，但不替代包管理器的解析结果。
- 不在计划中签名或证明操作者身份；计划身份是内容哈希，不是数字签名。
- 不改变未启用计划时的 tox 行为。
- 不要求首个版本支持交互式 `exec`、`devenv` 的计划生成；计划第一阶段覆盖 `run`、`run-parallel`（含 `p`）和默认 legacy 运行路径。
- 不在缺少 `requires` 中声明的 tox/插件时生成“完整计划”。生成完整计划前，这些插件必须已可导入；CI 可先读取 provision 元数据准备生成环境，再在干净的已审批运行中让 replay 执行 provision。

## Background & Context
当前主要链路如下：

- CLI 由 [parser.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/config/cli/parser.py) 和 [parse.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/config/cli/parse.py) 解析，默认值可能来自命令行、`TOX_*` 环境变量或用户配置。
- [main.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/run.py) 构建 `State` 后先进入 [provision.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/provision.py)，provision 子进程重新携带原始 argv 启动 tox。
- [state.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/session/state.py) 与 [env_select.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/session/env_select.py) 负责插件扩展环境、factor/label/env_list 选择、runner 与包装环境构建。
- 顺序与并行共用 [common.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/session/cmd/run/common.py)，并行使用同进程线程池；包装环境可被多个运行环境共享。
- 命令由 [single.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/session/cmd/run/single.py) 从已解析配置中读取并交给 [api.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/tox_env/api.py) 执行。
- result-json 由 [main.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/journal/main.py)、[env.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/journal/env.py) 和 [__init__.py](file:///Users/ding/Documents/swe/0910072/project-08/src/tox/journal/__init__.py) 写出，单命令日志由 ToxEnv 写出。

## Functional Requirements

- **FR-1: 计划生成入口**
  - `run`、`run-parallel`/`p` 和默认 legacy 运行路径支持 `--save-plan PATH`。
  - 使用 `--save-plan` 时 tox 只完成解析、选择、非修改性解释器发现和计划写入，不 setup、不 provision、不创建/修改目标环境、不安装包、不执行测试/构建命令。
  - 如果生成完整计划所需的 `requires` 缺失，`--save-plan` 必须拒绝生成并给出与 `--no-provision` 一致的缺失需求信息，而不是先 provision。
  - `--plan PATH` 用于重放已保存计划；重放命令的模式必须与计划记录一致。

- **FR-2: 计划身份与原子持久化**
  - 计划使用显式 schema version，并以 canonical JSON 的 SHA-256 作为内容身份。
  - 文件必须通过同目录临时文件、flush/fsync、原子 rename 发布；中断只能留下不会被 `--plan` 接受的临时文件。
  - replay 必须验证 schema、摘要和计划内部引用；损坏、半成品或身份不一致的计划不得执行。

- **FR-3: 冻结环境选择与依赖图**
  - 计划必须冻结最终选中的运行环境、包装环境、不可用环境、环境角色和 run/package 关联。
  - 必须记录 env_list、`-e`、label、factor、`--skip-env`、插件扩展环境等选择输入以及最终展开结果。
  - 必须冻结 `depends` 的模式匹配结果、每个环境的实际依赖边和稳定拓扑顺序。

- **FR-4: 冻结解析后配置与来源**
  - 计划必须冻结核心配置、每个运行/包装环境配置，包括默认值、CLI、用户配置、环境变量、配置文件、base 继承、override、factor 条件、TOML product/条件展开、插件钩子注册/修改后的最终值。
  - 每个冻结项必须可追溯到一个或多个来源：配置文件路径/section/key、base section、CLI token、`TOX_*` 环境变量、用户配置、override、默认值、插件/内存 loader 或 provision 内存种子。
  - 计划展示的命令必须区分 `commands_pre`、`commands`、`commands_post`、`extra_setup_commands`、包装环境 build commands 等阶段，并记录 argv、run_id、cwd、stdin/capture 约束、ignore/invert exit code、retry 等执行属性和来源。

- **FR-5: 冻结解释器与包装约束**
  - 计划必须记录每个 Python 环境的 base_python 候选、发现优先级、最终解释器路径、实现、版本、架构/机器特性、free-threaded/debug 标记，以及解释器文件内容身份。
  - 计划必须冻结 runner 类型、virtualenv/Python isolation 约束、site-packages、always-copy、download、virtualenv_spec、system/set env 中影响 virtualenv/pip 的变量来源。
  - 计划必须冻结 package 模式（`wheel`、`sdist`、`sdist-wheel`、`editable`、`editable-legacy`、`deps-only`、`skip`、`external`）、package_root、package env、child package env、editable/develop 选项、依赖/约束/pylock 输入。
  - 使用 `--installpkg` 时必须记录外部包路径、大小、SHA-256 和类型；replay 必须在安装前验证。

- **FR-6: 源文件与插件身份**
  - 计划必须记录主 tox 配置源和解析过程中实际读取的本地引用文件身份，包括 set_env env-file、requirements/constraints 文件、base_python_file、pylock 等；每条记录至少包含角色、路径、类型、大小和 SHA-256。
  - 计划必须记录 tox 版本、provision requires、内联 toxfile、外部 setuptools entrypoint 插件、插件分发名/版本、模块文件、内容摘要和实现/参与的钩子。
  - 源文件或插件新增、缺失、内容变化、钩子集合变化或版本/入口不兼容时，replay 必须输出结构化差异并拒绝执行。

- **FR-7: 敏感信息处理**
  - 名称匹配敏感规则的 set_env、pass_env、`{env:NAME}`、`TOX_*` CLI 默认值和其他环境变量绑定，计划中只能记录名称、来源、是否存在、长度和 SHA-256 等校验信息，不得写入明文。
  - 如果解析后的配置或命令包含敏感变量值，持久化计划中必须以秘密绑定/占位符表示；replay 仅在当前环境变量或已验证源文件中的值与摘要一致时还原。
  - 计划的人类可读摘要和 JSON 均必须通过相同的秘密扫描与脱敏。

- **FR-8: 无漂移重放**
  - replay 的环境选择、配置值、factor 展开、依赖图和命令必须从计划中的内存快照/绑定构造，不得被当前配置文件、环境变量、用户配置或插件钩子重新解析后的不同值覆盖。
  - 磁盘源文件仅可在哈希验证通过后用于计划中显式标记且未存储明文的秘密值；任何不匹配都必须在读取该值前拒绝。
  - 影响执行的 replay CLI 参数必须与计划一致；只允许明确的报告类参数（例如 verbose、color、result-json 输出路径）在不改变执行内容的前提下覆盖。
  - replay 必须在 provision、目标环境 setup、包装环境 setup、外部包安装或命令执行前完成不兼容检查。

- **FR-9: provision、并行与包装共享同一计划**
  - 当 replay 触发 provision 时，provision 子进程 argv 必须携带同一计划路径；provision 后的新 tox 进程必须重新验证计划并使用相同冻结内容。
  - 外层 tox 缺少计划所需且已在 `requires` 中声明的插件时，可以进入已批准的 provision；已安装但内容不匹配、未声明且缺失的插件必须直接拒绝。
  - 同进程并行 worker、共享包装环境、legacy 包装路径和顺序运行必须访问同一个 `State`/计划快照，不能各自重新解析可变来源。

- **FR-10: 结果与日志关联计划身份**
  - 控制台开始和/或结束报告必须显示计划 ID。
  - 每个命令日志文件必须包含计划 ID 和计划 schema version。
  - `--result-json` 顶层必须包含计划 ID、schema version、计划路径和计划内容摘要；环境/命令记录必须能关联到所属计划。

- **FR-11: 兼容性与可扩展性**
  - 未传 `--save-plan` 或 `--plan` 时，现有 CLI、配置解析、插件加载、直接运行、`tox config`、factor 选择、包装模式和测试结果保持不变。
  - 第三方 runner/plugin 应能在不实现可选计划钩子的情况下保持现有行为；内置 virtualenv runner 必须提供精确计划信息，其他 runner 至少提供可验证的通用配置与 runner 身份，无法安全保证解释器身份时应拒绝计划生成或明确标记不支持。

## Non-Functional Requirements

- **NFR-1: 安全性**: 计划文件不得包含敏感环境变量明文；秘密绑定必须默认拒绝，不得在哈希不匹配时继续。
- **NFR-2: 正确性**: 顺序、并行、provision、legacy packaging、package skip、editable/editable-legacy、factor/label 选择均需有回归测试。
- **NFR-3: 原子性与跨平台**: 文件写入在 POSIX 与 Windows 上均不得发布半成品；路径处理使用 tox 现有跨平台抽象。
- **NFR-4: 可审查性**: 计划 JSON 结构稳定、字段命名清晰、差异报告能指出预期值/实际值/来源。
- **NFR-5: 性能**: 计划生成不得创建目标虚拟环境；解释器发现只允许使用非修改性查询。
- **NFR-6: 可维护性**: 计划 schema、序列化、哈希、来源记录、replay 校验和执行守卫应模块化，避免散落在命令处理函数中。

## Constraints
- **Technical**: Python >= 3.10；复用现有 pluggy 插件系统、Config/ConfigSet/Loader、EnvSelector、LocalSubProcessExecutor、Journal 和 virtualenv/python-discovery 抽象。
- **Business**: 该能力必须完全 opt-in；不能破坏 tox 当前用户与外部插件的默认行为。
- **Dependencies**: 解释器发现依赖 `python-discovery`/`virtualenv`；插件元数据依赖 Python importlib metadata；provision 依赖现有 `requires` 机制。

## Assumptions
- CLI 采用 `--save-plan PATH` 生成计划、`--plan PATH` 重放计划，而不是新增嵌套的 `tox plan run ...` 子命令；这样可完整复用各运行模式已有的参数定义。
- 完整计划生成阶段要求 `requires` 中声明的 tox 和插件已经可导入；干净 CI 阶段可先准备生成器，审批计划后再由 replay 自动 provision 运行环境。
- 计划保存于待运行项目可访问的文件系统；跨机器重放需要计划记录的相对/绝对路径、源文件和外部 installpkg 按同一工作区布局可用。
- 网络上的 requirements/constraints URL 不属于本次本地源文件冻结范围；本地 pylock 和本地 requirement 文件必须冻结。

## Acceptance Criteria

### AC-1: 可生成不修改环境的计划
- **Type**: `rule`
- **Given**: 一个包含至少两个运行环境和依赖关系的 tox 项目。
- **When**: 用户执行 `tox run -e ... --save-plan plan.json`。
- **Then**: tox 退出码为 0，写入合法计划文件，不创建/清理/修改 `.tox` 中目标环境，不安装包，不执行测试命令。
- **Pass Condition**: 测试断言计划存在且可解析；环境目录、安装命令和测试命令均未发生；进程输出明确给出计划路径和 ID。
- **Evidence**: 新增单元/集成测试、文件系统状态断言和命令调用断言。

### AC-2: 计划完整冻结选择、图、配置和命令
- **Type**: `rule`
- **Given**: 配置同时使用 env_list/factor/label、base 继承、override、`commands_pre/main/post`、deps 和包装环境。
- **When**: 生成计划并检查 JSON。
- **Then**: JSON 包含最终环境集合、package/run 关联、依赖边和拓扑顺序、核心/环境配置值、命令 argv/阶段/cwd/执行选项，以及每个值/命令的来源。
- **Pass Condition**: 计划内容与当前 `tox config --format json` 的最终值一致，并额外包含来源、图和包装信息。
- **Evidence**: 针对 INI 与 TOML 的计划序列化测试、factor/label 测试和来源断言。

### AC-3: 计划身份和原子写入可靠
- **Type**: `rule`
- **Given**: 一次成功的 `--save-plan`。
- **When**: 检查目标目录、计划内容和摘要；并模拟写入中断/截断/篡改。
- **Then**: 成功路径只通过原子 rename 发布完整文件；临时文件不会被 replay 接受；截断或修改任何计划字节都会导致身份校验失败。
- **Pass Condition**: 哈希等于 canonical content 的 SHA-256；半成品/篡改计划在任何环境操作前被拒绝。
- **Evidence**: 原子写测试、schema/哈希校验测试和失败路径测试。

### AC-4: replay 使用计划而不是当前可变来源
- **Type**: `rule`
- **Given**: 已生成计划，随后修改 tox 配置、CLI 相关环境变量、factor 输入或用户配置。
- **When**: 用户执行原命令并加 `--plan plan.json`。
- **Then**: tox 报告计划与当前来源的差异并返回非零，不 provision、不 setup、不安装、不执行命令；若来源恢复为计划身份，则重放使用计划中的冻结值。
- **Pass Condition**: 修改源后 replay 失败且无环境修改；来源恢复后执行的环境、配置和命令与计划完全一致。
- **Evidence**: 参数化集成测试覆盖配置文件、环境变量、factor/label、用户配置和 override。

### AC-5: provision 后进程使用同一计划
- **Type**: `rule`
- **Given**: 计划记录了当前外层环境缺失但列在 `requires` 中的 tox/插件版本，且插件在计划生成环境中可用。
- **When**: 在干净运行环境中执行 `--plan plan.json`。
- **Then**: 外层 tox 不从缺失插件重新解析目标配置，而是依据计划进入 provision；provision 子进程 argv 携带同一计划路径，内层进程重新验证后执行冻结计划。
- **Pass Condition**: 外层和内层日志/result-json 具有相同计划 ID；内层环境选择和命令不随 provision 期间文件变化而改变。
- **Evidence**: provision 集成测试、argv 断言和内外层计划 ID 断言。

### AC-6: 插件变化在执行前被拒绝
- **Type**: `rule`
- **Given**: 计划由带内联或外部插件的项目生成。
- **When**: replay 前修改 toxfile/插件模块文件、阻断 entrypoint、改变插件分发版本或增加参与同名钩子的插件。
- **Then**: tox 输出插件身份差异并拒绝执行。
- **Pass Condition**: 任何插件新增、缺失、内容或钩子集合不兼容都发生在目标环境/provision 创建前；`requires` 中声明且当前缺失的待 provision 插件除外。
- **Evidence**: 内联插件和 setuptools entrypoint 插件测试，包含允许 provision 与拒绝不匹配两类场景。

### AC-7: 解释器与外部 installpkg 不兼容时拒绝
- **Type**: `rule`
- **Given**: 计划记录了每个环境的解释器身份，或使用 `--installpkg` 记录了包文件哈希。
- **When**: replay 前替换解释器路径/版本，或替换、修改、删除外部 installpkg。
- **Then**: tox 在创建解释器环境或安装包前报告差异并拒绝执行。
- **Pass Condition**: 不调用 virtualenv 创建，不执行 pip/包安装；差异列出计划身份与当前身份。
- **Evidence**: virtualenv runner 计划测试、缺失解释器 skip 策略测试和 installpkg 哈希测试。

### AC-8: 包装模式与包装环境被同一计划约束
- **Type**: `rule`
- **Given**: 分别使用 `package=skip`、sdist/wheel、editable、editable-legacy、external cmd builder 和共享 package env。
- **When**: 生成并重放计划，然后修改包装配置或包装命令。
- **Then**: 计划记录的 package 模式、package env 图、build 命令、依赖与实际执行一致；修改后 replay 在包装环境 setup 前拒绝。
- **Pass Condition**: 顺序和并行运行中共享 package env 均从同一计划快照构造，包装配置漂移无法进入执行。
- **Evidence**: 包装模式参数化集成测试和并行共享 package env 测试。

### AC-9: 敏感值不进入计划
- **Type**: `rule`
- **Given**: set_env、pass_env、`{env:NAME}` 或 `TOX_*` CLI 默认值包含名称如 `TOKEN`、`PASSWORD`、`API_KEY` 的变量及其秘密值。
- **When**: 生成计划并搜索整个计划文件。
- **Then**: 文件只包含变量名、来源、存在性、长度和摘要等元数据，不包含秘密明文；人类摘要同样脱敏。
- **Pass Condition**: 秘密字符串及其可逆编码均不出现在计划文件中；当前值摘要匹配时 replay 可执行，摘要不匹配时拒绝。
- **Evidence**: 秘密扫描测试、脱敏快照测试和 replay 绑定失败测试。

### AC-10: 结果、日志和 result-json 标明计划身份
- **Type**: `rule`
- **Given**: 一次带 `--plan` 的运行，启用或不启用 `--result-json`。
- **When**: 运行结束并检查控制台、命令日志和 result-json。
- **Then**: 三类输出均包含同一计划 ID；result-json 还包含 schema version 与计划路径/摘要。
- **Pass Condition**: 所有执行记录可通过计划 ID 追溯到同一个冻结计划。
- **Evidence**: journal 单元测试、日志文件内容测试和顺序/并行集成测试。

### AC-11: 未启用计划时保持兼容
- **Type**: `rule`
- **Given**: 不传 `--save-plan` 或 `--plan`。
- **When**: 运行现有直接运行、legacy、`tox config`、factor 选择、package skip、editable/editable-legacy 和插件测试。
- **Then**: CLI、配置解析、环境创建、输出、退出码和插件钩子调用顺序与变更前一致。
- **Pass Condition**: 现有相关测试不改变预期即可通过；新增计划字段不得写入未启用计划的 result-json。
- **Evidence**: 全量相关既有测试和兼容性快照测试。

### AC-12: 计划审查体验与架构集成质量
- **Type**: `rubric`
- **Dimension**: 可审查性、最小惊讶和架构内聚性。
- **Scale**: 1-5
- **Anchors**: 1 = 计划只是难以理解的完整内部对象转储，差异无法定位；3 = 能找到主要配置和命令但来源/阶段/差异不完整；5 = 计划按选择、图、配置、命令、解释器、包装、身份和秘密绑定清晰组织，差异能直接定位到来源与环境，且实现复用现有 Config/Loader/Journal 抽象而不是旁路复制执行逻辑。
- **Pass Threshold**: >= 4
- **Evidence**: 计划 schema 审查、代表性 fixture、代码结构评审和独立 review。
