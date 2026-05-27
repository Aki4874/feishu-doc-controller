# 飞书文档控制器 (Feishu Doc Controller)

一个功能强大的 AstrBot 异步插件，支持通过飞书开放平台 API 对飞书文档进行全生命周期管理。所有功能支持 AI 自动调用（通过 FunctionTool 机制）。

## 功能特性

- **文档操作**：搜索、读取、创建、写入、删除飞书文档
- **多维表格**：查询、新增、更新、删除多维表格记录
- **电子表格**：读取、写入单元格，创建电子表格
- **幻灯片**：创建幻灯片，查看演示文稿信息
- **画板**：获取、创建、删除画板节点
- **知识库**：列出知识库、管理节点、成员管理
- **权限管理**：添加/转移权限、查看协作者、权限设置
- **AI 增强**：Markdown 本地解析、富文本转换
- **AI 自动调用**：通过 FunctionTool 机制让 AI 自动搜索、读取、创建文档

---

## 快速开始

### 第一步：创建飞书应用

1. 打开 [飞书开放平台](https://open.feishu.cn/)
2. 创建一个企业自建应用
3. 记录 **App ID** 和 **App Secret**

### 第二步：配置权限

在应用的「权限管理」中添加以下权限：

- `docx:document` — 文档读写
- `docx:document:readonly` — 文档只读
- `docx:document.block:convert` — 文档块转换
- `docs:doc:readonly` — 旧版文档只读
- `drive:drive` — 云空间读写
- `drive:drive.search:readonly` — 云空间搜索
- `drive:file` — 文件管理
- `wiki:wiki:readonly` — 知识库只读
- `wiki:node:create` — 创建知识库节点
- `wiki:member:create` — 添加知识库成员
- `board:whiteboard:node:read` — 画板节点读取
- `board:whiteboard:node:create` — 画板节点创建
- `board:whiteboard:node:delete` — 画板节点删除
- `bitable:app` — 多维表格读写
- `bitable:app:readonly` — 多维表格只读
- `sheets:spreadsheet` — 电子表格读写
- `sheets:spreadsheet:create` — 创建电子表格
- `slides:presentation:create` — 创建幻灯片
- `slides:presentation:read` — 读取幻灯片
- `space:document:delete` — 删除文档
- `space:document:move` — 移动文档
- `space:folder:create` — 创建文件夹

### 第三步：发布应用

1. 在「版本管理与发布」中创建新版本
2. 提交审核并发布应用
3. 确保应用已被目标用户/群组安装

### 第四步：在 AstrBot 中配置

1. 将插件放入 AstrBot 的 `plugins/` 目录
2. 重启 AstrBot
3. 在管理面板中配置 `feishu_app_id` 和 `feishu_app_secret`
4. 保存配置即可开始使用

---

## 命令格式

基本命令格式：`/文档 [操作] [参数]`

## 常用命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/文档 搜索 关键词` | 搜索文档 | `/文档 搜索 项目计划` |
| `/文档 读取 [ID/URL]` | 读取文档内容 | `/文档 读取 https://xx.feishu.cn/docx/xxx` |
| `/文档 创建 标题` | 创建新文档 | `/文档 创建 我的笔记` |
| `/文档 信息 [ID/URL]` | 查看文档信息 | `/文档 信息 doxcnxxxx` |
| `/文档 知识库列表` | 列出所有知识库 | `/文档 知识库列表` |
| `/文档 知识库 [ID]` | 列出知识库节点 | `/文档 知识库 123456` |
| `/文档 状态` | 检查插件运行状态 | `/文档 状态` |
| `/文档 帮助` | 显示帮助信息 | `/文档 帮助` |
| `/飞书 搜索 关键词` | 快捷搜索（5条） | `/飞书 搜索 会议` |
| `/飞书 读取 [ID/URL]` | 快捷读取（500字） | `/飞书 读取 doxcnxxxx` |
| `/读取文档 [ID/URL]` | 直接读取文档 | `/读取文档 https://xx.feishu.cn/docx/xxx` |
| `/搜索文档 关键词` | 直接搜索文档 | `/搜索文档 周报` |
| `/创建文档 标题` | 直接创建文档 | `/创建文档 新文档` |

---

## AI 自动调用示例

当 AI 助手插件配合使用时，AI 可以自动识别用户意图并调用相应的飞书工具：

- **用户**：「帮我搜索关于Q3计划的文档」
  → AI 自动调用 `search_feishu_docs(关键词="Q3计划")`

- **用户**：「读取这个文档 https://xx.feishu.cn/docx/xxx」
  → AI 自动调用 `read_feishu_doc(doc_id_or_url="https://...")`

- **用户**：「帮我创建一个标题为"会议纪要"的新文档」
  → AI 自动调用 `create_feishu_doc(title="会议纪要")`

- **用户**：「帮我总结一下"产品文档"知识库」
  → AI 自动调用 `find_and_summarize_wiki_v2(wiki_name="产品文档")`

---

## 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `feishu_app_id` | string | 空 | 飞书应用 App ID（必填） |
| `feishu_app_secret` | string | 空 | 飞书应用 App Secret（必填） |
| `cache_dir` | string | `./data/feishu_cache` | 缓存目录路径 |
| `sync_interval` | int | 3600 | 自动同步间隔（秒） |
| `max_retry` | int | 3 | API 请求最大重试次数 |
| `retry_delay` | float | 1.0 | 重试延迟（秒） |
| `enable_auto_sync` | bool | false | 是否启用自动同步 |
| `admin_users` | list | [] | 管理员用户 ID 列表 |

示例配置 JSON：

```json
{
  "feishu_app_id": "cli_a1b2c3d4e5f6",
  "feishu_app_secret": "your_secret_here",
  "cache_dir": "./data/feishu_cache",
  "sync_interval": 3600,
  "max_retry": 3,
  "retry_delay": 1.0,
  "enable_auto_sync": false,
  "admin_users": []
}
```

---

## 常见问题 FAQ

### Q: 提示「未配置 App ID 或 App Secret」
A: 请在 AstrBot 管理面板的插件配置中填写 `feishu_app_id` 和 `feishu_app_secret`。

### Q: 提示「App ID 格式错误」
A: 飞书 App ID 应以 `cli_` 开头，请检查是否填写正确。

### Q: 读取文档返回的内容很少或为空
A: 插件内置了多级 Fallback 机制（新版 docx API → 旧版 docs API → 文档块API），请确保已授予完整的文档读写权限。

### Q: 创建文档块失败（code: 99992402）
A: 请确保已添加 `docx:document.block:convert` 权限。

### Q: 搜索不到文档
A: 请确保已添加 `drive:drive.search:readonly` 权限。

### Q: 支持哪些文档类型？
A: 支持飞书文档（Docx）、多维表格（Bitable）、电子表格（Sheet）、幻灯片（Slide）、画板（Board）、知识库（Wiki）等。

---

## 技术架构

- **异步架构**：基于 `aiohttp` 的全异步 HTTP 调用
- **Token 缓存**：内存缓存 Tenant Access Token，过期前自动刷新
- **智能 Fallback**：文档读取支持新版/旧版 API 自动降级
- **Markdown 解析**：本地纯 Python 解析，不依赖飞书 API
- **FunctionTool**：标准化 AI 工具接口，支持多轮调用
