# SubtitleAgentBridge (MoviePilot Plugin)

MoviePilot plugin for integrating an external Subtitle Agent service.

## Repository Layout

- `plugins.v2/subtitleagentbridge/__init__.py`

## What It Does

- Listens to MoviePilot `TransferComplete` events.
- Calls Subtitle Agent MoviePilot-compatible APIs to search/download subtitles.
- Writes subtitle files next to media files.

## Plugin Config (recommended)

- `host`: `http://host.docker.internal:8178` (if MoviePilot runs in Docker)
- `search_path`: `/api/v1/moviepilot/subtitles/search`
- `languages`: `zh-cn,zh-tw`
- `limit`: `5`
- `timeout`: `60`

## Install in MoviePilot

1. Put this repository where MoviePilot can read/install plugins from GitHub.
2. Ensure plugin directory is `plugins.v2/subtitleagentbridge`.
3. Restart MoviePilot and enable `Subtitle Agent Bridge`.

## Manual API Trigger (plugin endpoint)

- `/api/v1/plugin/SubtitleAgentBridge/download_subtitle`
- `/api/v1/plugin/SubtitleAgentBridge/backfill_directory`

Required param:

- `apikey` (MoviePilot API token)

### Backfill existing library subtitles

Use `backfill_directory` to scan an existing media folder and download subtitles for files that do not already have subtitle files.

Example:

```bash
curl -G "http://<moviepilot-host>:5010/api/v1/plugin/SubtitleAgentBridge/backfill_directory" \
  --data-urlencode "apikey=<API_TOKEN>" \
  --data-urlencode "directory=/media/tv/Marshals" \
  --data-urlencode "recursive=true" \
  --data-urlencode "media_type=tv" \
  --data-urlencode "languages=zh-cn,zh-tw" \
  --data-urlencode "name_contains=Marshals" \
  --data-urlencode "max_files=200"
```
