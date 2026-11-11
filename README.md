# AstrBot 长期记忆插件

为 AstrBot 提供按用户隔离的长期记忆能力：自动抽取对话中的关键信息、生成用户画像、为每条记忆生成关键字，并在后续对话中按关键字检索、注入提示词。适配 OneBot（aiocqhttp）与 QQ 官方机器人，支持群聊、图片理解、每日定时整理，以及适配手机端的可视化管理页面。

- 插件目录：`astrbot_plugin_longterm_memory`
- 数据存储：SQLite（`data/plugin_data/astrbot_plugin_longterm_memory/memory.db`）
- 外部依赖：无，仅使用 Python 标准库与 AstrBot 自带依赖
- 建议版本：AstrBot `>= 4.13.0`（可视化页面与 Web API 需要较新版本）

## 功能

- **按用户独立记忆**：OneBot 下直接使用消息发送者 QQ 号作为身份；QQ 官方机器人使用 openid，并通过 LLM 从对话中识别用户主动提供的 QQ 号。
- **群聊支持**：群内每位成员单独建档，记忆不会串到其他用户；默认只记录与机器人发生对话的消息。
- **LLM 自动抽取**：根据对话抽取偏好、生活、工作、关系、计划、事实等长期信息。
- **自动用户画像**：综合长期记忆生成 Markdown 画像与标签，注入对话提示词。
- **关键字检索**：每条记忆生成 2~5 个关键字，对话时对当前文本做轻量中英文切词与关键字匹配，选出最相关的记忆。
- **图片理解**：调用多模态模型描述图片，描述会进入当轮消息并参与后续记忆抽取。
- **每日整理**：默认每天 `00:00` 整理此前未整理的日期，生成当日摘要、分类记忆和更新画像；时间可在配置或 WebUI 修改。
- **分区存储**：每日记忆按 `daily/YYYY-MM-DD/类别` 分区，摘要单独存储，便于查看和清理。
- **可视化管理**：插件自带 WebUI，可查看用户、画像、记忆、关键字与摘要；支持删除记忆、删除画像、删除单个关键字、清空用户数据、手动补整理。
- **移动端适配**：页面使用折叠菜单、底部保存栏、响应式布局，窄屏下也能正常操作。
- **与其他插件共存**：不拦截消息、不终止事件，只通过 LLM 请求/响应钩子工作；另外提供公开方法与可选 LLM 工具，方便其他插件读写记忆。

## 安装

1. 将整个 `astrbot_plugin_longterm_memory` 目录放入 AstrBot 的插件目录：

   ```text
   AstrBot/data/plugins/astrbot_plugin_longterm_memory
   ```

2. 打开 AstrBot Dashboard，在「插件管理」中启用「长期记忆」。
3. 首次启用会自动创建数据库，无需手动初始化。
4. 在插件配置页或插件可视化页面中确认模型设置：
   - 「记忆整理模型」留空时使用当前会话模型；
   - 图片识别需要模型支持图片输入，可单独指定「图片识别模型」。

## 工作流程

```text
用户消息
  └─ on_llm_request
       ├─ 识别平台 / 用户 / QQ / 群号
       ├─ 写入 messages（文本、图片、来源群）
       ├─ 图片异步理解 → 写回图片描述
       ├─ 检索用户画像 + 相关记忆 → 注入本轮提示词
       └─ 达到阈值 → 后台调用 LLM 抽取记忆与关键字
  └─ 主模型回复
       └─ on_llm_response → 写入 assistant 消息

每日定时（默认 00:00）
  └─ 汇总未整理日期的对话
       ├─ 生成当日摘要
       ├─ 按类别生成每日记忆（daily/日期/类别）
       ├─ 更新用户画像与标签
       └─ 标记已整理
```

## 配置说明

配置文件为 `_conf_schema.json`，可在 AstrBot 插件配置页或插件 WebUI 的「设置」中修改。

### 基础设置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用长期记忆 |
| `capture_group_all` | `false` | 是否记录群聊中所有消息；默认只记录与机器人对话的群消息 |
| `enable_image_understanding` | `true` | 是否识别图片内容 |
| `max_images_per_message` | `1` | 每条消息最多识别的图片数 |
| `ignore_commands` | `true` | 是否忽略以 `/` 开头的指令 |

