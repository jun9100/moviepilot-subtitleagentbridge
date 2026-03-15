# SubtitleAgentBridge（MoviePilot 插件）

`SubtitleAgentBridge` 是用于对接 `moviepilot-subtitle-agent` 的 MoviePilot 插件。

> 当前文档对应版本：`v0.5.59`

## 插件作用

- 监听 MoviePilot 入库事件（`TransferComplete`）。
- 调用 Subtitle Agent 搜索并下载字幕。
- 将字幕写入视频同目录。
- 支持“已入库媒体批量补字幕”。

## 仓库结构

- `plugins.v2/subtitleagentbridge/__init__.py`
- `package.v2.json`
- `package.json`

## 安装方式

1. 在 MoviePilot 的插件市场仓库设置中加入本仓库地址。
2. 安装 `SubtitleAgentBridge`。
3. 在插件配置页填写 Subtitle Agent 服务地址并启用。

## 推荐配置

- `host`: `http://<subtitle-agent-host>:8178`（示例）
- `search_path`: `/api/v1/moviepilot/subtitles/search`
- `languages`: `zh-cn,zh-tw`
- `limit`: `5`
- `timeout`: `60` 或更高
- `auto_timing_sync`: `true`（建议开启）
- `auto_timing_max_offset_seconds`: `120`
- `periodic_enabled`: `true`（建议开启，定期补齐已入库缺字幕文件）
- `periodic_mode`: `interval`（每隔 N 小时）或 `daily`（每天固定时间）
- `periodic_interval_hours`: `24`（`interval` 生效）
- `periodic_daily_time`: `03:30`（`daily` 生效）
- `periodic_max_files`: `200`
- `periodic_recursive`: `true`
- `periodic_overwrite`: `false`（推荐，避免定时任务反复覆盖同一批字幕）
- `embedded_subtitle_skip_mode`: `chinese`（推荐，仅内封中文字幕才跳过；可选 `any/off`）

### 自动时间轴校正

插件会在写入字幕前自动尝试校时：

- 仅在目标视频同目录存在“同名参考字幕”时触发（如 `xxx.en.srt`、`xxx.ass`）。
- 自动估算最优整体偏移并平移时间轴。
- 没有足够置信度时不会改动原字幕（避免误修正）。

### 回填目录策略（重点）

为避免字幕写入未整理目录，请配置：

- `include_paths`：仅扫描刮削后目录（例如 `/media/tv,/media/movies`）
- `exclude_paths`：明确排除未整理目录（例如 `/media/downloads,/media/整理前,/media/刷流`）
- `exclude_keywords`：建议保留默认
- `title_aliases`：标题别名映射（中日/中英名不一致时推荐）

默认关键词已包含：

`整理前,刷流,strm,stream,downloads,download,incoming,temp,cache`

别名示例：

`短剧开始啦=コントが始まる; 黄石：法警小队=Marshals`

## 插件 API

- `/api/v1/plugin/SubtitleAgentBridge/download_subtitle`
- `/api/v1/plugin/SubtitleAgentBridge/download_subtitle_async`
- `/api/v1/plugin/SubtitleAgentBridge/job_status`
- `/api/v1/plugin/SubtitleAgentBridge/notify_status`
- `/api/v1/plugin/SubtitleAgentBridge/backfill_directory`
- `/api/v1/plugin/SubtitleAgentBridge/submit_captcha`

必填参数：

- `apikey`（MoviePilot 的 API_TOKEN）

## 文档脱敏说明

- 文档中的 `apikey`、主机地址、路径均为占位符示例（如 `<API_TOKEN>`、`<moviepilot-host>`）。
- 请勿将真实 API_TOKEN、Cookie、代理账号等写入 README 或公开截图。
- 生产环境建议通过 MoviePilot 配置项或环境变量注入敏感信息。

## 手动下载示例

```bash
curl -G "http://<moviepilot-host>:5010/api/v1/plugin/SubtitleAgentBridge/download_subtitle" \
  --data-urlencode "apikey=<API_TOKEN>" \
  --data-urlencode "title=コントが始まる" \
  --data-urlencode "media_type=tv" \
  --data-urlencode "year=2021" \
  --data-urlencode "season=1" \
  --data-urlencode "episode=1" \
  --data-urlencode "languages=zh-cn,zh-tw" \
  --data-urlencode "target_file=/tmp/KontoGaHajimaru.S01E01.mkv"
```

## 异步触发示例（推荐）

适合网络慢或站点响应慢场景，接口会立即返回 `job_id`，避免客户端超时：

```bash
curl -G "http://<moviepilot-host>:5010/api/v1/plugin/SubtitleAgentBridge/download_subtitle_async" \
  --data-urlencode "apikey=<API_TOKEN>" \
  --data-urlencode "title=コントが始まる" \
  --data-urlencode "media_type=tv" \
  --data-urlencode "year=2021" \
  --data-urlencode "season=1" \
  --data-urlencode "episode=3" \
  --data-urlencode "languages=zh-cn,zh-tw" \
  --data-urlencode "target_file=/tmp/KontoGaHajimaru.S01E03.mkv"
```

