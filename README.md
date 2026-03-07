# SubtitleAgentBridge（MoviePilot 插件）

`SubtitleAgentBridge` 是用于对接 `moviepilot-subtitle-agent` 的 MoviePilot 插件。

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
- `/api/v1/plugin/SubtitleAgentBridge/backfill_directory`

必填参数：

- `apikey`（MoviePilot 的 API_TOKEN）

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

- `v0.5.5`：只要存在候选但自动下载失败（不限验证码场景），统一通过 MoviePilot 通知推送候选链接供手动下载。
- `v0.5.4`：当候选字幕因验证码等原因无法自动下载时，自动通过 MoviePilot 通知推送候选下载链接供手动处理。
- `v0.5.3`：适配 Subtitle Agent `v0.1.5` 多源分层检索（`assrt/subhd -> podnapisi/tvsubtitles -> opensubtitles`）。
- `v0.5.2`：新增自动字幕时间轴校正（参考同目录已有字幕，自动判断并平移）。
- `v0.5.1`：新增标题别名映射（`title_aliases`）与泛化标题过滤，降低剧集误匹配字幕风险。
- `v0.5.0`：新增仅扫描目录白名单（`include_paths`）与默认 `downloads` 排除，避免字幕写入未整理目录。
- `v0.4.0`：支持排除目录和关键词。
- `v0.3.x`：支持目录回填、关键词过滤和标题回退搜索。