### 记忆与整理

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_auto_extract` | `true` | 达到消息阈值后自动抽取记忆 |
| `extract_every_n_messages` | `8` | 每 N 条用户消息抽取一次 |
| `consolidation_time` | `00:00` | 每日整理时间，24 小时制 `HH:MM` |
| `catch_up_on_start` | `true` | 启动后补整理遗漏日期（最多 7 天） |
| `profile_min_messages` | `6` | 生成画像所需的最少消息数 |
| `profile_refresh_days` | `3` | 画像自动刷新间隔 |
| `retention_days` | `0` | 数据保留天数，`0` 表示永久保留 |

### 提示词注入

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `inject_mode` | `extra_user_part` | `extra_user_part` 附加用户内容；`system_prompt` 追加系统提示词；`off` 关闭注入 |
| `max_inject_memories` | `6` | 最多注入记忆条数 |
| `max_inject_chars` | `1200` | 注入内容最大字符数 |
| `max_profile_chars` | `700` | 注入画像最大字符数 |

推荐保持 `extra_user_part`，避免每轮修改系统提示词导致上游模型缓存失效，也能减少与其他修改系统提示词的插件冲突。

### 高级设置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `llm_provider_id` | 空 | 记忆抽取/整理使用的模型，留空使用当前会话模型 |
| `vision_provider_id` | 空 | 图片识别模型，留空复用记忆整理模型 |
| `enable_llm_tools` | `true` | 是否向模型注册记忆读写工具，修改后需重载插件 |
| `debug` | `false` | 输出调试日志 |

## 可视化页面

在 AstrBot Dashboard 的插件卡片中打开「长期记忆」页面。页面包含四个标签：

- **概览**：用户/记忆/关键字/今日消息统计、待整理数量、最近记忆、活跃用户、手动整理。
- **用户**：搜索用户，展开折叠卡片查看画像、标签、每日摘要、关键字和记忆；可删除画像、清空用户数据。
- **记忆**：搜索记忆内容或关键字；点击关键字可筛选；可删除单条记忆或某个关键字。
- **设置**：直接修改常用配置并保存，与 AstrBot 插件配置页同步。

移动端说明：

- 顶部标签可横向滑动；
- 所有列表与分组使用折叠卡片；
- 删除操作使用页面内确认弹窗，兼容 Dashboard 的 iframe 沙箱；
- 设置页底部有固定保存栏。

## 数据表结构

数据库文件位于 `data/plugin_data/astrbot_plugin_longterm_memory/memory.db`。

| 表 | 作用 |
| --- | --- |
| `users` | 用户身份、QQ、昵称、画像、标签、消息计数、首次/最近互动 |
| `messages` | 对话原文、图片描述、角色、群号、会话 ID、日期、整理状态 |
| `memories` | 记忆正文、关键字、类别、重要度、分区、来源、日期 |
| `memory_keywords` | 记忆与关键字的多对多索引 |
| `daily_summaries` | 每位用户每天的摘要与关键词 |
| `job_logs` | 每日整理任务日志 |
| `settings` | 预留键值表 |

分区示例：

```text
daily/2026-11-10/偏好
daily/2026-11-10/生活
daily/2026-11-10/工作
longterm            # 手动或插件写入的长期记忆
```

## 用户识别与群聊行为

- **OneBot（aiocqhttp）**：`sender_id` 即 QQ 号，插件直接使用它作为用户标识；群聊中每位发言者独立建档。
- **QQ 官方机器人**：平台返回的是 openid，不是 QQ 号。插件会用正则与 LLM 抽取用户主动提供的 QQ 号并保存；未提供时仍按 openid 独立记忆。
- **用户 key 格式**：`平台:平台实例:平台用户 ID`。同一用户在不同机器人实例下会产生独立档案，避免多机器人数据串号。
- **默认记录范围**：私聊全部记录；群聊只记录触发机器人回复的消息。若确实需要记录群内所有发言，可开启 `capture_group_all`，但请先评估隐私和模型成本。

## 图片识别

图片会通过多模态模型生成客观描述，例如“图片里是一把木吉他”。描述会：

1. 写入对应消息的 `image_desc`；
2. 在记忆抽取时作为对话内容的一部分；
3. 在 WebUI 的用户详情中随记忆一起展示。

注意事项：

- 需要模型支持图片输入；若当前模型不支持，请单独配置 `vision_provider_id`。
- 每条消息默认只识别 1 张图片，可通过 `max_images_per_message` 调整。
- 图片下载/理解在后台任务中执行，不阻塞主回复。

## 与其他插件协作

插件不会调用 `event.stop_event()`，也不会修改主模型回复，因此可以和其他插件同时启用。

### 公开方法

其他插件可以通过插件实例调用：

```python
meta = self.context.get_registered_star("astrbot_plugin_longterm_memory")
memory_plugin = meta.star_cls if meta else None