查询任务状态：

```bash
curl -G "http://<moviepilot-host>:5010/api/v1/plugin/SubtitleAgentBridge/job_status" \
  --data-urlencode "apikey=<API_TOKEN>" \
  --data-urlencode "job_id=<JOB_ID>"
```

主动发送进度通知：

```bash
curl -G "http://<moviepilot-host>:5010/api/v1/plugin/SubtitleAgentBridge/notify_status" \
  --data-urlencode "apikey=<API_TOKEN>" \
  --data-urlencode "title=Subtitle Agent 进度" \
  --data-urlencode "text=当前正在扫描并测试验证码链路"
```

## 字母验证码接力

当 `subhd/subhdtw` 下载阶段遇到字母验证码时，插件会：

1. 通过 MoviePilot 通知推送验证码图片。
2. 给出一个短任务 ID，例如 `a1b2c3d4`。
3. 用户通过 Telegram / 企业微信等回复：

```text
/subcap a1b2c3d4 RhmE
```

插件收到后会继续调用 Subtitle Agent 完成验证码提交与字幕下载。

Telegram 侧可用命令：

- `/subcap 任务ID 验证码`
- `/subcap 任务ID refresh`（刷新验证码）
- `/substatus [任务ID]`

网页回填（v0.5.44+）：

- 打开通知里的 `网页回填` 链接，直接在网页输入验证码提交。
- 若验证码失效，可点击网页内 `刷新验证码` 按钮，无需在 TG 手输命令。

也可以直接走插件接口：

```bash
curl -G "http://<moviepilot-host>:5010/api/v1/plugin/SubtitleAgentBridge/submit_captcha" \
  --data-urlencode "apikey=<API_TOKEN>" \
  --data-urlencode "task_id=<TASK_ID>" \
  --data-urlencode "code=<CAPTCHA_CODE>"
```

## 手动通知策略

- 自动下载失败时，通知内容默认只保留 2 个推荐候选。
- 不再附带长篇原因和建议，减少消息噪音。
- 若是验证码场景，优先推送验证码图片与任务 ID，而不是一长串候选解释。

## 已入库批量补字幕示例

```bash
curl -G "http://<moviepilot-host>:5010/api/v1/plugin/SubtitleAgentBridge/backfill_directory" \
  --data-urlencode "apikey=<API_TOKEN>" \
  --data-urlencode "directory=/media" \
  --data-urlencode "include_paths=/media/tv,/media/movies" \
  --data-urlencode "exclude_paths=/media/downloads,/media/整理前,/media/刷流" \
  --data-urlencode "exclude_keywords=整理前,刷流,strm,stream,downloads,download,incoming,temp,cache" \
  --data-urlencode "title_aliases=短剧开始啦=コントが始まる" \
  --data-urlencode "recursive=true" \
  --data-urlencode "media_type=tv" \
  --data-urlencode "languages=zh-cn,zh-tw" \
  --data-urlencode "name_contains=短剧" \
  --data-urlencode "max_files=200"
```

## 版本说明（近期）

