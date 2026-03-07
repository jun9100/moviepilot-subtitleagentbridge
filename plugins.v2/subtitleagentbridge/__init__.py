import json
import re
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType


class SubtitleAgentBridge(_PluginBase):
    """
    MoviePilot 插件示例：对接外部 Subtitle Agent，自动为入库媒体下载字幕。
    """

    plugin_name = "Subtitle Agent Bridge"
    plugin_desc = "调用外部 MoviePilot Subtitle Agent 自动检索并下载字幕。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.5.13"
    plugin_author = "jun9100"
    author_url = "https://github.com/jun9100/moviepilot-subtitleagentbridge"
    plugin_config_prefix = "subtitleagentbridge_"
    plugin_order = 50
    auth_level = 1

    _enabled: bool = False
    _host: str = ""
    _search_path: str = "/api/v1/moviepilot/subtitles/search"
    _languages: str = "zh-cn,zh-tw"
    _limit: int = 5
    _timeout: int = 60
    _overwrite: bool = False
    _notify: bool = True
    _include_paths: str = ""
    _exclude_paths: str = ""
    _exclude_keywords: str = "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache"
    _title_aliases: str = ""
    _auto_timing_sync: bool = True
    _auto_timing_max_offset_seconds: int = 120
    _manual_notice_cache: set[str] = set()
    _periodic_enabled: bool = False
    _periodic_mode: str = "interval"
    _periodic_interval_hours: int = 24
    _periodic_daily_time: str = "03:30"
    _periodic_max_files: int = 200
    _periodic_recursive: bool = True
    _periodic_thread: Optional[threading.Thread] = None
    _periodic_stop_event: Optional[threading.Event] = None
    _periodic_run_lock = threading.Lock()
    _media_probe_cache: Dict[str, Dict[str, Any]] = {}

    _subtitle_suffixes = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
    _chinese_audio_lang_aliases = {
        "zh",
        "zho",
        "chi",
        "chs",
        "cht",
        "cmn",
        "yue",
        "zh-cn",
        "zh-tw",
        "zh-hans",
        "zh-hant",
    }
    _chinese_audio_title_markers = {"中文", "国语", "普通话", "mandarin", "chinese", "cantonese", "粤语"}
    _chinese_library_markers = {
        "/国产剧/",
        "/国产电影/",
        "/华语电影/",
        "/华语剧/",
        "/国语/",
        "/大陆剧/",
        "/中国/",
        "/国漫/",
    }
    _chinese_library_keywords = {
        "国产剧",
        "国产电影",
        "华语",
        "国语",
        "大陆剧",
        "内地剧",
        "中国",
        "国漫",
        "央视",
        "cctv",
        "中配",
        "国配",
    }
    _probe_command_timeout_seconds = 15
    _season_episode_pattern = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
    _year_pattern = re.compile(r"\b(19\d{2}|20\d{2})\b")

    def init_plugin(self, config: dict = None):
        self.__stop_periodic_worker()
        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._host = self.__normalize_host(config.get("host"))
        self._search_path = str(config.get("search_path") or "/api/v1/moviepilot/subtitles/search")
        self._languages = str(config.get("languages") or "zh-cn,zh-tw")
        self._limit = int(config.get("limit") or 5)
        # Some providers may respond slowly in NAS environments; keep a safe minimum timeout.
        self._timeout = max(int(config.get("timeout") or 60), 60)
        self._overwrite = bool(config.get("overwrite"))
        self._notify = bool(config.get("notify", True))
        self._include_paths = str(config.get("include_paths") or "")
        self._exclude_paths = str(config.get("exclude_paths") or "")
        self._exclude_keywords = str(
            config.get("exclude_keywords") or "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache"
        )
        self._title_aliases = str(config.get("title_aliases") or "")
        self._auto_timing_sync = self.__to_bool(config.get("auto_timing_sync"), default=True)
        max_offset = self.__to_int(config.get("auto_timing_max_offset_seconds"))
        self._auto_timing_max_offset_seconds = max(10, min(max_offset or 120, 600))
        self._periodic_enabled = self.__to_bool(config.get("periodic_enabled"), default=False)
        self._periodic_mode = self.__normalize_periodic_mode(config.get("periodic_mode"))
        periodic_hours = self.__to_int(config.get("periodic_interval_hours"))
        self._periodic_interval_hours = max(1, min(periodic_hours or 24, 168))
        self._periodic_daily_time = self.__normalize_daily_time(config.get("periodic_daily_time"))
        periodic_max_files = self.__to_int(config.get("periodic_max_files"))
        self._periodic_max_files = max(1, min(periodic_max_files or 200, 2000))
        self._periodic_recursive = self.__to_bool(config.get("periodic_recursive"), default=True)
        self._manual_notice_cache = set()
        self._media_probe_cache = {}
        self.__start_periodic_worker()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/download_subtitle",
                "endpoint": self.download_subtitle,
                "methods": ["GET"],
                "summary": "手动下载字幕",
                "description": "按标题信息调用 Subtitle Agent 搜索并下载字幕。",
            },
            {
                "path": "/backfill_directory",
                "endpoint": self.backfill_directory,
                "methods": ["GET"],
                "summary": "补齐目录字幕",
                "description": "扫描目录中缺失字幕的视频文件并批量下载字幕。支持仅扫描刮削后目录，并排除整理前/刷流目录。",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "overwrite", "label": "覆盖已有字幕"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送结果通知"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "periodic_enabled", "label": "启用定期自动补字幕"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "periodic_mode",
                                            "label": "执行方式",
                                            "items": [
                                                {"title": "每隔 N 小时（推荐）", "value": "interval"},
                                                {"title": "每天固定时间", "value": "daily"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "periodic_interval_hours",
                                            "label": "循环间隔(小时)",
                                            "type": "number",
                                            "placeholder": "24",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "periodic_daily_time",
                                            "label": "每日执行时间(HH:MM)",
                                            "placeholder": "03:30",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "periodic_max_files",
                                            "label": "单次最多扫描文件数",
                                            "type": "number",
                                            "placeholder": "200",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "periodic_recursive", "label": "扫描子目录(递归)"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "定期任务使用说明：执行方式选“每隔 N 小时”时只看“循环间隔”；选“每天固定时间”时只看“每日执行时间”。其余配置建议保持默认即可。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "auto_timing_sync", "label": "自动校正字幕时间轴"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "auto_timing_max_offset_seconds",
                                            "label": "自动校时最大偏移(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "include_paths",
                                            "label": "仅扫描目录(刮削后)",
                                            "placeholder": "/media/library/tv,/media/library/movie",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_paths",
                                            "label": "回填排除目录",
                                            "placeholder": "/media/downloads,/media/整理前,/media/刷流",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "title_aliases",
                                            "label": "标题别名映射",
                                            "placeholder": "短剧开始啦=コントが始まる; 黄石：法警小队=Marshals",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_keywords",
                                            "label": "回填排除关键词",
                                            "placeholder": "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "host",
                                            "label": "Subtitle Agent 地址",
                                            "placeholder": "http://127.0.0.1:8178",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "search_path",
                                            "label": "搜索接口路径",
                                            "placeholder": "/api/v1/moviepilot/subtitles/search",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "languages",
                                            "label": "字幕语言",
                                            "placeholder": "zh-cn,zh-tw",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "limit",
                                            "label": "单文件候选数量",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout",
                                            "label": "HTTP超时(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "插件监听 transfer.complete 事件。建议配置“仅扫描目录”为刮削后媒体库路径，避免字幕写入下载/未整理目录。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "host": "http://127.0.0.1:8178",
            "search_path": "/api/v1/moviepilot/subtitles/search",
            "languages": "zh-cn,zh-tw",
            "limit": 5,
            "timeout": 60,
            "overwrite": False,
            "notify": True,
            "include_paths": "",
            "exclude_paths": "",
            "exclude_keywords": "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache",
            "title_aliases": "",
            "auto_timing_sync": True,
            "auto_timing_max_offset_seconds": 120,
            "periodic_enabled": False,
            "periodic_mode": "interval",
            "periodic_interval_hours": 24,
            "periodic_daily_time": "03:30",
            "periodic_max_files": 200,
            "periodic_recursive": True,
        }

    def get_page(self) -> List[dict]:
        last_result = self.get_data("last_result") or {}

        lines = [
            f"最近执行时间: {last_result.get('time') or '-'}",
            f"处理文件数: {last_result.get('total') or 0}",
            f"成功数: {last_result.get('success') or 0}",
            f"跳过数: {last_result.get('skipped') or 0}",
            f"排除数: {last_result.get('excluded') or 0}",
            f"失败数: {last_result.get('failed') or 0}",
        ]
        errors = last_result.get("errors") or []
        if errors:
            lines.append("失败详情:")
            lines.extend(errors[:10])

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "\n".join(lines),
                                },
                            }
                        ],
                    }
                ],
            }
        ]

    def stop_service(self):
        self.__stop_periodic_worker()

    def __start_periodic_worker(self) -> None:
        if not self._enabled or not self._periodic_enabled:
            return
        if not self._host:
            logger.warn("[SubtitleAgentBridge] 定期补字幕已启用但未配置 Subtitle Agent 地址，已跳过启动")
            return
        include_paths = self.__merge_csv_values(self._include_paths)
        if not include_paths:
            logger.warn("[SubtitleAgentBridge] 定期补字幕已启用但未配置仅扫描目录，已跳过启动")
            return

        stop_event = threading.Event()
        thread = threading.Thread(
            target=self.__periodic_worker_loop,
            args=(stop_event,),
            name="SubtitleAgentBridgePeriodic",
            daemon=True,
        )
        self._periodic_stop_event = stop_event
        self._periodic_thread = thread
        thread.start()
        mode = self.__normalize_periodic_mode(self._periodic_mode)
        if mode == "daily":
            logger.info(
                f"[SubtitleAgentBridge] 定期补字幕任务已启动: 每日 {self._periodic_daily_time} 执行，"
                f"单次最多扫描 {self._periodic_max_files} 个文件"
            )
        else:
            logger.info(
                f"[SubtitleAgentBridge] 定期补字幕任务已启动: 每 {self._periodic_interval_hours} 小时执行一次，"
                f"单次最多扫描 {self._periodic_max_files} 个文件"
            )

    def __stop_periodic_worker(self) -> None:
        stop_event = self._periodic_stop_event
        thread = self._periodic_thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._periodic_stop_event = None
        self._periodic_thread = None

    def __periodic_worker_loop(self, stop_event: threading.Event) -> None:
        mode = self.__normalize_periodic_mode(self._periodic_mode)
        if mode == "daily":
            while not stop_event.is_set():
                wait_seconds = self.__seconds_until_daily_run(self._periodic_daily_time)
                if stop_event.wait(wait_seconds):
                    break
                self.__run_periodic_backfill_once()
            return

        interval_seconds = max(3600, int(self._periodic_interval_hours) * 3600)
        while not stop_event.is_set():
            self.__run_periodic_backfill_once()
            if stop_event.wait(interval_seconds):
                break

    def __run_periodic_backfill_once(self) -> None:
        if not self._enabled or not self._periodic_enabled:
            return
        if not self._host:
            return

        include_paths = self.__merge_csv_values(self._include_paths)
        if not include_paths:
            return

        if not self._periodic_run_lock.acquire(blocking=False):
            logger.info("[SubtitleAgentBridge] 定期补字幕任务仍在执行中，跳过本轮")
            return

        try:
            response = self.backfill_directory(
                apikey=settings.API_TOKEN,
                directory=include_paths[0],
                recursive=self._periodic_recursive,
                media_type="",
                languages=self._languages,
                name_contains="",
                include_paths="",
                exclude_paths="",
                exclude_keywords="",
                title_aliases="",
                overwrite=self._overwrite,
                max_files=self._periodic_max_files,
                limit=self._limit,
            )
            success = bool(getattr(response, "success", False))
            message = str(getattr(response, "message", ""))
            if success:
                logger.info(f"[SubtitleAgentBridge] 定期补字幕完成: {message}")
            else:
                logger.warn(f"[SubtitleAgentBridge] 定期补字幕失败: {message}")
        except Exception as err:
            logger.error(f"[SubtitleAgentBridge] 定期补字幕异常: {str(err)}")
        finally:
            self._periodic_run_lock.release()

    @eventmanager.register(EventType.TransferComplete)
    def download_on_transfer_complete(self, event: Event):
        if not self._enabled:
            return
        if not self._host:
            logger.warn("[SubtitleAgentBridge] 未配置 Subtitle Agent 地址")
            return

        event_data = event.event_data or {}
        mediainfo = event_data.get("mediainfo")
        transferinfo = event_data.get("transferinfo")
        meta = event_data.get("meta")

        if not mediainfo or not transferinfo:
            return

        file_list = getattr(transferinfo, "file_list_new", None) or []
        if not file_list:
            return

        total = 0
        success = 0
        skipped = 0
        errors = []
        include_paths = self.__merge_csv_values(self._include_paths)
        excluded_paths = self.__merge_csv_values(self._exclude_paths)
        excluded_keywords = self.__merge_csv_values(self._exclude_keywords)

        for media_file in file_list:
            if not self.__is_video_file(media_file):
                continue
            media_path = Path(media_file)
            if not self.__is_included_path(media_path, include_paths):
                logger.info(f"[SubtitleAgentBridge] 跳过非库目录文件: {media_file}")
                continue
            if self.__is_excluded_path(media_path, excluded_paths, excluded_keywords):
                logger.info(f"[SubtitleAgentBridge] 跳过排除目录文件: {media_file}")
                continue
            parsed_context = self.__parse_media_context_from_file(
                media_path,
                forced_media_type="series" if getattr(mediainfo, "type", None) == MediaType.TV else "movie",
            )
            skip_reason = self.__skip_reason_for_media(media_path, parsed_context)
            if skip_reason:
                skipped += 1
                logger.info(f"[SubtitleAgentBridge] 跳过无需补字幕文件: {media_file} ({skip_reason})")
                continue
            total += 1
            state, message = self.__download_for_media_file(
                media_file=media_file,
                mediainfo=mediainfo,
                meta=meta,
            )
            if state:
                success += 1
            else:
                errors.append(f"{Path(media_file).name}: {message}")

        if total == 0:
            return

        failed = max(total - success, 0)
        result = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": total,
            "success": success,
            "skipped": skipped,
            "failed": failed,
            "errors": errors,
        }
        self.save_data("last_result", result)

        if self._notify:
            text = f"处理 {total} 个视频，成功 {success} 个，跳过 {skipped} 个，失败 {failed} 个"
            if errors:
                text = f"{text}\n" + "\n".join(errors[:5])
            self.post_message(
                mtype=NotificationType.Plugin,
                title="Subtitle Agent 字幕下载结果",
                text=text,
            )

    def download_subtitle(
        self,
        title: str,
        apikey: str,
        media_type: str = "movie",
        year: int = None,
        season: int = None,
        episode: int = None,
        target_file: str = "",
        languages: str = "",
    ) -> schemas.Response:
        """
        手动触发字幕下载，可由插件API调用：
        /api/v1/plugin/SubtitleAgentBridge/download_subtitle
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        payload = {
            "title": title,
            "type": "series" if media_type in ["tv", "series", "show"] else "movie",
            "year": year,
            "season": season,
            "episode": episode,
            "language": languages or self._languages,
            "limit": self._limit,
        }

        items, message = self.__search_items(payload)
        if not items:
            return schemas.Response(
                success=False,
                message=self.__normalize_failure_message(message, "未找到可用字幕"),
            )

        selected = self.__pick_item(
            items,
            preferred_languages=self.__split_languages(languages or self._languages),
        )
        content, subtitle_format, message = self.__download_item(selected)
        if not content:
            failure_message = self.__normalize_failure_message(message, "下载字幕失败")
            self.__maybe_notify_manual_download(
                media_name=self.__format_media_name(
                    title=title,
                    media_type=media_type,
                    year=year,
                    season=season,
                    episode=episode,
                ),
                failure_message=failure_message,
                items=items,
                target_file=target_file,
                preferred_languages=self.__split_languages(languages or self._languages),
            )
            return schemas.Response(success=False, message=failure_message)

        if not target_file:
            return schemas.Response(
                success=True,
                message="字幕下载成功（未写入文件，因 target_file 为空）",
                data={
                    "provider": selected.get("provider"),
                    "subtitle_id": selected.get("subtitle_id"),
                    "size": len(content),
                },
            )

        subtitle_path = self.__build_subtitle_path(target_file, subtitle_format)
        sync_note = ""
        try:
            subtitle_file = Path(subtitle_path)
            subtitle_file.parent.mkdir(parents=True, exist_ok=True)
            content, sync_note = self.__maybe_auto_sync_timing(
                content=content,
                subtitle_format=subtitle_format,
                media_file=Path(target_file),
                subtitle_file=subtitle_file,
            )
            subtitle_file.write_bytes(content)
        except Exception as err:
            return schemas.Response(success=False, message=f"写入字幕失败: {str(err)}")

        message = f"字幕下载完成: {subtitle_path}"
        if sync_note:
            message = f"{message}（{sync_note}）"

        return schemas.Response(
            success=True,
            message=message,
            data={
                "path": subtitle_path,
                "provider": selected.get("provider"),
                "subtitle_id": selected.get("subtitle_id"),
                "size": len(content),
                "sync": sync_note,
            },
        )

    def backfill_directory(
        self,
        apikey: str,
        directory: str,
        recursive: bool = True,
        media_type: str = "",
        languages: str = "",
        name_contains: str = "",
        include_paths: str = "",
        exclude_paths: str = "",
        exclude_keywords: str = "",
        title_aliases: str = "",
        overwrite: bool = False,
        max_files: int = 200,
        limit: int = 0,
        dry_run: bool = False,
    ) -> schemas.Response:
        """
        扫描目录里没有字幕的视频文件并批量下载字幕。
        API:
        /api/v1/plugin/SubtitleAgentBridge/backfill_directory
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        if not self._host:
            return schemas.Response(success=False, message="未配置 Subtitle Agent 地址")
        if not directory:
            return schemas.Response(success=False, message="directory 参数不能为空")

        recursive_flag = self.__to_bool(recursive, default=True)
        overwrite_flag = self.__to_bool(overwrite, default=self._overwrite)
        desired_type = self.__normalize_media_type(media_type)
        preferred_languages = self.__split_languages(languages or self._languages)
        selected_languages = ",".join(preferred_languages) if preferred_languages else self._languages
        effective_limit = self.__to_int(limit) or self._limit
        if effective_limit <= 0:
            effective_limit = self._limit
        dry_run_flag = self.__to_bool(dry_run, default=False)

        max_file_count = self.__to_int(max_files) or 200
        if max_file_count <= 0:
            max_file_count = 200
        name_keyword = str(name_contains or "").strip()
        name_filter = name_keyword.lower()
        included_paths = self.__merge_csv_values(self._include_paths, include_paths)
        excluded_paths = self.__merge_csv_values(self._exclude_paths, exclude_paths)
        excluded_keywords = self.__merge_csv_values(self._exclude_keywords, exclude_keywords)
        alias_map = self.__merge_title_aliases(self._title_aliases, title_aliases)
        scan_roots = self.__collect_scan_roots(directory=directory, include_paths=included_paths)
        if not scan_roots:
            return schemas.Response(success=False, message="没有可扫描目录，请检查 directory/include_paths 配置")

        processed = 0
        success = 0
        skipped = 0
        excluded = 0
        failed = 0
        errors: List[str] = []
        downloaded: List[Dict[str, Any]] = []
        missing_files: List[Dict[str, Any]] = []

        matched = 0
        seen_identities = set()
        try:
            for scan_root in scan_roots:
                file_iter = self.__iter_video_files(scan_root, recursive=recursive_flag)
                for video_file in file_iter:
                    identity = self.__file_identity(video_file)
                    if identity and identity in seen_identities:
                        continue
                    if identity:
                        seen_identities.add(identity)

                    if self.__is_excluded_path(
                        media_file=video_file,
                        excluded_paths=excluded_paths,
                        excluded_keywords=excluded_keywords,
                    ):
                        excluded += 1
                        continue

                    if name_filter and name_filter not in video_file.name.lower():
                        continue

                    matched += 1
                    if matched > max_file_count:
                        break

                    processed += 1

                    if not overwrite_flag and self.__has_subtitle(video_file):
                        skipped += 1
                        continue

                    parsed = self.__parse_media_context_from_file(video_file, forced_media_type=desired_type)
                    skip_reason = self.__skip_reason_for_media(video_file, parsed)
                    if skip_reason:
                        skipped += 1
                        continue
                    if dry_run_flag:
                        missing_files.append(
                            {
                                "video": str(video_file),
                                "title": parsed.get("title") or video_file.stem,
                                "type": parsed.get("type"),
                                "year": parsed.get("year"),
                                "season": parsed.get("season"),
                                "episode": parsed.get("episode"),
                            }
                        )
                        continue

                    if not parsed.get("title"):
                        failed += 1
                        errors.append(f"{video_file.name}: 无法从文件名解析标题")
                        continue

                    payload = {
                        "title": parsed.get("title"),
                        "type": parsed.get("type"),
                        "year": parsed.get("year"),
                        "season": parsed.get("season"),
                        "episode": parsed.get("episode"),
                        "language": selected_languages,
                        "limit": effective_limit,
                    }

                    selected_title = payload.get("title")
                    items = []
                    message = None
                    for title_candidate in self.__build_title_candidates(
                        parsed_title=parsed.get("title"),
                        media_file=video_file,
                        name_keyword=name_keyword,
                        alias_map=alias_map,
                    ):
                        payload["title"] = title_candidate
                        items, message = self.__search_items(payload)
                        if items:
                            selected_title = title_candidate
                            break

                    if not items:
                        failed += 1
                        errors.append(
                            f"{video_file.name}: {self.__normalize_failure_message(message, '未找到字幕')}"
                        )
                        continue

                    selected = self.__pick_item(items, preferred_languages=preferred_languages)
                    content, subtitle_format, message = self.__download_item(selected)
                    if not content:
                        failure_message = self.__normalize_failure_message(message, "下载字幕失败")
                        self.__maybe_notify_manual_download(
                            media_name=self.__format_media_name(
                                title=str(selected_title or parsed.get("title") or video_file.stem),
                                media_type=str(parsed.get("type") or ""),
                                year=self.__to_int(parsed.get("year")),
                                season=self.__to_int(parsed.get("season")),
                                episode=self.__to_int(parsed.get("episode")),
                            ),
                            failure_message=failure_message,
                            items=items,
                            target_file=str(video_file),
                            preferred_languages=preferred_languages,
                        )
                        failed += 1
                        errors.append(
                            f"{video_file.name}: {failure_message}"
                        )
                        continue

                    subtitle_path = Path(self.__build_subtitle_path(str(video_file), subtitle_format))
                    if subtitle_path.exists() and not overwrite_flag:
                        skipped += 1
                        continue

                    try:
                        subtitle_path.parent.mkdir(parents=True, exist_ok=True)
                        content, sync_note = self.__maybe_auto_sync_timing(
                            content=content,
                            subtitle_format=subtitle_format,
                            media_file=video_file,
                            subtitle_file=subtitle_path,
                        )
                        subtitle_path.write_bytes(content)
                    except Exception as err:
                        failed += 1
                        errors.append(f"{video_file.name}: 写入字幕失败: {str(err)}")
                        continue

                    success += 1
                    downloaded.append(
                        {
                            "video": str(video_file),
                            "subtitle": str(subtitle_path),
                            "title": selected_title,
                            "provider": selected.get("provider"),
                            "language": selected.get("language"),
                            "sync": sync_note,
                        }
                    )
                    if sync_note:
                        logger.info(f"[SubtitleAgentBridge] 批量补字幕成功: {subtitle_path} ({sync_note})")
                    else:
                        logger.info(f"[SubtitleAgentBridge] 批量补字幕成功: {subtitle_path}")

                if matched > max_file_count:
                    break
        except Exception as err:
            return schemas.Response(success=False, message=f"扫描目录失败: {str(err)}")

        if processed == 0:
            if name_filter:
                return schemas.Response(success=False, message=f"目录中未找到匹配关键词的视频文件: {name_filter}")
            return schemas.Response(success=False, message="目录中未找到视频文件")

        result = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": processed,
            "success": success,
            "skipped": skipped,
            "excluded": excluded,
            "failed": failed,
            "dry_run": dry_run_flag,
            "missing": len(missing_files),
            "errors": errors,
        }
        self.save_data("last_result", result)

        if self._notify:
            if dry_run_flag:
                text = (
                    f"缺字幕扫描完成，共扫描 {processed} 个视频，"
                    f"缺字幕 {len(missing_files)} 个，跳过 {skipped} 个，排除 {excluded} 个"
                )
            else:
                text = (
                    f"补字幕完成，共扫描 {processed} 个视频，成功 {success} 个，"
                    f"跳过 {skipped} 个，排除 {excluded} 个，失败 {failed} 个"
                )
            if errors:
                text = f"{text}\n" + "\n".join(errors[:5])
            self.post_message(
                mtype=NotificationType.Plugin,
                title="Subtitle Agent 扫描结果" if dry_run_flag else "Subtitle Agent 补字幕结果",
                text=text,
            )

        if dry_run_flag:
            message = (
                f"扫描 {processed} 个视频，缺字幕 {len(missing_files)}，"
                f"跳过 {skipped}，排除 {excluded}"
            )
        else:
            message = f"扫描 {processed} 个视频，成功 {success}，跳过 {skipped}，排除 {excluded}，失败 {failed}"
        return schemas.Response(
            success=failed == 0,
            message=message,
            data={
                "directory": directory,
                "scan_roots": [str(path) for path in scan_roots],
                "recursive": recursive_flag,
                "overwrite": overwrite_flag,
                "dry_run": dry_run_flag,
                "name_contains": name_filter,
                "include_paths": included_paths,
                "exclude_paths": excluded_paths,
                "exclude_keywords": excluded_keywords,
                "languages": selected_languages,
                "total": processed,
                "success": success,
                "skipped": skipped,
                "excluded": excluded,
                "failed": failed,
                "missing": len(missing_files),
                "missing_files": missing_files[:200],
                "items": downloaded[:50],
                "errors": errors[:50],
            },
        )

    def __download_for_media_file(self, media_file: str, mediainfo: Any, meta: Any) -> Tuple[bool, str]:
        payload = self.__build_search_payload(mediainfo=mediainfo, meta=meta)
        items, message = self.__search_items(payload)
        if not items:
            return False, self.__normalize_failure_message(message, "未找到字幕")

        selected = self.__pick_item(items)
        content, subtitle_format, message = self.__download_item(selected)
        if not content:
            failure_message = self.__normalize_failure_message(message, "下载失败")
            self.__maybe_notify_manual_download(
                media_name=self.__format_media_name(
                    title=str(payload.get("title") or Path(media_file).stem),
                    media_type=str(payload.get("type") or ""),
                    year=self.__to_int(payload.get("year")),
                    season=self.__to_int(payload.get("season")),
                    episode=self.__to_int(payload.get("episode")),
                ),
                failure_message=failure_message,
                items=items,
                target_file=media_file,
                preferred_languages=self.__split_languages(self._languages),
            )
            return False, failure_message

        subtitle_path = self.__build_subtitle_path(media_file, subtitle_format)
        subtitle_file = Path(subtitle_path)

        if subtitle_file.exists() and not self._overwrite:
            return False, "字幕已存在且未启用覆盖"

        try:
            content, sync_note = self.__maybe_auto_sync_timing(
                content=content,
                subtitle_format=subtitle_format,
                media_file=Path(media_file),
                subtitle_file=subtitle_file,
            )
            subtitle_file.write_bytes(content)
        except Exception as err:
            return False, f"写入字幕失败: {str(err)}"

        if sync_note:
            logger.info(f"[SubtitleAgentBridge] 字幕下载完成: {subtitle_file} ({sync_note})")
            return True, f"{subtitle_path} ({sync_note})"

        logger.info(f"[SubtitleAgentBridge] 字幕下载完成: {subtitle_file}")
        return True, subtitle_path

    def __build_search_payload(self, mediainfo: Any, meta: Any) -> Dict[str, Any]:
        media_type = "movie"
        if getattr(mediainfo, "type", None) == MediaType.TV:
            media_type = "series"

        season = getattr(mediainfo, "season", None)
        episode = getattr(meta, "begin_episode", None) if meta else None
        if not season and meta:
            season = getattr(meta, "begin_season", None)

        payload = {
            "title": getattr(mediainfo, "title", None) or getattr(mediainfo, "en_title", None),
            "type": media_type,
            "year": self.__to_int(getattr(mediainfo, "year", None)),
            "season": self.__to_int(season),
            "episode": self.__to_int(episode),
            "imdbid": getattr(mediainfo, "imdb_id", None),
            "tmdbid": getattr(mediainfo, "tmdb_id", None),
            "language": self._languages,
            "limit": self._limit,
        }
        return payload

    def __search_items(self, payload: Dict[str, Any]) -> Tuple[List[dict], Optional[str]]:
        search_url = self.__compose_url(self._search_path)
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url=search_url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self._timeout) as res:
                status_code = int(getattr(res, "status", 200))
                content = res.read()
        except Exception as err:
            return [], f"请求搜索接口失败: {str(err)}"

        if status_code != 200:
            return [], f"搜索接口返回错误: {status_code}"

        try:
            body = json.loads(content.decode("utf-8", errors="ignore"))
        except Exception as err:
            return [], f"解析搜索结果失败: {str(err)}"

        # 兼容 MoviePilot 包装格式
        if isinstance(body, dict) and "success" in body:
            if body.get("success") is True and isinstance(body.get("data"), dict):
                items = body.get("data", {}).get("items") or []
                item_list = items if isinstance(items, list) else []
                if item_list:
                    return item_list, body.get("message")
                return [], str(body.get("message") or "未找到字幕")
            return [], str(body.get("message") or "字幕检索失败")

        # 兼容标准格式
        items = body.get("items") if isinstance(body, dict) else []
        return items if isinstance(items, list) else [], None

    def __pick_item(self, items: List[dict], preferred_languages: Optional[List[str]] = None) -> dict:
        preferred_langs = preferred_languages or self.__split_languages(self._languages)
        for lang in preferred_langs:
            for item in items:
                if str(item.get("language") or "").lower().startswith(lang.lower()):
                    return item
        return items[0]

    def __download_item(self, item: dict) -> Tuple[Optional[bytes], str, Optional[str]]:
        download_url = item.get("download_url")
        if not download_url:
            return None, "srt", "候选字幕缺少下载地址"

        full_url = self.__compose_url(download_url)
        try:
            request = Request(
                url=full_url,
                headers={"Accept": "*/*"},
                method="GET",
            )
            with urlopen(request, timeout=self._timeout) as res:
                status_code = int(getattr(res, "status", 200))
                content_type = str(res.headers.get("Content-Type") or "").lower()
                content = res.read()
        except Exception as err:
            return None, "srt", f"请求下载接口失败: {str(err)}"

        if status_code != 200:
            return None, "srt", f"下载接口返回错误: {status_code}"

        if "application/json" in content_type:
            try:
                body = json.loads(content.decode("utf-8", errors="ignore"))
            except Exception:
                body = None
            if isinstance(body, dict) and "success" in body and body.get("success") is not True:
                return None, "srt", str(body.get("message") or "字幕下载失败")

        if not content:
            return None, "srt", "下载内容为空"

        subtitle_format = str(item.get("format") or item.get("subtitle_format") or "srt").lower()
        return content, subtitle_format, None

    def __maybe_auto_sync_timing(
        self,
        *,
        content: bytes,
        subtitle_format: str,
        media_file: Path,
        subtitle_file: Path,
    ) -> Tuple[bytes, str]:
        if not self._auto_timing_sync:
            return content, ""

        fmt = str(subtitle_format or "").strip().lower()
        if fmt not in {"srt", "ass", "ssa", "vtt"}:
            return content, ""
        if not media_file.exists() or not media_file.is_file():
            return content, ""

        source_text = self.__decode_subtitle_text(content)
        if not source_text:
            return content, ""

        source_times = self.__extract_cue_times(source_text, fmt)
        if len(source_times) < 8:
            return content, ""

        refs = self.__collect_reference_subtitles(media_file, subtitle_file)
        if not refs:
            return content, ""

        max_offset_ms = max(10, self._auto_timing_max_offset_seconds) * 1000
        best_offset_ms: Optional[int] = None
        best_score = 0
        best_ref_name = ""

        for ref_path in refs:
            ref_fmt = ref_path.suffix.lower().lstrip(".")
            if ref_fmt not in {"srt", "ass", "ssa", "vtt"}:
                continue
            try:
                ref_content = ref_path.read_bytes()
            except Exception:
                continue
            ref_text = self.__decode_subtitle_text(ref_content)
            if not ref_text:
                continue
            ref_times = self.__extract_cue_times(ref_text, ref_fmt)
            if len(ref_times) < 8:
                continue

            estimated = self.__estimate_offset_ms(source_times, ref_times, max_offset_ms=max_offset_ms)
            if not estimated:
                continue

            offset_ms, score = estimated
            if score > best_score:
                best_score = score
                best_offset_ms = offset_ms
                best_ref_name = ref_path.name

        if best_offset_ms is None or abs(best_offset_ms) < 500:
            return content, ""

        shifted_text = self.__shift_subtitle_text(source_text, fmt, best_offset_ms)
        if not shifted_text or shifted_text == source_text:
            return content, ""

        note = f"自动校时 {best_offset_ms / 1000:+.1f}s, 参考 {best_ref_name}, 匹配 {best_score}"
        return shifted_text.encode("utf-8"), note

    def __collect_reference_subtitles(self, media_file: Path, subtitle_file: Path) -> List[Path]:
        refs: List[Path] = []
        prefix = media_file.stem
        for candidate in media_file.parent.iterdir():
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in self._subtitle_suffixes:
                continue
            if candidate.stem != prefix and not candidate.stem.startswith(f"{prefix}."):
                continue
            if self.__normalize_path(str(candidate)) == self.__normalize_path(str(subtitle_file)):
                continue
            refs.append(candidate)

        def sort_key(path: Path) -> Tuple[int, int]:
            name = path.name.lower()
            prefer_en = 0 if any(tag in name for tag in [".en.", ".eng.", "english", "英文"]) else 1
            try:
                size = -int(path.stat().st_size)
            except Exception:
                size = 0
            return prefer_en, size

        refs.sort(key=sort_key)
        return refs[:8]

    @staticmethod
    def __decode_subtitle_text(content: bytes) -> str:
        encodings = ("utf-8-sig", "utf-16", "utf-16le", "utf-16be", "gb18030", "cp936", "big5", "cp950")
        for encoding in encodings:
            try:
                return content.decode(encoding)
            except Exception:
                continue
        return content.decode("utf-8", errors="ignore")

    def __extract_cue_times(self, text: str, subtitle_format: str) -> List[int]:
        fmt = str(subtitle_format or "").strip().lower()
        if fmt == "srt":
            pattern = re.compile(
                r"(?m)(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
            )
            values = [self.__parse_srt_time(match.group(1)) for match in pattern.finditer(text)]
        elif fmt in {"ass", "ssa"}:
            pattern = re.compile(r"(?m)^(?:Dialogue|Comment):\s*\d+,(\d+:\d{2}:\d{2}\.\d{2}),")
            values = [self.__parse_ass_time_ms(match.group(1)) for match in pattern.finditer(text)]
        elif fmt == "vtt":
            pattern = re.compile(
                r"(?m)(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*-->\s*"
                r"(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
            )
            values = [self.__parse_vtt_time(match.group(1)) for match in pattern.finditer(text)]
        else:
            values = []

        cleaned = sorted(value for value in values if value >= 0)
        return cleaned[:600]

    @staticmethod
    def __estimate_offset_ms(
        source_times: List[int],
        ref_times: List[int],
        *,
        max_offset_ms: int,
    ) -> Optional[Tuple[int, int]]:
        if not source_times or not ref_times:
            return None

        bin_ms = 500
        max_shift = int(max_offset_ms // bin_ms)
        src_bins: Dict[int, int] = {}
        ref_bins: Dict[int, int] = {}

        for value in source_times[:600]:
            key = int(round(value / bin_ms))
            src_bins[key] = src_bins.get(key, 0) + 1
        for value in ref_times[:600]:
            key = int(round(value / bin_ms))
            ref_bins[key] = ref_bins.get(key, 0) + 1

        if not src_bins or not ref_bins:
            return None

        def score_for(shift: int) -> int:
            score = 0
            for key, count in src_bins.items():
                ref_count = ref_bins.get(key + shift, 0)
                if ref_count > 0:
                    score += min(count, ref_count)
            return score

        zero_score = score_for(0)
        best_shift = 0
        best_score = zero_score

        for shift in range(-max_shift, max_shift + 1):
            if shift == 0:
                continue
            current = score_for(shift)
            if current > best_score:
                best_score = current
                best_shift = shift

        min_required = max(6, min(sum(src_bins.values()), sum(ref_bins.values())) // 12)
        if best_shift == 0 or best_score < min_required:
            return None
        if best_score <= zero_score + 2:
            return None
        return best_shift * bin_ms, best_score

    def __shift_subtitle_text(self, text: str, subtitle_format: str, offset_ms: int) -> str:
        fmt = str(subtitle_format or "").strip().lower()
        if fmt == "srt":
            return self.__shift_srt_text(text, offset_ms)
        if fmt in {"ass", "ssa"}:
            return self.__shift_ass_text(text, offset_ms)
        if fmt == "vtt":
            return self.__shift_vtt_text(text, offset_ms)
        return text

    def __shift_srt_text(self, text: str, offset_ms: int) -> str:
        pattern = re.compile(
            r"(?m)(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
        )

        def repl(match: re.Match) -> str:
            start_ms = self.__parse_srt_time(match.group(1))
            end_ms = self.__parse_srt_time(match.group(2))
            shifted_start = max(0, start_ms + offset_ms)
            shifted_end = max(shifted_start, end_ms + offset_ms)
            return f"{self.__format_srt_time(shifted_start)} --> {self.__format_srt_time(shifted_end)}"

        return pattern.sub(repl, text)

    def __shift_ass_text(self, text: str, offset_ms: int) -> str:
        pattern = re.compile(
            r"(?m)^(?P<prefix>(?:Dialogue|Comment):\s*\d+,)"
            r"(?P<start>\d+:\d{2}:\d{2}\.\d{2}),"
            r"(?P<end>\d+:\d{2}:\d{2}\.\d{2})"
            r"(?P<suffix>,.*)$"
        )

        def repl(match: re.Match) -> str:
            start_ms = self.__parse_ass_time_ms(match.group("start"))
            end_ms = self.__parse_ass_time_ms(match.group("end"))
            shifted_start = max(0, start_ms + offset_ms)
            shifted_end = max(shifted_start, end_ms + offset_ms)
            return (
                f"{match.group('prefix')}"
                f"{self.__format_ass_time_ms(shifted_start)},"
                f"{self.__format_ass_time_ms(shifted_end)}"
                f"{match.group('suffix')}"
            )

        return pattern.sub(repl, text)

    def __shift_vtt_text(self, text: str, offset_ms: int) -> str:
        pattern = re.compile(
            r"(?m)(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*-->\s*"
            r"(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
        )

        def repl(match: re.Match) -> str:
            start_ms = self.__parse_vtt_time(match.group(1))
            end_ms = self.__parse_vtt_time(match.group(2))
            shifted_start = max(0, start_ms + offset_ms)
            shifted_end = max(shifted_start, end_ms + offset_ms)
            return f"{self.__format_vtt_time(shifted_start)} --> {self.__format_vtt_time(shifted_end)}"

        return pattern.sub(repl, text)

    @staticmethod
    def __parse_srt_time(value: str) -> int:
        match = re.match(r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})$", str(value).strip())
        if not match:
            return 0
        hour = int(match.group(1))
        minute = int(match.group(2))
        second = int(match.group(3))
        millisecond = int(match.group(4))
        return ((hour * 3600 + minute * 60 + second) * 1000) + millisecond

    @staticmethod
    def __format_srt_time(value_ms: int) -> str:
        total = max(0, int(value_ms))
        hour, remain = divmod(total, 3_600_000)
        minute, remain = divmod(remain, 60_000)
        second, millisecond = divmod(remain, 1000)
        return f"{hour:02d}:{minute:02d}:{second:02d},{millisecond:03d}"

    @staticmethod
    def __parse_ass_time_ms(value: str) -> int:
        match = re.match(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$", str(value).strip())
        if not match:
            return 0
        hour = int(match.group(1))
        minute = int(match.group(2))
        second = int(match.group(3))
        centisecond = int(match.group(4))
        return ((hour * 3600 + minute * 60 + second) * 1000) + centisecond * 10

    @staticmethod
    def __format_ass_time_ms(value_ms: int) -> str:
        total = max(0, int(value_ms))
        hour, remain = divmod(total, 3_600_000)
        minute, remain = divmod(remain, 60_000)
        second, millisecond = divmod(remain, 1000)
        centisecond = int(round(millisecond / 10.0))
        if centisecond >= 100:
            second += 1
            centisecond = 0
            if second >= 60:
                minute += 1
                second = 0
                if minute >= 60:
                    hour += 1
                    minute = 0
        return f"{hour}:{minute:02d}:{second:02d}.{centisecond:02d}"

    @staticmethod
    def __parse_vtt_time(value: str) -> int:
        text = str(value).strip()
        parts = text.split(":")
        if len(parts) == 2:
            hour = 0
            minute = int(parts[0])
            second_part = parts[1]
        elif len(parts) == 3:
            hour = int(parts[0])
            minute = int(parts[1])
            second_part = parts[2]
        else:
            return 0
        sec_parts = second_part.split(".")
        if len(sec_parts) != 2:
            return 0
        second = int(sec_parts[0])
        millisecond = int(sec_parts[1].ljust(3, "0")[:3])
        return ((hour * 3600 + minute * 60 + second) * 1000) + millisecond

    @staticmethod
    def __format_vtt_time(value_ms: int) -> str:
        total = max(0, int(value_ms))
        hour, remain = divmod(total, 3_600_000)
        minute, remain = divmod(remain, 60_000)
        second, millisecond = divmod(remain, 1000)
        return f"{hour:02d}:{minute:02d}:{second:02d}.{millisecond:03d}"

    @staticmethod
    def __is_video_file(path: str) -> bool:
        ext = Path(path).suffix.lower()
        return ext in {
            ".mp4",
            ".mkv",
            ".avi",
            ".wmv",
            ".flv",
            ".mov",
            ".m4v",
            ".ts",
            ".m2ts",
            ".webm",
        }

    def __iter_video_files(self, root: Path, recursive: bool) -> Iterator[Path]:
        iterator = root.rglob("*") if recursive else root.iterdir()
        for path in iterator:
            if not path.is_file():
                continue
            if not self.__is_video_file(str(path)):
                continue
            yield path

    def __has_subtitle(self, media_file: Path) -> bool:
        prefix = media_file.stem
        subtitle_prefix = f"{prefix}."
        for candidate in media_file.parent.iterdir():
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            if suffix not in self._subtitle_suffixes:
                continue
            name = candidate.name
            if name == f"{prefix}{suffix}" or name.startswith(subtitle_prefix):
                return True
        return False

    def __skip_reason_for_media(self, media_file: Path, parsed: Optional[Dict[str, Any]] = None) -> str:
        normalized_path = self.__normalize_path(str(media_file))
        if self.__is_chinese_library_path(normalized_path):
            return "中文内容库（国产/华语目录）"

        probe = self.__probe_media_streams(media_file)
        if probe.get("has_embedded_subtitle"):
            return "媒体已内封字幕"
        if probe.get("has_chinese_audio"):
            return "媒体含中文音轨"
        return ""

    def __is_chinese_library_path(self, normalized_path: str) -> bool:
        if any(marker in normalized_path for marker in self._chinese_library_markers):
            return True

        for segment in [item for item in normalized_path.split("/") if item]:
            if any(keyword in segment for keyword in self._chinese_library_keywords):
                return True
        return False

    def __probe_media_streams(self, media_file: Path) -> Dict[str, Any]:
        cache_key = self.__media_probe_cache_key(media_file)
        if cache_key and cache_key in self._media_probe_cache:
            return self._media_probe_cache[cache_key]

        result: Dict[str, Any] = {
            "has_embedded_subtitle": False,
            "has_chinese_audio": False,
            "probe_backend": "",
        }
        probe_errors: List[str] = []

        backends: List[Tuple[str, Any]] = [
            ("ffprobe", self.__probe_with_ffprobe),
            ("mediainfo", self.__probe_with_mediainfo),
            ("ffmpeg", self.__probe_with_ffmpeg),
        ]

        for backend_name, backend in backends:
            try:
                partial = backend(media_file)
            except FileNotFoundError:
                continue
            except Exception as error:
                probe_errors.append(f"{backend_name}:{error}")
                continue

            if not partial:
                continue

            result["has_embedded_subtitle"] = bool(
                result["has_embedded_subtitle"] or partial.get("has_embedded_subtitle")
            )
            result["has_chinese_audio"] = bool(result["has_chinese_audio"] or partial.get("has_chinese_audio"))
            if not result["probe_backend"] and (result["has_embedded_subtitle"] or result["has_chinese_audio"]):
                result["probe_backend"] = backend_name
            if result["has_embedded_subtitle"] and result["has_chinese_audio"]:
                break

        if not result["probe_backend"] and probe_errors:
            logger.debug(
                "[SubtitleAgentBridge] 媒体流探测失败，已使用默认判定: %s (%s)",
                media_file,
                "; ".join(probe_errors[:3]),
            )

        if cache_key:
            self._media_probe_cache[cache_key] = result
        return result

    def __probe_with_ffprobe(self, media_file: Path) -> Optional[Dict[str, bool]]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "stream=codec_type:stream_tags=language,title",
            str(media_file),
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._probe_command_timeout_seconds,
        )
        if proc.returncode != 0:
            return None

        payload = json.loads(proc.stdout or "{}")
        streams = payload.get("streams") if isinstance(payload, dict) else []
        if not isinstance(streams, list):
            streams = []

        result = {"has_embedded_subtitle": False, "has_chinese_audio": False}
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            codec_type = str(stream.get("codec_type") or "").strip().lower()
            tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
            language = str(tags.get("language") or "")
            title = str(tags.get("title") or "")
            if codec_type == "subtitle":
                result["has_embedded_subtitle"] = True
            elif codec_type == "audio" and self.__is_chinese_audio_stream(language=language, title=title):
                result["has_chinese_audio"] = True
            if result["has_embedded_subtitle"] and result["has_chinese_audio"]:
                break
        return result

    def __probe_with_mediainfo(self, media_file: Path) -> Optional[Dict[str, bool]]:
        cmd = ["mediainfo", "--Output=JSON", str(media_file)]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._probe_command_timeout_seconds,
        )
        if proc.returncode != 0:
            return None

        payload = json.loads(proc.stdout or "{}")
        media = payload.get("media") if isinstance(payload, dict) else {}
        tracks = media.get("track") if isinstance(media, dict) else []
        if not isinstance(tracks, list):
            tracks = []

        result = {"has_embedded_subtitle": False, "has_chinese_audio": False}
        for track in tracks:
            if not isinstance(track, dict):
                continue
            track_type = str(track.get("@type") or "").strip().lower()
            language = str(
                track.get("Language") or track.get("Language/String") or track.get("Language_String3") or ""
            )
            title = str(track.get("Title") or track.get("Title_More") or track.get("Title/String") or "")

            if track_type in {"text", "subtitle"}:
                result["has_embedded_subtitle"] = True
            elif track_type == "audio" and self.__is_chinese_audio_stream(language=language, title=title):
                result["has_chinese_audio"] = True

            if result["has_embedded_subtitle"] and result["has_chinese_audio"]:
                break
        return result

    def __probe_with_ffmpeg(self, media_file: Path) -> Optional[Dict[str, bool]]:
        cmd = ["ffmpeg", "-hide_banner", "-i", str(media_file)]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._probe_command_timeout_seconds,
        )
        output = "\n".join([str(proc.stdout or ""), str(proc.stderr or "")]).strip()
        if not output:
            return None

        result = {"has_embedded_subtitle": False, "has_chinese_audio": False}
        for raw_line in output.splitlines():
            line = str(raw_line or "").strip().lower()
            if "stream #" not in line:
                continue
            if "subtitle:" in line:
                result["has_embedded_subtitle"] = True
            if "audio:" in line:
                if re.search(r"\((zh|zho|chi|chs|cht|cmn|yue)\)", line):
                    result["has_chinese_audio"] = True
                elif self.__is_chinese_audio_stream(language="", title=line):
                    result["has_chinese_audio"] = True
            if result["has_embedded_subtitle"] and result["has_chinese_audio"]:
                break
        return result

    def __is_chinese_audio_stream(self, language: str, title: str) -> bool:
        lang = str(language or "").strip().lower().replace("_", "-")
        if lang in self._chinese_audio_lang_aliases or lang.startswith("zh"):
            return True
        title_text = str(title or "").strip().lower()
        if any(marker in title_text for marker in self._chinese_audio_title_markers):
            return True
        return False

    @staticmethod
    def __media_probe_cache_key(media_file: Path) -> str:
        try:
            stat = media_file.stat()
            return f"{media_file}:{stat.st_size}:{stat.st_mtime_ns}"
        except Exception:
            return ""

    def __build_title_candidates(
        self,
        parsed_title: Any,
        media_file: Path,
        name_keyword: str = "",
        alias_map: Optional[Dict[str, List[str]]] = None,
    ) -> List[str]:
        candidates: List[str] = []
        seen = set()

        keyword = name_keyword if self.__should_use_name_keyword(name_keyword) else ""
        alias_titles = self.__lookup_alias_titles(parsed_title=parsed_title, media_file=media_file, alias_map=alias_map or {})
        series_title = self.__extract_series_title(media_file)

        for raw_title in [parsed_title, *alias_titles, series_title, keyword, media_file.stem]:
            normalized = self.__clean_title_text(str(raw_title or ""))
            if not normalized:
                continue
            if self.__is_generic_title(normalized):
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(normalized)

        return candidates

    @staticmethod
    def __should_use_name_keyword(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        compact = re.sub(r"\s+", "", text)
        return len(compact) >= 3

    def __clean_title_text(self, title: str) -> str:
        value = str(title or "")
        value = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]", " ", value)
        value = self._season_episode_pattern.sub(" ", value)
        value = re.sub(r"(?i)\bS\d{1,2}\b", " ", value)
        value = re.sub(r"(?i)\bseason\s*\d+\b", " ", value)
        value = re.sub(r"第\s*\d+\s*[季集话]", " ", value)
        value = self._year_pattern.sub(" ", value)
        value = re.sub(
            r"(?i)\b(2160p|1080p|720p|480p|x264|x265|h264|h265|hevc|hdr|dv|bluray|bdrip|web[-_. ]?dl|webrip|hdrip|dvdrip|aac|dts|atmos|remux|repack|proper|extended|paramount\+?|netflix|amzn|hmax|hbo|max|disney\+?|atvp)\b",
            " ",
            value,
        )
        value = re.sub(r"(?i)\b\d+audios?\b", " ", value)
        value = re.sub(r"-[A-Za-z0-9]{2,}$", " ", value)
        value = re.sub(r"[._]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" -._")
        return value

    @staticmethod
    def __is_generic_title(value: str) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return True
        if re.fullmatch(r"season\s*\d+", text):
            return True
        if re.fullmatch(r"s\d{1,2}", text):
            return True
        if re.fullmatch(r"第\s*\d+\s*[季集话]", text):
            return True
        if text in {"movie", "tv", "series"}:
            return True
        return False

    def __extract_series_title(self, media_file: Path) -> str:
        for parent in media_file.parents[:3]:
            title = self.__clean_title_text(parent.name)
            if not title:
                continue
            if self.__is_generic_title(title):
                continue
            return title
        return ""

    def __parse_media_context_from_file(self, media_file: Path, forced_media_type: str = "") -> Dict[str, Any]:
        raw_name = media_file.stem
        cleaned = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]", " ", raw_name)

        season = None
        episode = None
        season_episode_match = self._season_episode_pattern.search(cleaned)
        if season_episode_match:
            season = self.__to_int(season_episode_match.group(1))
            episode = self.__to_int(season_episode_match.group(2))

        media_kind = self.__normalize_media_type(forced_media_type)
        if not media_kind:
            media_kind = "series" if season and episode else "movie"

        year = None
        year_match = self._year_pattern.search(raw_name) or self._year_pattern.search(cleaned)
        if year_match:
            year = self.__to_int(year_match.group(1))

        normalized_title = self.__clean_title_text(cleaned)

        if not normalized_title:
            normalized_title = self.__clean_title_text(media_file.parent.name)

        return {
            "title": normalized_title,
            "type": media_kind,
            "year": year,
            "season": season if media_kind == "series" else None,
            "episode": episode if media_kind == "series" else None,
        }

    @staticmethod
    def __build_subtitle_path(media_file: str, subtitle_format: str) -> str:
        fmt = (subtitle_format or "srt").strip().lower()
        if not fmt:
            fmt = "srt"
        return str(Path(media_file).with_suffix(f".{fmt}"))

    @staticmethod
    def __split_csv(value: Any) -> List[str]:
        return [item.strip() for item in str(value or "").split(",") if item and item.strip()]

    @staticmethod
    def __merge_csv_values(*values: Any) -> List[str]:
        merged: List[str] = []
        seen = set()
        for value in values:
            for item in SubtitleAgentBridge.__split_csv(value):
                key = item.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item.strip())
        return merged

    def __merge_title_aliases(self, *values: Any) -> Dict[str, List[str]]:
        alias_map: Dict[str, List[str]] = {}
        for value in values:
            if not value:
                continue
            chunks = re.split(r"[;\n]+", str(value))
            for chunk in chunks:
                pair = str(chunk or "").strip()
                if not pair:
                    continue
                key, sep, raw_values = pair.partition("=")
                if not sep:
                    key, sep, raw_values = pair.partition(":")
                if not sep:
                    continue
                source = self.__clean_title_text(key)
                if not source:
                    continue
                aliases = [
                    self.__clean_title_text(item)
                    for item in re.split(r"[|,]+", raw_values)
                    if self.__clean_title_text(item)
                ]
                if not aliases:
                    continue
                alias_map[source.lower()] = aliases
        return alias_map

    def __lookup_alias_titles(self, parsed_title: Any, media_file: Path, alias_map: Dict[str, List[str]]) -> List[str]:
        if not alias_map:
            return []

        keys = []
        parsed = self.__clean_title_text(parsed_title)
        series = self.__extract_series_title(media_file)
        if parsed:
            keys.append(parsed.lower())
        if series:
            keys.append(series.lower())

        aliases: List[str] = []
        seen = set()
        for key in keys:
            for item in alias_map.get(key, []):
                lowered = item.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                aliases.append(item)
        return aliases

    def __collect_scan_roots(self, directory: str, include_paths: List[str]) -> List[Path]:
        candidates = include_paths if include_paths else [directory]
        roots: List[Path] = []
        seen = set()
        for item in candidates:
            path = Path(str(item)).expanduser()
            if not path.exists() or not path.is_dir():
                continue
            key = self.__normalize_path(str(path))
            if key in seen:
                continue
            seen.add(key)
            roots.append(path)
        return roots

    @staticmethod
    def __split_languages(languages: str) -> List[str]:
        return SubtitleAgentBridge.__split_csv(languages)

    @staticmethod
    def __file_identity(path: Path) -> str:
        try:
            stat = path.stat()
            return f"{stat.st_dev}:{stat.st_ino}"
        except Exception:
            return ""

    def __is_included_path(self, media_file: Path, include_paths: List[str]) -> bool:
        if not include_paths:
            return True
        normalized_file = self.__normalize_path(str(media_file))
        for raw_path in include_paths:
            path_prefix = self.__normalize_path(raw_path)
            if not path_prefix:
                continue
            if normalized_file == path_prefix or normalized_file.startswith(f"{path_prefix}/"):
                return True
        return False

    def __is_excluded_path(self, media_file: Path, excluded_paths: List[str], excluded_keywords: List[str]) -> bool:
        normalized_file = self.__normalize_path(str(media_file))
        for raw_path in excluded_paths:
            path_prefix = self.__normalize_path(raw_path)
            if not path_prefix:
                continue
            if normalized_file == path_prefix or normalized_file.startswith(f"{path_prefix}/"):
                return True

        for keyword in excluded_keywords:
            key = str(keyword or "").strip().lower()
            if key and key in normalized_file:
                return True
        return False

    @staticmethod
    def __format_media_name(
        *,
        title: str,
        media_type: str = "",
        year: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> str:
        base = str(title or "").strip() or "未知媒体"
        media_key = str(media_type or "").strip().lower()
        if media_key in {"series", "tv", "show", "episode"} and season and episode:
            base = f"{base} S{int(season):02d}E{int(episode):02d}"
        if year:
            base = f"{base} ({int(year)})"
        return base

    @staticmethod
    def __requires_manual_download_notice(message: str) -> bool:
        lowered = str(message or "").strip().lower()
        return bool(lowered)

    def __maybe_notify_manual_download(
        self,
        *,
        media_name: str,
        failure_message: str,
        items: List[dict],
        target_file: str = "",
        preferred_languages: Optional[List[str]] = None,
    ) -> None:
        if not self._notify:
            return
        if not self.__requires_manual_download_notice(failure_message):
            return
        if not items:
            return

        dedup_key = f"{media_name}|{target_file}|{failure_message}"
        if dedup_key in self._manual_notice_cache:
            return
        self._manual_notice_cache.add(dedup_key)

        lines: List[str] = []
        seen_links = set()
        seen_items = set()
        copy_links: List[str] = []
        recommended_entry = ""
        preferred = preferred_languages or self.__split_languages(self._languages)
        selected = self.__pick_item(items, preferred_languages=preferred)
        selected_link = str(selected.get("page_link") or "").strip()
        if not selected_link:
            raw_download = str(selected.get("download_url") or "").strip()
            if raw_download:
                selected_link = self.__compose_url(raw_download)
        if selected_link:
            selected_provider = str(selected.get("provider") or "unknown")
            selected_language = str(selected.get("language") or "und")
            selected_name = str(
                selected.get("name")
                or selected.get("title")
                or selected.get("subtitle_id")
                or "字幕候选"
            )
            selected_score = self.__to_int(selected.get("score"))
            score_suffix = f"（score={selected_score}）" if selected_score is not None else ""
            recommended_entry = f"[{selected_provider}/{selected_language}] {selected_name}{score_suffix}\n{selected_link}"
            copy_links.append(selected_link)

        for item in items:
            dedup_item_key = self.__manual_item_dedup_key(item)
            if dedup_item_key in seen_items:
                continue
            seen_items.add(dedup_item_key)

            raw_link = str(item.get("page_link") or "").strip()
            if not raw_link:
                raw_download = str(item.get("download_url") or "").strip()
                if raw_download:
                    raw_link = self.__compose_url(raw_download)
            if not raw_link:
                continue
            if raw_link in seen_links:
                continue
            seen_links.add(raw_link)
            copy_links.append(raw_link)

            provider = str(item.get("provider") or "unknown")
            language = str(item.get("language") or "und")
            subtitle_name = str(
                item.get("name")
                or item.get("title")
                or item.get("subtitle_id")
                or "字幕候选"
            )
            lines.append(f"[{provider}/{language}] {subtitle_name}\n{raw_link}")
            if len(lines) >= 5:
                break

        if not lines:
            return

        text_lines = [
            f"媒体: {media_name}",
            f"原因: {failure_message}",
        ]
        failure_kind = self.__classify_manual_failure(failure_message)
        failure_hint = self.__manual_failure_hint(failure_kind)
        if failure_hint:
            text_lines.append(f"建议: {failure_hint}")
        if target_file:
            text_lines.append(f"文件: {target_file}")
        if recommended_entry:
            text_lines.append("推荐优先下载：")
            text_lines.append(recommended_entry)
        text_lines.append("请手动下载以下候选字幕：")
        for index, entry in enumerate(lines, 1):
            text_lines.append(f"{index}. {entry}")
        if copy_links:
            deduped_copy_links: List[str] = []
            seen_copy_links = set()
            for link in copy_links:
                if link in seen_copy_links:
                    continue
                seen_copy_links.add(link)
                deduped_copy_links.append(link)
            text_lines.append("复制全部链接：")
            text_lines.append("\n".join(deduped_copy_links))

        search_keyword = self.__manual_search_keyword(media_name)
        if search_keyword:
            encoded = quote(search_keyword)
            text_lines.append("可补充检索：")
            text_lines.append(f"SubHD: https://subhd.tv/search/{encoded}")
            text_lines.append(f"SubHD-TW: https://subhdtw.com/search/{encoded}")
            text_lines.append(f"Assrt: https://assrt.net/sub/?searchword={encoded}")

        self.post_message(
            mtype=NotificationType.Plugin,
            title="Subtitle Agent 需手动下载字幕",
            text="\n".join(text_lines),
        )

    @staticmethod
    def __provider_family(provider: Any) -> str:
        text = str(provider or "").strip().lower()
        if text in {"subhd", "subhdtw"}:
            return "subhd-family"
        return text or "unknown"

    def __manual_item_dedup_key(self, item: dict) -> str:
        provider = self.__provider_family(item.get("provider"))
        subtitle_id = str(item.get("subtitle_id") or item.get("id") or "").strip()
        title = str(item.get("name") or item.get("title") or "").strip().lower()
        if subtitle_id:
            return f"{provider}|{subtitle_id}"
        return f"{provider}|{title}"

    @staticmethod
    def __normalize_failure_message(message: Any, default: str) -> str:
        text = str(message or "").strip()
        if not text or text.lower() in {"ok", "success", "none"}:
            return default
        return text

    @staticmethod
    def __classify_manual_failure(message: str) -> str:
        text = str(message or "").strip().lower()
        if "captcha" in text or "验证码" in text:
            return "captcha"
        if "timeout" in text or "timed out" in text or "超时" in text:
            return "timeout"
        if "未找到" in text or "not found" in text:
            return "not_found"
        if "no verified chinese" in text or "无可自动下载中文字幕" in text:
            return "no_verified_chinese"
        return "generic"

    @staticmethod
    def __manual_failure_hint(kind: str) -> str:
        if kind == "captcha":
            return "目标站点触发验证码，自动下载受限。请先在浏览器打开候选链接完成验证后再重试。"
        if kind == "timeout":
            return "请求超时。可稍后重试，或提高插件 HTTP 超时后再补字幕。"
        if kind == "no_verified_chinese":
            return "自动链路未命中可验证的中文字幕。可先手动下载候选字幕，后续等待新字幕再自动回填。"
        if kind == "not_found":
            return "当前源未检索到结果。可用片名别名或英文名重试。"
        return ""

    @staticmethod
    def __manual_search_keyword(media_name: str) -> str:
        text = str(media_name or "").strip()
        if not text:
            return ""
        text = re.sub(r"\s+S\d{2}E\d{2}\b.*$", "", text)
        text = re.sub(r"\s+\(\d{4}\)\s*$", "", text)
        return text.strip()

    @staticmethod
    def __to_int(value: Any) -> Optional[int]:
        if value in [None, "", 0, "0"]:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def __to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n"}:
            return False
        return default

    @staticmethod
    def __normalize_periodic_mode(value: Any) -> str:
        mode = str(value or "").strip().lower()
        if mode in {"daily", "day", "cron"}:
            return "daily"
        return "interval"

    @staticmethod
    def __normalize_daily_time(value: Any) -> str:
        text = str(value or "").strip()
        match = re.match(r"^(\d{1,2}):(\d{1,2})$", text)
        if not match:
            return "03:30"
        hour = max(0, min(int(match.group(1)), 23))
        minute = max(0, min(int(match.group(2)), 59))
        return f"{hour:02d}:{minute:02d}"

    def __seconds_until_daily_run(self, daily_time: str) -> int:
        normalized = self.__normalize_daily_time(daily_time)
        hour, minute = (int(part) for part in normalized.split(":"))
        now = datetime.now()
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run + timedelta(days=1)
        return max(1, int((next_run - now).total_seconds()))

    @staticmethod
    def __normalize_media_type(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"series", "tv", "show", "episode"}:
            return "series"
        if text in {"movie", "film"}:
            return "movie"
        return ""

    @staticmethod
    def __normalize_path(value: Any) -> str:
        return str(value or "").replace("\\", "/").strip().rstrip("/").lower()

    @staticmethod
    def __normalize_host(host: Any) -> str:
        if not host:
            return ""
        value = str(host).strip()
        if not value:
            return ""
        if not value.startswith("http"):
            value = f"http://{value}"
        if value.endswith("/"):
            value = value[:-1]
        return value

    def __compose_url(self, path: str) -> str:
        if str(path).startswith("http://") or str(path).startswith("https://"):
            return str(path)
        if not self._host:
            return str(path)
        return urljoin(f"{self._host}/", str(path).lstrip("/"))