if memory_plugin:
    # 检索记忆（返回列表）
    memories = await memory_plugin.search_memories(user_key, "咖啡", limit=5)

    # 获取画像
    profile = await memory_plugin.get_user_profile(user_key)

    # 生成可直接拼进提示词的文本
    block = await memory_plugin.build_memory_prompt(user_key, "用户刚才在聊什么")

    # 写入一条记忆
    memory_id = await memory_plugin.add_memory(
        user_key,
        "用户每周三晚上去上吉他课",
        keywords=["吉他课", "周三"],
        category="计划",
        importance=4,
    )
```

`user_key` 的生成规则见上文；也可以在自己的插件中按同样格式构造。

### LLM 工具

开启 `enable_llm_tools` 后，会向模型注册两个函数工具：

- `recall_user_memory(query)`：检索当前用户的长期记忆；
- `save_user_memory(content, keywords, category, importance)`：把用户明确要求记住的信息写入记忆。

如果与其他插件的工具产生冲突，或希望减少工具定义占用，可以关闭该选项。

### 注入方式

若另一个插件已经大量修改系统提示词，建议保持 `inject_mode = extra_user_part`；若你的上游模型不支持附加内容，可切换为 `system_prompt`。

## 常见问题

**Q：为什么群里其他人的消息没有记忆？**
默认只记录与机器人发生对话的群消息。需要记录所有群发言时，开启 `capture_group_all`。

**Q：QQ 官方机器人为什么显示 openid 而不是 QQ 号？**
QQ 官方接口不直接返回 QQ 号。插件会在用户主动说出 QQ 号时通过正则和 LLM 识别；否则只能按 openid 独立建档。

**Q：为什么没有生成画像？**
画像需要至少 `profile_min_messages` 条消息，并且需要可用的 LLM Provider。每日整理也会更新画像。

**Q：每日整理没有执行？**
确认 AstrBot 进程在设定时间处于运行状态；若错过，重启后会根据 `catch_up_on_start` 补整理最近 7 天。也可以在 WebUI 手动整理指定日期。

**Q：图片没有被识别？**
确认已开启 `enable_image_understanding`，并且模型支持图片输入；必要时单独设置 `vision_provider_id`。

**Q：删除记忆后画像还会提到它吗？**
画像是一份独立的生成文本，删除记忆不会自动重写画像。可在 WebUI 删除画像，下一次整理会重新生成。

**Q：数据会不会很大？**
可在 WebUI 设置 `retention_days` 自动清理旧消息与记忆，或在用户详情中清空单个用户数据。

## 隐私说明

- 对话内容、图片描述、画像和记忆会发送给配置的 LLM Provider 进行处理；
- 数据默认保存在本机 SQLite 文件，不会主动上传到其他服务；
- 开启 `capture_group_all` 会记录群内所有成员发言，请确保符合所在群组的规定；
- 建议定期备份 `memory.db`，并在不需要时使用 WebUI 清理数据。

## 目录结构

```text
astrbot_plugin_longterm_memory/
├── main.py                 # 插件入口、事件钩子、Web API、定时任务
├── memory_store.py         # SQLite 存储层
├── memory_engine.py        # LLM 抽取、检索、每日整理
├── identity.py             # 平台无关的用户身份解析
├── tools.py                # 可选 LLM 函数工具
├── _conf_schema.json       # 插件配置定义
├── metadata.yaml           # 插件元数据
├── requirements.txt        # 无额外依赖
├── README.md
├── .astrbot-plugin/i18n/   # 页面标题国际化
└── views/memory/
    ├── index.html          # 可视化页面
    ├── app.js              # 页面逻辑
    └── style.css           # 移动端优先样式
```

## 兼容性

- 已在 AstrBot 当前主线的插件 API 上按以下接口实现：
  - `filter.on_llm_request` / `filter.on_llm_response`
  - `filter.on_astrbot_loaded`
  - `context.register_web_api`
  - `context.add_llm_tools`
  - `views/<name>/index.html` 插件页面
- 对旧版本做了兼容处理：`TextPart` 有回退导入；没有 Web API 时核心记忆功能仍可使用，但可视化页面不可用。
- 不依赖 jieba、向量数据库或第三方 HTTP 库，部署成本低。

## 开发自检

```bash
python3 -m py_compile main.py memory_store.py memory_engine.py identity.py tools.py
node --check views/memory/app.js
```

## License

MIT