- `v0.5.59`：修复 `/sub scan` 任务可见性与冲突体验：查漏任务写入任务状态列表（`/sub status` 可见），锁冲突改为“等待后超时提示”，减少“提示冲突但无任务可查”的困惑。
- `v0.5.58`：修复定时补字幕重复命中同一批文件：新增 `periodic_overwrite`（定时任务独立覆盖开关，默认关闭），并引入扫描游标轮转（`start_offset/next_offset`）避免总是只扫前 200 个候选。
- `v0.5.57`：内封字幕跳过策略升级：新增 `embedded_subtitle_skip_mode`（`chinese/any/off`），默认改为“仅内封中文字幕才跳过”，降低日韩剧等场景误判。
- `v0.5.56`：增强 `/tmp` 等临时 `target_file` 的媒体路径修正：提高解析扫描预算，减少手动/API 触发时误走补字幕链路。
- `v0.5.55`：手动/API 下载链路新增跳过判定：当 `target_file` 可解析到媒体库真实文件时，同步应用中文内容/内封字幕/中文音轨跳过规则，减少无效补字幕通知。
- `v0.5.54`：修复 Telegram 命令栏消失问题：不再注册非法中文 BotCommand（保留手动输入 `/字幕` 兼容）。
- `v0.5.53`：修复 Plex 外挂字幕语言识别：字幕文件命名改为带语言后缀（如 `.zh.srt`），避免显示“未知”。
- `v0.5.52`：命令体系重构：统一主入口 `/sub`（`help/status/cap/scan`），并在状态回复中显示插件版本，便于确认强制更新是否生效。
- `v0.5.51`：命令收敛：统一解析链路，避免 Telegram 手工输入命令被不同事件通道漏处理。
- `v0.5.50`：新增查漏兜底命令：`/substatus scan [max] [keyword]`，规避部分环境下 `/subscan` 路由不生效问题。
- `v0.5.49`：修复 `/subscan` 在 UserMessage 通道不触发的问题，确保 Telegram 手动输入也能触发查漏任务。
- `v0.5.48`：新增 `/subscan` 稳定别名并回退验证码回填提示到 `/subcap`，提升 Telegram 兼容性。
- `v0.5.47`：新增中文命令 `/字幕`（状态/验证码/查漏），并支持从聊天直接触发查漏补字幕任务。
- `v0.5.46`：验证码通知优先展示网页回填链接；自动修正 `/tmp` 等临时 `target_file` 到媒体库真实文件路径；验证码场景不再重复发送“异步任务失败”通知。
- `v0.5.45`：修复验证码网页回填链路：网页链接自动携带 `apikey`，网页提交与刷新表单也保留 `apikey`，避免“apikey 校验不通过”。
- `v0.5.44`：新增验证码网页回填页（`/captcha_web`）与 `/subcap 任务ID refresh` 命令；通知中增加网页回填链接，降低 TG 手输时效失败。
- `v0.5.33`：优化验证码人工辅助交互：`subcap` 参数错误时主动返回格式提示；通知新增验证码详情页链接；回复文案改为“图中字母”避免误填占位词。
- `v0.5.32`：新增异步手动下载任务（`download_subtitle_async`/`job_status`）和状态通知接口（`notify_status`），避免长耗时触发接口超时，便于通过 MoviePilot/Telegram 跟踪进度。
- `v0.5.31`：新增 SubHD 字母验证码任务链路：通知推送验证码图片、支持回复 `subcap 任务ID 验证码` 继续下载；同时将手动下载通知精简为 2 个推荐候选。
- `v0.5.30`：补字幕结果通知新增成功明细（文件名、字幕名、来源），并将最近成功详情写入插件数据页便于复核。
- `v0.5.29`：优化日番同季内封字幕推断阈值，提高烧录字幕场景的漏判覆盖率。
- `v0.5.28`：新增日番同季内封字幕推断：当同季多数样本识别为内封字幕时，自动跳过该季其余漏判集。
- `v0.5.27`：新增中文纪录片自动跳过规则（`/tv/纪录片` + CJK 片名推断），减少中文纪录片误报缺字幕。
- `v0.5.26`：目录补字幕 API 新增 `detail_limit` 与 `scanned` 字段，支持更完整的 `dry_run` 审计输出。
- `v0.5.25`：修复 Season 与 CJK 正则转义错误，恢复未分类中文剧集兜底识别，减少国产剧误报缺字幕。
- `v0.5.24`：新增未分类中文剧集兜底跳过规则：仅对 `/tv/剧名/Season` 结构且无外语目录标记的中文剧生效。
- `v0.5.23`：新增 NFO 兜底识别中文内容：按国家/原始语言自动跳过中文音轨媒体，减少无效补字幕任务。
- `v0.5.22`：强化剧集字幕识别：新增 `SxxEyy` 同集精确匹配，避免跨集字幕被误判为已覆盖。
- `v0.5.21`：`dry_run` 返回 `missing_files` 数量上限提升，便于完整排查误报。
- `v0.5.20`：新增 `debug_subtitle_presence` 调试接口，并进一步加固电影目录共享字幕判定。
- `v0.5.19`：增强电影目录字幕复用判定：同目录多分辨率视频可复用同片名字幕，减少误报缺字幕。
- `v0.5.18`：修复同目录字幕识别：忽略 `chi/zh-cn` 等语言后缀后再做同片名匹配，避免误报缺字幕。
- `v0.5.17`：新增“手动跳过媒体关键词”配置，可按片名/路径关键词显式跳过硬字幕或不需补字幕的媒体。
- `v0.5.16`：`dry_run` 返回新增 `skipped_files`（含跳过原因），便于定位“为什么被跳过/未跳过”。
- `v0.5.15`：补充外挂字幕存在判定：同目录同片名（忽略分辨率/年份差异）的字幕文件也视为已覆盖，避免误判缺字幕。
- `v0.5.14`：进一步增强内封字幕识别：在命令行探测外新增文件签名兜底，修复个别媒体流信息缺失导致的漏判。
- `v0.5.13`：修复跳过判定：增强国产/华语目录识别；新增 `ffprobe/mediainfo/ffmpeg + 文件签名` 多后端探测内封字幕与中文音轨。
- `v0.5.12`：扫描/入库补字幕新增智能跳过：国产/华语目录、内封字幕媒体、含中文音轨媒体默认不下载字幕。
- `v0.5.11`：目录补字幕接口新增 `dry_run` 参数：只扫描缺字幕文件不触发下载，快速输出缺字幕清单，便于先盘点再测试。
- `v0.5.0` - `v0.5.10`：完成目录回填、自动校时、定时补字幕、手动下载通知、多标题回退搜索与路径排除等基础能力建设。
