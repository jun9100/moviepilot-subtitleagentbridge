import json
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import Request as FastAPIRequest
from fastapi.responses import HTMLResponse
from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType

_USER_MESSAGE_EVENT = getattr(EventType, "UserMessage", None)


class SubtitleAgentBridge(_PluginBase):
    """
    MoviePilot 插件示例：对接外部 Subtitle Agent，自动为入库媒体下载字幕。
    """

    plugin_name = "Subtitle Agent Bridge"
    plugin_desc = "调用外部 MoviePilot Subtitle Agent 自动检索并下载字幕。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.5.46"
    plugin_author = "jun9100"
    author_url = "https://github.com/jun9100/moviepilot-subtitleagentbridge"
    plugin_config_prefix = "subtitleagentbridge_"
    plugin_order = 50
    auth_level = 1

    _enabled: bool = False
    _host: str = ""
    _web_base_url: str = ""
    _runtime_web_base_url: str = ""
    _search_path: str = "/api/v1/moviepilot/subtitles/search"
    _languages: str = "zh-cn,zh-tw"
    _limit: int = 5
    _timeout: int = 60
    _overwrite: bool = False
    _notify: bool = True
    _include_paths: str = ""
    _exclude_paths: str = ""
    _exclude_keywords: str = "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache"
    _manual_skip_keywords: str = ""
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
    _manual_job_lock = threading.Lock()
    _media_probe_cache: Dict[str, Dict[str, Any]] = {}
    _nfo_chinese_cache: Dict[str, bool] = {}
    _season_embedded_hint_cache: Dict[str, bool] = {}
    _dry_run_detail_limit: int = 500
    _auto_skip_cjk_documentary: bool = True
    _notify_success_detail_limit: int = 5
    _captcha_task_ttl_hours: int = 6
    _manual_job_ttl_hours: int = 24
    _captcha_submit_dedup_seconds: float = 2.0
    _recent_captcha_submit_lock = threading.Lock()
    _recent_captcha_submits: Dict[str, float] = {}
    _refresh_code_keywords = {"refresh", "reload", "new", "again"}
    _target_resolve_scan_limit: int = 3000

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
    _subtitle_name_noise_tokens = {
        "chi",
        "zho",
        "chs",
        "cht",
        "zh",
        "zhcn",
        "zhtw",
        "cn",
        "tw",
        "eng",
        "en",
        "jpn",
        "jp",
        "kor",
        "kr",
        "sub",
        "subs",
        "subtitle",
        "subtitles",
        "srt",
        "ass",
        "ssa",
        "vtt",
        "简体",
        "繁体",
        "简繁",
        "双语",
        "中字",
        "字幕",
    }
    _nfo_chinese_country_markers = {"china", "cn", "中国", "中国大陆", "大陆", "hong kong", "香港", "taiwan", "台湾"}
    _nfo_chinese_language_markers = {
        "zh",
        "zho",
        "chi",
        "chs",
        "cht",
        "cmn",
        "yue",
        "chinese",
        "mandarin",
        "国语",
        "普通话",
        "中文",
        "粤语",
    }
    _foreign_library_markers = {
        "/日韩剧/",
        "/欧美剧/",
        "/韩剧/",
        "/日剧/",
        "/美剧/",
        "/英剧/",
        "/外语电影/",
        "/欧美电影/",
        "/日本/",
        "/韩国/",
        "/japan/",
        "/korea/",
    }
    _anime_library_markers = {
        "/日番/",
        "/番剧/",
        "/anime/",
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
        self._web_base_url = self.__normalize_host(config.get("web_base_url"))
        self._runtime_web_base_url = ""
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
        self._manual_skip_keywords = str(config.get("manual_skip_keywords") or "")
        self._title_aliases = str(config.get("title_aliases") or "")
        self._auto_skip_cjk_documentary = self.__to_bool(config.get("auto_skip_cjk_documentary"), default=True)
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
        self._nfo_chinese_cache = {}
        self._season_embedded_hint_cache = {}
        self._recent_captcha_submits = {}
        self.__cleanup_captcha_tasks()
        self.__cleanup_manual_jobs()
        self.__start_periodic_worker()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/subcap",
                "event": EventType.PluginAction,
                "desc": "提交字幕验证码: /subcap 任务ID 验证码",
                "category": "字幕",
                "data": {"action": "subtitle_agent_subcap"},
            },
            {
                "cmd": "/substatus",
                "event": EventType.PluginAction,
                "desc": "查询字幕任务状态: /substatus [任务ID]",
                "category": "字幕",
                "data": {"action": "subtitle_agent_substatus"},
            },
            {
                "cmd": "/subhelp",
                "event": EventType.PluginAction,
                "desc": "查看字幕验证码命令帮助",
                "category": "字幕",
                "data": {"action": "subtitle_agent_subhelp"},
            },
        ]

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
                "path": "/download_subtitle_async",
                "endpoint": self.download_subtitle_async,
                "methods": ["GET"],
                "summary": "异步下载字幕",
                "description": "异步触发字幕搜索下载任务，立即返回任务ID，避免长耗时请求超时。",
            },
            {
                "path": "/job_status",
                "endpoint": self.job_status,
                "methods": ["GET"],
                "summary": "查看任务状态",
                "description": "查询异步字幕任务的执行状态和结果。",
            },
            {
                "path": "/notify_status",
                "endpoint": self.notify_status,
                "methods": ["GET"],
                "summary": "发送状态通知",
                "description": "通过 MoviePilot 通知渠道推送进度或结果消息。",
            },
            {
                "path": "/submit_captcha",
                "endpoint": self.submit_captcha,
                "methods": ["GET"],
                "summary": "提交字幕验证码",
                "description": "为待处理的 SubHD 验证码任务提交验证码并继续下载字幕。",
            },
            {
                "path": "/captcha_web",
                "endpoint": self.captcha_web,
                "methods": ["GET"],
                "summary": "验证码网页回填",
                "description": "打开网页查看最新验证码并提交，无需在TG手动输入命令。",
            },
            {
                "path": "/backfill_directory",
                "endpoint": self.backfill_directory,
                "methods": ["GET"],
                "summary": "补齐目录字幕",
                "description": "扫描目录中缺失字幕的视频文件并批量下载字幕。支持仅扫描刮削后目录，并排除整理前/刷流目录。",
            },
            {
                "path": "/debug_subtitle_presence",
                "endpoint": self.debug_subtitle_presence,
                "methods": ["GET"],
                "summary": "调试字幕判定",
                "description": "返回指定媒体文件的字幕存在判定细节，便于排查误判。",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "web_base_url",
                                            "label": "网页回填基地址(可选)",
                                            "placeholder": "http://192.168.12.4:5010",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_skip_cjk_documentary",
                                            "label": "自动跳过中文纪录片",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "manual_skip_keywords",
                                            "label": "手动跳过媒体关键词",
                                            "placeholder": "哈尔的移动城堡,无需字幕示例片名",
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
            "web_base_url": "",
            "search_path": "/api/v1/moviepilot/subtitles/search",
            "languages": "zh-cn,zh-tw",
            "limit": 5,
            "timeout": 60,
            "overwrite": False,
            "notify": True,
            "include_paths": "",
            "exclude_paths": "",
            "exclude_keywords": "整理前,刷流,strm,stream,downloads,download,incoming,temp,cache",
            "manual_skip_keywords": "",
            "title_aliases": "",
            "auto_skip_cjk_documentary": True,
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
        success_details = last_result.get("success_details") or []
        if success_details:
            lines.append("成功详情:")
            lines.extend(success_details[:10])
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

    if _USER_MESSAGE_EVENT is not None:
        @eventmanager.register(_USER_MESSAGE_EVENT)
        def handle_user_message(self, event: Event):
            if not self._enabled or not self._host:
                return

            event_data = event.event_data or {}
            text = self.__extract_user_message_text(event_data)
            if not text:
                return

            parsed = self.__parse_captcha_reply(text)
            if parsed is None:
                if re.match(r"^\s*/?substatus(?:\s+[A-Za-z0-9]{4,32})?\s*$", text, re.IGNORECASE):
                    job_id_match = re.match(r"^\s*/?substatus(?:\s+([A-Za-z0-9]{4,32}))?\s*$", text, re.IGNORECASE)
                    job_id = job_id_match.group(1) if job_id_match else ""
                    self.__post_message_to_context(
                        title="Subtitle Agent 任务状态",
                        text=self.__render_manual_job_status(job_id=job_id),
                        message_context=self.__extract_message_context(event_data),
                    )
                    return
                if re.match(r"^\s*/?subcap(?:tcha)?\b", text, re.IGNORECASE):
                    self.__post_message_to_context(
                        title="Subtitle Agent 验证码格式错误",
                        text="请发送: /subcap 任务ID 图中字母\n示例: /subcap 91e65710 AbCd\n刷新验证码: /subcap 91e65710 refresh",
                        message_context=self.__extract_message_context(event_data),
                    )
                    return
                if re.match(r"^\s*/?subhelp\s*$", text, re.IGNORECASE) or re.match(
                    r"^\s*/?subcap(?:tcha)?\s*$", text, re.IGNORECASE
                ):
                    self.__post_message_to_context(
                        title="Subtitle Agent 验证码帮助",
                        text="可用命令：\n1) /subcap 任务ID 验证码\n2) /subcap 任务ID refresh\n3) /substatus [任务ID]",
                        message_context=self.__extract_message_context(event_data),
                    )
                return

            task_id, code = parsed
            message_context = self.__extract_message_context(event_data)
            if self.__is_duplicate_captcha_submit(
                task_id=task_id,
                code=code,
                message_context=message_context,
            ):
                return
            self.__submit_captcha_task(
                task_id=task_id,
                code=code,
                message_context=message_context,
            )

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not self._enabled or not self._host or not event or not event.event_data:
            return

        event_data = event.event_data or {}
        action = str(event_data.get("action") or "").strip().lower()
        if action not in {"subtitle_agent_subcap", "subtitle_agent_substatus", "subtitle_agent_subhelp"}:
            return

        message_context = self.__extract_message_context(event_data)
        arg_str = str(event_data.get("arg_str") or "").strip()
        fallback_text = self.__extract_user_message_text(event_data)

        if action == "subtitle_agent_subhelp":
            self.__post_message_to_context(
                title="Subtitle Agent 验证码帮助",
                text="可用命令：\n1) /subcap 任务ID 验证码\n2) /subcap 任务ID refresh\n3) /substatus [任务ID]",
                message_context=message_context,
            )
            return

        if action == "subtitle_agent_substatus":
            status_arg = self.__extract_command_args(
                command="substatus",
                arg_str=arg_str,
                fallback_text=fallback_text,
            )
            job_id_match = re.match(r"^([A-Za-z0-9]{4,32})", status_arg or "")
            job_id = job_id_match.group(1) if job_id_match else ""
            self.__post_message_to_context(
                title="Subtitle Agent 任务状态",
                text=self.__render_manual_job_status(job_id=job_id),
                message_context=message_context,
            )
            return

        cap_arg = self.__extract_command_args(
            command="subcap",
            arg_str=arg_str,
            fallback_text=fallback_text,
        )
        parsed = self.__parse_captcha_reply(cap_arg) or self.__parse_captcha_reply(f"/subcap {cap_arg}")
        if parsed is None:
            self.__post_message_to_context(
                title="Subtitle Agent 验证码格式错误",
                text="请发送: /subcap 任务ID 图中字母\n示例: /subcap 91e65710 AbCd\n刷新验证码: /subcap 91e65710 refresh",
                message_context=message_context,
            )
            return

        task_id, code = parsed
        if self.__is_duplicate_captcha_submit(
            task_id=task_id,
            code=code,
            message_context=message_context,
        ):
            return
        self.__submit_captcha_task(
            task_id=task_id,
            code=code,
            message_context=message_context,
        )

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
        success_details = []
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
                success_details.append(
                    f"{Path(media_file).name} -> {self.__format_success_target(message)}"
                )
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
            "success_details": success_details[:200],
            "errors": errors,
        }
        self.save_data("last_result", result)

        if self._notify:
            text = f"处理 {total} 个视频，成功 {success} 个，跳过 {skipped} 个，失败 {failed} 个"
            success_lines = self.__render_success_details(success_details)
            if success_lines:
                text = f"{text}\n成功详情:\n" + "\n".join(success_lines)
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
        request: FastAPIRequest = None,
    ) -> schemas.Response:
        """
        手动触发字幕下载，可由插件API调用：
        /api/v1/plugin/SubtitleAgentBridge/download_subtitle
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        self.__remember_web_base_url(request)

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
        content, subtitle_format, message, error_data = self.__download_item(selected)
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
                error_data=error_data,
                title=title,
                media_type=media_type,
                year=year,
                season=season,
                episode=episode,
            )
            response_data = self.__captcha_task_response_data(
                error_data=error_data,
                media_name=self.__format_media_name(
                    title=title,
                    media_type=media_type,
                    year=year,
                    season=season,
                    episode=episode,
                ),
                target_file=target_file,
                title=title,
                media_type=media_type,
                year=year,
                season=season,
                episode=episode,
            )
            return schemas.Response(success=False, message=failure_message, data=response_data)

        target_file = self.__resolve_target_file_for_write(
            target_file=target_file,
            title=title,
            media_type=media_type,
            year=year,
            season=season,
            episode=episode,
        )
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

    def download_subtitle_async(
        self,
        title: str,
        apikey: str,
        media_type: str = "movie",
        year: int = None,
        season: int = None,
        episode: int = None,
        target_file: str = "",
        languages: str = "",
        request: FastAPIRequest = None,
    ) -> schemas.Response:
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        self.__remember_web_base_url(request)

        media_name = self.__format_media_name(
            title=title,
            media_type=media_type,
            year=year,
            season=season,
            episode=episode,
        )
        job_id = uuid4().hex[:8]
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job_payload = {
            "job_id": job_id,
            "status": "queued",
            "title": title,
            "media_type": media_type,
            "year": year,
            "season": season,
            "episode": episode,
            "target_file": target_file,
            "languages": languages or self._languages,
            "media_name": media_name,
            "message": "任务排队中",
            "created_at": now_text,
            "updated_at": now_text,
        }
        self.__save_manual_job(job_id=job_id, payload=job_payload)

        worker = threading.Thread(
            target=self.__run_manual_download_job,
            kwargs={
                "job_id": job_id,
                "title": title,
                "media_type": media_type,
                "year": year,
                "season": season,
                "episode": episode,
                "target_file": target_file,
                "languages": languages or self._languages,
                "media_name": media_name,
            },
            daemon=True,
            name=f"subtitleagentbridge-manual-{job_id}",
        )
        worker.start()

        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="Subtitle Agent 异步任务已提交",
                text=f"任务ID: {job_id}\n媒体: {media_name}",
            )

        return schemas.Response(
            success=True,
            message=f"任务已提交: {job_id}",
            data={
                "job_id": job_id,
                "status": "queued",
                "media_name": media_name,
            },
        )

    def job_status(self, apikey: str, job_id: str = "") -> schemas.Response:
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        self.__cleanup_manual_jobs()
        jobs = self.__load_manual_jobs()
        normalized_job_id = str(job_id or "").strip()
        if normalized_job_id:
            payload = jobs.get(normalized_job_id)
            if not isinstance(payload, dict):
                return schemas.Response(success=False, message="任务不存在或已过期")
            return schemas.Response(success=True, message="ok", data=payload)

        entries = list(jobs.values())
        entries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return schemas.Response(success=True, message="ok", data={"total": len(entries), "jobs": entries[:30]})

    def notify_status(self, apikey: str, text: str, title: str = "Subtitle Agent 状态") -> schemas.Response:
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        body = str(text or "").strip()
        if not body:
            return schemas.Response(success=False, message="text 不能为空")

        msg_title = str(title or "").strip() or "Subtitle Agent 状态"
        self.post_message(
            mtype=NotificationType.Plugin,
            title=msg_title,
            text=body,
        )
        return schemas.Response(success=True, message="通知已发送")

    def __render_manual_job_status(self, job_id: str = "") -> str:
        self.__cleanup_manual_jobs()
        jobs = self.__load_manual_jobs()
        normalized = str(job_id or "").strip()
        if normalized:
            job = jobs.get(normalized)
            if not isinstance(job, dict):
                return "任务不存在或已过期"
            return "\n".join(
                [
                    f"任务ID: {normalized}",
                    f"状态: {self.__human_job_status(job.get('status'))}",
                    f"媒体: {job.get('media_name') or job.get('title') or '未知媒体'}",
                    f"更新时间: {job.get('updated_at')}",
                    f"结果: {job.get('message') or ''}",
                ]
            )

        if not jobs:
            return "当前没有可查询任务"

        items = list(jobs.values())
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        lines = ["最近任务："]
        for item in items[:5]:
            lines.append(
                f"{item.get('job_id')} | {self.__human_job_status(item.get('status'))} | "
                f"{item.get('media_name') or item.get('title') or '未知媒体'} | "
                f"{item.get('updated_at')}"
            )
        return "\n".join(lines)

    @staticmethod
    def __human_job_status(status: Any) -> str:
        key = str(status or "").strip().lower()
        if key == "captcha_required":
            return "等待验证码"
        if key == "queued":
            return "排队中"
        if key == "running":
            return "执行中"
        if key == "success":
            return "成功"
        if key == "failed":
            return "失败"
        return key or "未知"

    def __run_manual_download_job(
        self,
        *,
        job_id: str,
        title: str,
        media_type: str,
        year: int,
        season: int,
        episode: int,
        target_file: str,
        languages: str,
        media_name: str,
    ) -> None:
        self.__update_manual_job(
            job_id=job_id,
            status="running",
            message="任务执行中",
        )
        try:
            response = self.download_subtitle(
                title=title,
                apikey=settings.API_TOKEN,
                media_type=media_type,
                year=year,
                season=season,
                episode=episode,
                target_file=target_file,
                languages=languages,
            )
            captcha_required = bool(
                (not response.success)
                and isinstance(response.data, dict)
                and str(response.data.get("captcha_task_id") or "").strip()
            )
            if response.success:
                status = "success"
            elif captcha_required:
                status = "captcha_required"
            else:
                status = "failed"
            self.__update_manual_job(
                job_id=job_id,
                status=status,
                message=response.message,
                result_data=response.data if isinstance(response.data, dict) else {},
            )

            if self._notify:
                if captcha_required:
                    logger.info(f"[SubtitleAgentBridge] 异步任务 {job_id} 已进入验证码流程，跳过重复失败通知")
                    return
                notice_title = "Subtitle Agent 异步任务成功" if response.success else "Subtitle Agent 异步任务失败"
                lines = [f"任务ID: {job_id}", f"媒体: {media_name}"]
                notice_image = None
                if isinstance(response.data, dict):
                    output_path = str(response.data.get("path") or "").strip()
                    if output_path:
                        lines.append(f"字幕: {output_path}")
                    captcha_task_id = str(response.data.get("captcha_task_id") or "").strip()
                    detail_url = str(response.data.get("detail_url") or "").strip()
                    image_url = str(response.data.get("image_url") or "").strip()
                    web_url = str(response.data.get("web_url") or "").strip()
                    if captcha_task_id:
                        lines.append("结果: 需要验证码")
                        lines.append(f"验证码任务: {captcha_task_id}")
                        if web_url:
                            lines.append(f"网页回填: {web_url}")
                        if detail_url:
                            lines.append(f"详情页: {detail_url}")
                        if image_url:
                            lines.append(f"验证码图: {image_url}")
                            notice_image = image_url
                    reply_format = str(response.data.get("reply_format") or "").strip()
                    if reply_format:
                        lines.append(f"回复: {reply_format}")
                if len(lines) == 2:
                    lines.append(f"结果: {response.message}")
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title=notice_title,
                    text="\n".join(lines),
                    image=notice_image,
                )
        except Exception as err:
            message = f"异步任务异常: {str(err)}"
            self.__update_manual_job(job_id=job_id, status="failed", message=message)
            logger.error(f"[SubtitleAgentBridge] {message}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="Subtitle Agent 异步任务异常",
                    text=f"任务ID: {job_id}\n媒体: {media_name}\n{message}",
                )

    def submit_captcha(self, apikey: str, task_id: str, code: str, request: FastAPIRequest = None) -> schemas.Response:
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        self.__remember_web_base_url(request)
        return self.__submit_captcha_task(task_id=task_id, code=code)

    def captcha_web(
        self,
        token: str = "",
        task_id: str = "",
        code: str = "",
        action: str = "",
        apikey: str = "",
        request: FastAPIRequest = None,
    ) -> HTMLResponse:
        self.__remember_web_base_url(request)
        normalized_token = str(token or "").strip().lower()
        normalized_task_id = str(task_id or "").strip().lower()
        if not normalized_task_id and normalized_token:
            normalized_task_id = self.__find_captcha_task_id_by_web_token(normalized_token)

        tasks = self.__load_captcha_tasks()
        task = tasks.get(normalized_task_id) if normalized_task_id else None
        if not isinstance(task, dict):
            html = self.__render_captcha_web_html(
                task_data=None,
                result_text="验证码任务不存在或已过期，请回到 Telegram 获取最新通知。",
                result_ok=False,
            )
            return HTMLResponse(content=html, status_code=404)

        result: Optional[schemas.Response] = None
        normalized_action = str(action or "").strip().lower()
        normalized_code = str(code or "").strip()
        if normalized_action in self._refresh_code_keywords or normalized_code.lower() in self._refresh_code_keywords:
            result = self.__refresh_captcha_task(task_id=normalized_task_id)
        elif normalized_code:
            result = self.__submit_captcha_task(task_id=normalized_task_id, code=normalized_code)

        latest_tasks = self.__load_captcha_tasks()
        latest_task = latest_tasks.get(normalized_task_id)
        view_data = (
            self.__captcha_task_view_data(
                task_id=normalized_task_id,
                task=latest_task,
                apikey=apikey,
            )
            if latest_task
            else None
        )

        result_text = ""
        result_ok = False
        if isinstance(result, schemas.Response):
            result_text = str(result.message or "").strip()
            result_ok = bool(result.success)

        html = self.__render_captcha_web_html(
            task_data=view_data,
            result_text=result_text,
            result_ok=result_ok,
        )
        return HTMLResponse(content=html, status_code=200 if view_data or result_ok else 404)

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
        detail_limit: int = 0,
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
        detail_count = self.__to_int(detail_limit) or self._dry_run_detail_limit
        if detail_count <= 0:
            detail_count = self._dry_run_detail_limit
        detail_count = min(detail_count, 10000)
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
        skipped_files: List[Dict[str, Any]] = []

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
                        if dry_run_flag and len(skipped_files) < detail_count:
                            skipped_files.append(
                                {
                                    "video": str(video_file),
                                    "reason": "已有外挂字幕",
                                }
                            )
                        continue

                    parsed = self.__parse_media_context_from_file(video_file, forced_media_type=desired_type)
                    skip_reason = self.__skip_reason_for_media(video_file, parsed)
                    if skip_reason:
                        skipped += 1
                        if dry_run_flag and len(skipped_files) < detail_count:
                            skipped_files.append(
                                {
                                    "video": str(video_file),
                                    "reason": skip_reason,
                                }
                            )
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
                    content, subtitle_format, message, error_data = self.__download_item(selected)
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
                            error_data=error_data,
                            title=str(selected_title or parsed.get("title") or video_file.stem),
                            media_type=str(parsed.get("type") or ""),
                            year=self.__to_int(parsed.get("year")),
                            season=self.__to_int(parsed.get("season")),
                            episode=self.__to_int(parsed.get("episode")),
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
            "scanned": processed,
            "total": processed,
            "success": success,
            "skipped": skipped,
            "excluded": excluded,
            "failed": failed,
            "dry_run": dry_run_flag,
            "missing": len(missing_files),
            "skipped_files": skipped_files[:200],
            "success_details": [
                self.__format_backfill_success_detail(item)
                for item in downloaded[:200]
            ],
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
                success_lines = self.__render_success_details(
                    [self.__format_backfill_success_detail(item) for item in downloaded]
                )
                if success_lines:
                    text = f"{text}\n成功详情:\n" + "\n".join(success_lines)
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
                "scanned": processed,
                "processed": processed,
                "total": processed,
                "success": success,
                "skipped": skipped,
                "excluded": excluded,
                "failed": failed,
                "missing": len(missing_files),
                "missing_files": missing_files[:detail_count] if dry_run_flag else missing_files[:200],
                "skipped_files": skipped_files[:detail_count] if dry_run_flag else [],
                "items": downloaded[:50],
                "errors": errors[:50],
            },
        )

    def __format_success_target(self, raw: Any) -> str:
        text = str(raw or "").strip()
        if not text:
            return "-"
        main = text.split(" (", 1)[0].strip()
        if not main:
            return text
        return Path(main).name or main

    def __format_backfill_success_detail(self, item: Dict[str, Any]) -> str:
        video_name = Path(str(item.get("video") or "")).name
        subtitle_name = Path(str(item.get("subtitle") or "")).name
        provider = str(item.get("provider") or "-").strip() or "-"
        sync_note = str(item.get("sync") or "").strip()
        detail = f"{video_name} -> {subtitle_name} [{provider}]"
        if sync_note:
            detail = f"{detail} ({sync_note})"
        return detail

    def __render_success_details(self, details: List[str]) -> List[str]:
        clean = [str(item).strip() for item in details if str(item).strip()]
        if not clean:
            return []
        limit = max(1, self._notify_success_detail_limit)
        lines = clean[:limit]
        remaining = len(clean) - len(lines)
        if remaining > 0:
            lines.append(f"... 其余 {remaining} 条请在插件“查看数据”中查看")
        return lines

    def debug_subtitle_presence(self, apikey: str, media_file: str) -> schemas.Response:
        """
        调试某个媒体文件的字幕存在判定：
        /api/v1/plugin/SubtitleAgentBridge/debug_subtitle_presence
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        if not media_file:
            return schemas.Response(success=False, message="media_file 参数不能为空")

        path = Path(str(media_file))
        if not path.exists():
            return schemas.Response(success=False, message=f"文件不存在: {media_file}")
        if not path.is_file():
            return schemas.Response(success=False, message=f"不是文件: {media_file}")
        if not self.__is_video_file(str(path)):
            return schemas.Response(success=False, message=f"不是视频文件: {media_file}")

        parsed = self.__parse_media_context_from_file(path)
        has_subtitle, detail = self.__has_subtitle_detail(path)
        skip_reason = self.__skip_reason_for_media(path, parsed)
        return schemas.Response(
            success=True,
            message="ok",
            data={
                "media_file": str(path),
                "parsed": parsed,
                "has_subtitle": has_subtitle,
                "skip_reason": skip_reason,
                "detail": detail,
            },
        )

    def __download_for_media_file(self, media_file: str, mediainfo: Any, meta: Any) -> Tuple[bool, str]:
        payload = self.__build_search_payload(mediainfo=mediainfo, meta=meta)
        items, message = self.__search_items(payload)
        if not items:
            return False, self.__normalize_failure_message(message, "未找到字幕")

        selected = self.__pick_item(items)
        content, subtitle_format, message, error_data = self.__download_item(selected)
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
                error_data=error_data,
                title=str(payload.get("title") or ""),
                media_type=str(payload.get("type") or ""),
                year=self.__to_int(payload.get("year")),
                season=self.__to_int(payload.get("season")),
                episode=self.__to_int(payload.get("episode")),
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

    def __download_item(self, item: dict) -> Tuple[Optional[bytes], str, Optional[str], Optional[dict]]:
        download_url = item.get("download_url")
        if not download_url:
            return None, "srt", "候选字幕缺少下载地址", None

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
            return None, "srt", f"请求下载接口失败: {str(err)}", None

        if status_code != 200:
            return None, "srt", f"下载接口返回错误: {status_code}", None

        if "application/json" in content_type:
            try:
                body = json.loads(content.decode("utf-8", errors="ignore"))
            except Exception:
                body = None
            if isinstance(body, dict) and "success" in body and body.get("success") is not True:
                error_data = body.get("data") if isinstance(body.get("data"), dict) else None
                return None, "srt", str(body.get("message") or "字幕下载失败"), error_data

        if not content:
            return None, "srt", "下载内容为空", None

        subtitle_format = str(item.get("format") or item.get("subtitle_format") or "srt").lower()
        return content, subtitle_format, None, None

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
        present, _ = self.__has_subtitle_detail(media_file)
        return present

    def __has_subtitle_detail(self, media_file: Path) -> Tuple[bool, Dict[str, Any]]:
        prefix = media_file.stem
        subtitle_prefix = f"{prefix}."
        media_key_variants = self.__subtitle_match_keys(prefix)
        folder_key_variants = self.__subtitle_match_keys(media_file.parent.name)
        is_episode_media = bool(self._season_episode_pattern.search(prefix))
        media_episode_key = self.__season_episode_key(prefix)
        subtitle_candidates: List[Path] = []
        sibling_videos = 0
        debug: Dict[str, Any] = {
            "media_file": str(media_file),
            "media_prefix": prefix,
            "is_episode_media": is_episode_media,
            "media_episode_key": media_episode_key,
            "media_keys": sorted(media_key_variants),
            "folder_keys": sorted(folder_key_variants),
            "subtitle_candidates": [],
            "sibling_videos": 0,
            "matched_by": "",
        }

        for candidate in media_file.parent.iterdir():
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            if self.__is_video_file(str(candidate)):
                sibling_videos += 1
            if suffix not in self._subtitle_suffixes:
                continue
            subtitle_candidates.append(candidate)

            name = candidate.name
            candidate_key_variants = self.__subtitle_match_keys(candidate.stem)
            candidate_episode_key = self.__season_episode_key(candidate.stem)
            debug["subtitle_candidates"].append(
                {
                    "path": str(candidate),
                    "name": name,
                    "episode_key": candidate_episode_key,
                    "keys": sorted(candidate_key_variants),
                }
            )
            if name == f"{prefix}{suffix}" or name.startswith(subtitle_prefix):
                debug["matched_by"] = "exact_name_or_prefix"
                debug["sibling_videos"] = sibling_videos
                return True, debug
            if is_episode_media:
                if media_episode_key and candidate_episode_key and media_episode_key == candidate_episode_key:
                    debug["matched_by"] = "same_episode_key"
                    debug["sibling_videos"] = sibling_videos
                    return True, debug
                continue
            if not media_key_variants:
                continue
            if media_key_variants.intersection(candidate_key_variants) or folder_key_variants.intersection(
                candidate_key_variants
            ):
                debug["matched_by"] = "key_intersection"
                debug["sibling_videos"] = sibling_videos
                return True, debug

        debug["sibling_videos"] = sibling_videos
        if is_episode_media or not subtitle_candidates:
            return False, debug

        # Movie folders are often one title with multiple resolutions; allow shared sidecar subtitles.
        for candidate in subtitle_candidates:
            candidate_key_variants = self.__subtitle_match_keys(candidate.stem)
            if media_key_variants.intersection(candidate_key_variants) or folder_key_variants.intersection(
                candidate_key_variants
            ):
                debug["matched_by"] = "movie_shared_key_intersection"
                return True, debug

        if sibling_videos <= 3:
            debug["matched_by"] = "movie_folder_with_subtitles"
            return True, debug
        return False, debug

    def __season_episode_key(self, value: str) -> str:
        match = self._season_episode_pattern.search(str(value or ""))
        if not match:
            return ""
        season = int(match.group(1))
        episode = int(match.group(2))
        return f"s{season:02d}e{episode:02d}"

    def __subtitle_match_keys(self, raw_name: str) -> set:
        cleaned = self.__clean_title_text(raw_name)
        if not cleaned:
            return set()
        normalized = self.__normalize_subtitle_name_for_match(cleaned)
        if not normalized:
            return set()
        variants = {normalized}
        without_year = re.sub(r"\b(?:19|20)\d{2}\b", " ", normalized)
        without_year = re.sub(r"\s+", " ", without_year).strip()
        if without_year:
            variants.add(without_year)
        first_part = re.split(r"\s+-\s+", normalized, maxsplit=1)[0].strip()
        if first_part:
            variants.add(first_part)
        return variants

    def __normalize_subtitle_name_for_match(self, value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[_\.]+", " ", text)
        text = re.sub(r"[-\u2013\u2014]+", " - ", text)
        text = re.sub(r"\bzh[-_ ]?cn\b", " zhcn ", text)
        text = re.sub(r"\bzh[-_ ]?tw\b", " zhtw ", text)
        text = re.sub(r"\bzh[-_ ]?hans\b", " zhcn ", text)
        text = re.sub(r"\bzh[-_ ]?hant\b", " zhtw ", text)
        filtered_tokens: List[str] = []
        for token in re.split(r"\s+", text):
            item = token.strip()
            if not item:
                continue
            if item in self._subtitle_name_noise_tokens:
                continue
            filtered_tokens.append(item)
        normalized = " ".join(filtered_tokens)
        normalized = re.sub(r"\s*-\s*", " - ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip(" -")
        return normalized

    def __skip_reason_for_media(self, media_file: Path, parsed: Optional[Dict[str, Any]] = None) -> str:
        manual_hit = self.__match_manual_skip_keyword(media_file=media_file, parsed=parsed)
        if manual_hit:
            return f"命中手动跳过关键词: {manual_hit}"

        parsed_ctx = parsed or self.__parse_media_context_from_file(media_file)
        normalized_path = self.__normalize_path(str(media_file))
        if self.__is_chinese_library_path(normalized_path):
            return "中文内容库（国产/华语目录）"
        if self.__is_chinese_by_nfo(media_file=media_file, parsed=parsed_ctx):
            return "媒体NFO标记为中文内容"
        if self.__is_likely_cjk_documentary(media_file=media_file, parsed=parsed_ctx):
            return "中文纪录片目录（规则推断）"
        if self.__is_likely_unclassified_cjk_series(media_file=media_file, parsed=parsed_ctx):
            return "未分类目录下中文剧集（规则推断）"

        probe = self.__probe_media_streams(media_file)
        if probe.get("has_embedded_subtitle"):
            return "媒体已内封字幕"
        if self.__is_anime_library_path(normalized_path) and self.__has_embedded_subtitle_sibling_hint(media_file):
            return "同季样本已识别内封字幕（规则推断）"
        if probe.get("has_chinese_audio"):
            return "媒体含中文音轨"
        return ""

    def __match_manual_skip_keyword(self, media_file: Path, parsed: Optional[Dict[str, Any]] = None) -> str:
        keywords = self.__split_csv(self._manual_skip_keywords)
        if not keywords:
            return ""

        parsed_title = ""
        if isinstance(parsed, dict):
            parsed_title = str(parsed.get("title") or "")

        candidates = [
            self.__normalize_path(str(media_file)),
            self.__normalize_path(str(media_file.parent)),
            self.__clean_title_text(media_file.stem).lower(),
            self.__clean_title_text(media_file.parent.name).lower(),
            self.__clean_title_text(parsed_title).lower(),
        ]

        for raw in keywords:
            raw_text = str(raw or "").strip()
            if not raw_text:
                continue
            normalized = self.__clean_title_text(raw_text).lower()
            if not normalized:
                normalized = raw_text.lower()
            for candidate in candidates:
                if candidate and normalized in candidate:
                    return raw_text
        return ""

    def __is_chinese_library_path(self, normalized_path: str) -> bool:
        if any(marker in normalized_path for marker in self._chinese_library_markers):
            return True

        for segment in [item for item in normalized_path.split("/") if item]:
            if any(keyword in segment for keyword in self._chinese_library_keywords):
                return True
        return False

    def __is_chinese_by_nfo(self, media_file: Path, parsed: Optional[Dict[str, Any]] = None) -> bool:
        cache_key = self.__nfo_cache_key(media_file=media_file, parsed=parsed)
        if cache_key and cache_key in self._nfo_chinese_cache:
            return bool(self._nfo_chinese_cache.get(cache_key))

        result = False
        for nfo in self.__related_nfo_files(media_file=media_file, parsed=parsed):
            text = self.__read_text_file_safe(nfo, max_bytes=512 * 1024)
            if not text:
                continue
            if self.__nfo_indicates_chinese(text):
                result = True
                break

        if cache_key:
            self._nfo_chinese_cache[cache_key] = result
        return result

    def __nfo_cache_key(self, media_file: Path, parsed: Optional[Dict[str, Any]] = None) -> str:
        context = parsed or {}
        media_type = str(context.get("type") or "").lower()
        base_dir = media_file.parent
        if media_type == "series":
            for parent in media_file.parents[:3]:
                if re.match(r"(?i)^season\s*\d+", parent.name):
                    base_dir = parent.parent
                    break
        return self.__normalize_path(str(base_dir))

    def __related_nfo_files(self, media_file: Path, parsed: Optional[Dict[str, Any]] = None) -> List[Path]:
        context = parsed or {}
        media_type = str(context.get("type") or "").lower()
        candidates: List[Path] = []
        seen = set()

        def add_candidate(path: Path) -> None:
            try:
                key = self.__normalize_path(str(path))
                if key in seen:
                    return
                seen.add(key)
                if path.exists() and path.is_file() and path.suffix.lower() == ".nfo":
                    candidates.append(path)
            except Exception:
                return

        add_candidate(media_file.with_suffix(".nfo"))
        add_candidate(media_file.parent / "movie.nfo")
        add_candidate(media_file.parent / "tvshow.nfo")

        if media_type == "series":
            show_root = media_file.parent
            for parent in media_file.parents[:3]:
                if re.match(r"(?i)^season\s*\d+", parent.name):
                    show_root = parent.parent
                    break
            add_candidate(show_root / "tvshow.nfo")
            add_candidate(show_root / f"{show_root.name}.nfo")
            try:
                for path in sorted(show_root.glob("*.nfo"))[:5]:
                    add_candidate(path)
            except Exception:
                pass
        else:
            movie_root = media_file.parent
            add_candidate(movie_root / f"{movie_root.name}.nfo")
            try:
                for path in sorted(movie_root.glob("*.nfo"))[:5]:
                    add_candidate(path)
            except Exception:
                pass

        return candidates

    @staticmethod
    def __read_text_file_safe(path: Path, max_bytes: int = 262_144) -> str:
        try:
            raw = path.read_bytes()[:max_bytes]
        except Exception:
            return ""

        for encoding in ("utf-8", "utf-8-sig", "gb18030", "big5"):
            try:
                return raw.decode(encoding, errors="ignore").lower()
            except Exception:
                continue
        return raw.decode("latin-1", errors="ignore").lower()

    def __nfo_indicates_chinese(self, text: str) -> bool:
        lowered = str(text or "").lower()
        if not lowered:
            return False

        for marker in self._nfo_chinese_country_markers:
            if re.search(rf"<country>[^<]*{re.escape(marker)}[^<]*</country>", lowered):
                return True
            if re.search(rf"<countrycode>[^<]*{re.escape(marker)}[^<]*</countrycode>", lowered):
                return True

        for marker in self._nfo_chinese_language_markers:
            if re.search(rf"<(?:original)?language>[^<]*{re.escape(marker)}[^<]*</(?:original)?language>", lowered):
                return True
            if re.search(rf"<audio(?:language)?>[^<]*{re.escape(marker)}[^<]*</audio(?:language)?>", lowered):
                return True

        return False

    def __is_likely_unclassified_cjk_series(self, media_file: Path, parsed: Optional[Dict[str, Any]] = None) -> bool:
        context = parsed or {}
        if str(context.get("type") or "").lower() != "series":
            return False

        normalized_path = self.__normalize_path(str(media_file))
        if any(marker in normalized_path for marker in self._foreign_library_markers):
            return False

        segments = [segment for segment in normalized_path.split("/") if segment]
        if "tv" not in segments:
            return False
        tv_index = segments.index("tv")
        if tv_index + 2 >= len(segments):
            return False

        show_segment = segments[tv_index + 1]
        season_segment = segments[tv_index + 2]
        if not re.match(r"(?i)^season\s*\d+", season_segment):
            return False
        if not self.__is_mostly_cjk_text(show_segment):
            return False
        return True

    def __is_likely_cjk_documentary(self, media_file: Path, parsed: Optional[Dict[str, Any]] = None) -> bool:
        if not self._auto_skip_cjk_documentary:
            return False

        context = parsed or {}
        if str(context.get("type") or "").lower() != "series":
            return False

        normalized_path = self.__normalize_path(str(media_file))
        if "/tv/纪录片/" not in normalized_path:
            return False
        if any(marker in normalized_path for marker in self._foreign_library_markers):
            return False

        segments = [segment for segment in normalized_path.split("/") if segment]
        if "tv" not in segments:
            return False
        tv_index = segments.index("tv")
        if tv_index + 3 >= len(segments):
            return False

        category = segments[tv_index + 1]
        show_segment = segments[tv_index + 2]
        season_segment = segments[tv_index + 3]
        if category != "纪录片":
            return False
        if not re.match(r"(?i)^season\s*\d+", season_segment):
            return False

        show_name = re.sub(r"\(\d{4}\)", " ", show_segment)
        show_name = re.sub(r"\s+", " ", show_name).strip()
        return self.__is_mostly_cjk_text(show_name)

    def __has_embedded_subtitle_sibling_hint(self, media_file: Path) -> bool:
        season_dir = media_file.parent
        season_key = self.__normalize_path(str(season_dir))
        if season_key in self._season_embedded_hint_cache:
            return bool(self._season_embedded_hint_cache.get(season_key))

        files: List[Path] = []
        try:
            for candidate in sorted(season_dir.iterdir()):
                if not candidate.is_file():
                    continue
                if not self.__is_video_file(str(candidate)):
                    continue
                files.append(candidate)
                if len(files) >= 30:
                    break
        except Exception:
            self._season_embedded_hint_cache[season_key] = False
            return False

        if len(files) < 4:
            self._season_embedded_hint_cache[season_key] = False
            return False

        checked = 0
        embedded_hits = 0
        for candidate in files:
            probe = self.__probe_media_streams(candidate)
            checked += 1
            if probe.get("has_embedded_subtitle"):
                embedded_hits += 1

        hint = checked >= 4 and embedded_hits >= 3 and (embedded_hits / max(checked, 1)) >= 0.35
        self._season_embedded_hint_cache[season_key] = hint
        return hint

    def __is_anime_library_path(self, normalized_path: str) -> bool:
        return any(marker in normalized_path for marker in self._anime_library_markers)

    @staticmethod
    def __is_mostly_cjk_text(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        cjk = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", value)
        if not cjk:
            return False
        latin = re.findall(r"[a-zA-Z]", value)
        # Avoid skipping titles with obvious latin content.
        return len(latin) == 0

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
            ("signature", self.__probe_with_signature),
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
            if result["has_embedded_subtitle"] or result["has_chinese_audio"]:
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

    def __probe_with_signature(self, media_file: Path) -> Optional[Dict[str, bool]]:
        try:
            size = media_file.stat().st_size
            if size <= 0:
                return None
            chunk_size = min(4 * 1024 * 1024, size)
            with media_file.open("rb") as handle:
                head = handle.read(chunk_size)
                tail = b""
                if size > chunk_size * 2:
                    handle.seek(max(0, size - chunk_size))
                    tail = handle.read(chunk_size)
            payload = (head + tail).lower()
            if not payload:
                return None
        except Exception:
            return None

        subtitle_markers = (
            b"tx3g",  # MP4 mov_text
            b"wvtt",  # WebVTT in MP4
            b"stpp",  # TTML in MP4
            b"subrip",
            b"s_text/",
            b"s_ssa",
            b"s_ass",
            b"s_hdmv/pgs",
            b"dvb_subtitle",
            b"hdmv pgs",
            b"vobsub",
        )
        has_subtitle = any(marker in payload for marker in subtitle_markers)
        if not has_subtitle:
            return None

        return {"has_embedded_subtitle": True, "has_chinese_audio": False}

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

    def __resolve_target_file_for_write(
        self,
        *,
        target_file: str,
        title: str = "",
        media_type: str = "",
        year: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> str:
        raw_target = str(target_file or "").strip()
        if not raw_target:
            return ""

        target_path = Path(raw_target)
        if target_path.exists() and target_path.is_file() and self.__is_video_file(str(target_path)):
            return str(target_path)

        if not self.__should_resolve_target_file(raw_target):
            return raw_target

        include_paths = self.__merge_csv_values(self._include_paths)
        roots = self.__collect_scan_roots(directory="", include_paths=include_paths)
        if not roots:
            return raw_target

        expected_type = self.__normalize_media_type(media_type)
        expected_year = self.__to_int(year)
        expected_season = self.__to_int(season)
        expected_episode = self.__to_int(episode)
        expected_title = self.__clean_title_text(title).lower()
        if not expected_title:
            expected_title = self.__clean_title_text(Path(raw_target).stem).lower()

        best_path: Optional[Path] = None
        best_score = 0
        scanned = 0

        for root in roots:
            for candidate in self.__iter_video_files(root, recursive=True):
                scanned += 1
                if scanned > self._target_resolve_scan_limit:
                    break
                parsed = self.__parse_media_context_from_file(candidate, forced_media_type=expected_type)
                score = self.__score_target_candidate(
                    parsed=parsed,
                    expected_title=expected_title,
                    expected_type=expected_type,
                    expected_year=expected_year,
                    expected_season=expected_season,
                    expected_episode=expected_episode,
                )
                if score > best_score:
                    best_score = score
                    best_path = candidate
            if scanned > self._target_resolve_scan_limit:
                break

        if best_path and best_score >= 55:
            logger.info(
                f"[SubtitleAgentBridge] 自动修正 target_file: {raw_target} -> {best_path} (score={best_score})"
            )
            return str(best_path)
        return raw_target

    def __score_target_candidate(
        self,
        *,
        parsed: Dict[str, Any],
        expected_title: str,
        expected_type: str,
        expected_year: Optional[int],
        expected_season: Optional[int],
        expected_episode: Optional[int],
    ) -> int:
        score = 0
        parsed_title = self.__clean_title_text(parsed.get("title") or "").lower()
        parsed_type = self.__normalize_media_type(parsed.get("type"))
        parsed_year = self.__to_int(parsed.get("year"))
        parsed_season = self.__to_int(parsed.get("season"))
        parsed_episode = self.__to_int(parsed.get("episode"))

        if expected_title and parsed_title:
            if parsed_title == expected_title:
                score += 48
            elif expected_title in parsed_title or parsed_title in expected_title:
                score += 30
            else:
                ratio = SequenceMatcher(None, expected_title, parsed_title).ratio()
                score += int(ratio * 24)

        if expected_type:
            if parsed_type == expected_type:
                score += 14
            elif parsed_type:
                score -= 12

        if expected_year:
            if parsed_year == expected_year:
                score += 12
            elif parsed_year:
                score -= 8

        if expected_type == "series":
            if expected_season:
                if parsed_season == expected_season:
                    score += 14
                elif parsed_season:
                    score -= 10
            if expected_episode:
                if parsed_episode == expected_episode:
                    score += 28
                elif parsed_episode:
                    score -= 20

        return score

    def __should_resolve_target_file(self, target_file: str) -> bool:
        normalized = self.__normalize_path(target_file)
        if not normalized:
            return False
        if normalized.startswith("/tmp/") or normalized.startswith("/var/tmp/") or normalized.startswith("/dev/shm/"):
            return True
        name = Path(target_file).name.lower()
        if any(flag in name for flag in ("verifynotif", "telegram", "post-update", "webfix", "user-trigger")):
            return True
        path = Path(target_file)
        return not path.exists()

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
        error_data: Optional[dict] = None,
        title: str = "",
        media_type: str = "",
        year: int = None,
        season: int = None,
        episode: int = None,
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

        preferred = preferred_languages or self.__split_languages(self._languages)
        picked_items = self.__pick_manual_notice_items(items, preferred_languages=preferred)
        if not picked_items:
            return

        captcha_payload = self.__extract_captcha_payload(error_data)
        task_data = self.__create_captcha_task(
            captcha_payload=captcha_payload,
            media_name=media_name,
            target_file=target_file,
            title=title,
            media_type=media_type,
            year=year,
            season=season,
            episode=episode,
        )

        text_lines = [f"媒体: {media_name}"]
        if target_file:
            text_lines.append(f"文件: {target_file}")
        image_url = None
        title = "Subtitle Agent 需手动下载字幕"
        if task_data:
            title = "Subtitle Agent 需要验证码"
            task_id = str(task_data.get("task_id") or "").strip()
            text_lines.append(f"任务ID: {task_id}")
            web_url = str(task_data.get("web_url") or "").strip()
            if web_url:
                text_lines.append(f"网页回填(推荐): {web_url}")
            text_lines.append(f"回复: /subcap {task_id} 图中字母")
            text_lines.append(f"刷新: /subcap {task_id} refresh")
            image_url = str(task_data.get("image_url") or "").strip() or None
            if image_url:
                text_lines.append(f"验证码图: {image_url}")
            else:
                text_lines.append("验证码图: 直链不可用，请打开详情页")
            detail_url = str(task_data.get("detail_url") or "").strip()
            if detail_url:
                text_lines.append(f"详情页: {detail_url}")

        text_lines.append("推荐下载：")
        for index, item in enumerate(picked_items, 1):
            link = self.__manual_item_link(item)
            provider = str(item.get("provider") or "unknown")
            language = str(item.get("language") or "und")
            subtitle_name = str(
                item.get("name")
                or item.get("title")
                or item.get("subtitle_id")
                or "字幕候选"
            )
            text_lines.append(f"{index}. [{provider}/{language}] {subtitle_name}")
            text_lines.append(link)

        self.post_message(
            mtype=NotificationType.Plugin,
            title=title,
            text="\n".join(text_lines),
            image=image_url,
        )

    def __pick_manual_notice_items(
        self,
        items: List[dict],
        *,
        preferred_languages: Optional[List[str]] = None,
    ) -> List[dict]:
        if not items:
            return []

        preferred = preferred_languages or self.__split_languages(self._languages)
        ordered: List[dict] = []
        try:
            ordered.append(self.__pick_item(items, preferred_languages=preferred))
        except Exception:
            pass
        ordered.extend(items)

        selected: List[dict] = []
        seen = set()
        for item in ordered:
            if not isinstance(item, dict):
                continue
            key = self.__manual_item_dedup_key(item)
            if key in seen:
                continue
            if not self.__manual_item_link(item):
                continue
            seen.add(key)
            selected.append(item)
            if len(selected) >= 2:
                break
        return selected

    @staticmethod
    def __provider_family(provider: Any) -> str:
        text = str(provider or "").strip().lower()
        if text in {"subhd", "subhdtw"}:
            return "subhd-family"
        return text or "unknown"

    def __manual_item_link(self, item: dict) -> str:
        raw_link = str(item.get("page_link") or "").strip()
        if raw_link:
            return raw_link
        raw_download = str(item.get("download_url") or "").strip()
        if raw_download:
            return self.__compose_url(raw_download)
        return ""

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
        lowered = text.lower()
        if "<svg" in lowered and "</svg>" in lowered:
            return default
        if "captcha challenge not found or expired" in lowered:
            return "验证码任务不存在或已过期，请重新触发下载任务"
        if (
            "subhd captcha validation failed" in lowered
            or "captcha validation failed" in lowered
            or "captcha code is required" in lowered
            or "验证码校验失败" in text
        ):
            return "验证码错误，请按最新验证码图重试"
        if (
            "subhd captcha expired or invalid" in lowered
            or "temporary page expired" in lowered
            or "page expired" in lowered
            or "临时页面已经失效" in text
            or "页面已经失效" in text
            or "时间过长" in text
        ):
            return "验证码错误或已过期，请按最新验证码图重试"
        return text

    def __is_duplicate_captcha_submit(
        self,
        *,
        task_id: str,
        code: str,
        message_context: Optional[Dict[str, Any]],
    ) -> bool:
        normalized_task_id = str(task_id or "").strip().lower()
        normalized_code = str(code or "").strip().lower()
        if not normalized_task_id or not normalized_code:
            return False

        channel = ""
        userid = ""
        if isinstance(message_context, dict):
            channel = str(message_context.get("channel") or "").strip().lower()
            userid = str(message_context.get("userid") or "").strip().lower()
        dedup_key = f"{channel}|{userid}|{normalized_task_id}|{normalized_code}"
        now = time.monotonic()

        with self._recent_captcha_submit_lock:
            stale = [key for key, ts in self._recent_captcha_submits.items() if (now - ts) > 30.0]
            for key in stale:
                self._recent_captcha_submits.pop(key, None)

            last = self._recent_captcha_submits.get(dedup_key)
            if last is not None and (now - last) <= self._captcha_submit_dedup_seconds:
                return True

            self._recent_captcha_submits[dedup_key] = now
        return False

    @staticmethod
    def __extract_captcha_payload(error_data: Any) -> Optional[dict]:
        if not isinstance(error_data, dict):
            return None
        captcha = error_data.get("captcha")
        return captcha if isinstance(captcha, dict) else None

    def __captcha_task_response_data(
        self,
        *,
        error_data: Optional[dict],
        media_name: str,
        target_file: str,
        title: str = "",
        media_type: str = "",
        year: int = None,
        season: int = None,
        episode: int = None,
    ) -> Optional[dict]:
        captcha_payload = self.__extract_captcha_payload(error_data)
        if not captcha_payload:
            return None
        task_data = self.__create_captcha_task(
            captcha_payload=captcha_payload,
            media_name=media_name,
            target_file=target_file,
            title=title,
            media_type=media_type,
            year=year,
            season=season,
            episode=episode,
        )
        if not task_data:
            return None
        return {
            "captcha_task_id": task_data.get("task_id"),
            "challenge_id": task_data.get("challenge_id"),
            "image_url": task_data.get("image_url"),
            "detail_url": task_data.get("detail_url"),
            "web_url": task_data.get("web_url"),
            "reply_format": f"/subcap {task_data.get('task_id')} 图中字母",
        }

    def __create_captcha_task(
        self,
        *,
        captcha_payload: Optional[dict],
        media_name: str,
        target_file: str,
        title: str = "",
        media_type: str = "",
        year: int = None,
        season: int = None,
        episode: int = None,
    ) -> Optional[dict]:
        if not isinstance(captcha_payload, dict):
            return None

        challenge_id = str(captcha_payload.get("challenge_id") or "").strip()
        if not challenge_id:
            return None

        tasks = self.__load_captcha_tasks()
        for task_id, task in tasks.items():
            if str(task.get("challenge_id") or "").strip() == challenge_id:
                task["media_name"] = media_name
                task["target_file"] = target_file
                task["title"] = str(title or task.get("title") or "").strip()
                task["media_type"] = self.__normalize_media_type(media_type) or str(task.get("media_type") or "").strip()
                task["year"] = self.__to_int(year) or self.__to_int(task.get("year"))
                task["season"] = self.__to_int(season) or self.__to_int(task.get("season"))
                task["episode"] = self.__to_int(episode) or self.__to_int(task.get("episode"))
                detail_url = str(captcha_payload.get("detail_url") or "").strip()
                if detail_url:
                    task["detail_url"] = detail_url
                image_path = str(captcha_payload.get("image_path") or "").strip()
                if image_path:
                    task["image_path"] = image_path
                task["image_available"] = bool(captcha_payload.get("image_available"))
                task["web_token"] = str(task.get("web_token") or "").strip() or uuid4().hex
                task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.__save_captcha_tasks(tasks)
                task["task_id"] = task_id
                task["image_url"] = (
                    self.__compose_url(str(task.get("image_path") or ""))
                    if bool(task.get("image_available")) and str(task.get("image_path") or "").strip()
                    else ""
                )
                task["web_url"] = self.__compose_web_url(
                    self.__build_captcha_web_path(task["web_token"])
                )
                return task

        task_id = uuid4().hex[:8]
        image_path = str(captcha_payload.get("image_path") or "").strip()
        image_available = bool(captcha_payload.get("image_available"))
        web_token = uuid4().hex
        task = {
            "challenge_id": challenge_id,
            "media_name": media_name,
            "target_file": target_file,
            "title": str(title or "").strip(),
            "media_type": self.__normalize_media_type(media_type),
            "year": self.__to_int(year),
            "season": self.__to_int(season),
            "episode": self.__to_int(episode),
            "provider": str(captcha_payload.get("provider") or "").strip(),
            "subtitle_id": str(captcha_payload.get("subtitle_id") or "").strip(),
            "image_path": image_path,
            "image_available": image_available,
            "web_token": web_token,
            "detail_url": str(captcha_payload.get("detail_url") or "").strip(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        tasks[task_id] = task
        self.__save_captcha_tasks(tasks)
        task["task_id"] = task_id
        task["image_url"] = self.__compose_url(image_path) if (image_path and image_available) else ""
        task["web_url"] = self.__compose_web_url(self.__build_captcha_web_path(web_token))
        return task

    def __find_captcha_task_id_by_web_token(self, token: str) -> str:
        normalized = str(token or "").strip().lower()
        if not normalized:
            return ""
        tasks = self.__load_captcha_tasks()
        for task_id, payload in tasks.items():
            if not isinstance(payload, dict):
                continue
            if str(payload.get("web_token") or "").strip().lower() == normalized:
                return str(task_id or "").strip().lower()
        return ""

    def __captcha_task_view_data(self, *, task_id: str, task: Optional[dict], apikey: str = "") -> Optional[dict]:
        if not isinstance(task, dict):
            return None
        normalized_task_id = str(task_id or "").strip().lower()
        web_token = str(task.get("web_token") or "").strip()
        if not web_token and normalized_task_id:
            web_token = uuid4().hex
            tasks = self.__load_captcha_tasks()
            existing = tasks.get(normalized_task_id)
            if isinstance(existing, dict):
                existing["web_token"] = web_token
                tasks[normalized_task_id] = existing
                self.__save_captcha_tasks(tasks)
                task = existing
        image_path = str(task.get("image_path") or "").strip()
        image_available = bool(task.get("image_available"))
        image_url = self.__compose_url(image_path) if (image_available and image_path) else ""
        web_url = self.__compose_web_url(self.__build_captcha_web_path(web_token)) if web_token else ""
        return {
            "captcha_task_id": normalized_task_id,
            "challenge_id": str(task.get("challenge_id") or "").strip(),
            "image_url": image_url,
            "detail_url": str(task.get("detail_url") or "").strip(),
            "web_url": web_url,
            "reply_format": f"/subcap {normalized_task_id} 图中字母",
            "token": web_token,
            "apikey": str(apikey or settings.API_TOKEN or ""),
            "media_name": str(task.get("media_name") or "").strip(),
            "target_file": str(task.get("target_file") or "").strip(),
        }

    def __render_captcha_web_html(
        self,
        *,
        task_data: Optional[dict],
        result_text: str = "",
        result_ok: bool = False,
    ) -> str:
        safe_result = escape(str(result_text or "").strip())
        result_block = ""
        if safe_result:
            color = "#16a34a" if result_ok else "#b91c1c"
            result_block = (
                f'<div style="margin:12px 0;padding:10px 12px;border-radius:8px;'
                f'background:#f8fafc;border:1px solid #e2e8f0;color:{color};font-size:14px;">{safe_result}</div>'
            )

        if not isinstance(task_data, dict):
            return (
                "<!doctype html><html><head><meta charset='utf-8'><title>Subtitle Agent 验证码</title></head>"
                "<body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;padding:20px;'>"
                "<h2 style='margin:0 0 12px;'>Subtitle Agent 验证码</h2>"
                f"{result_block or '<p>验证码任务不存在或已过期。</p>'}"
                "</body></html>"
            )

        token = escape(str(task_data.get("token") or ""))
        apikey = escape(str(task_data.get("apikey") or ""))
        media_name = escape(str(task_data.get("media_name") or "未知媒体"))
        task_id = escape(str(task_data.get("captcha_task_id") or ""))
        image_url = str(task_data.get("image_url") or "").strip()
        detail_url = str(task_data.get("detail_url") or "").strip()
        target_file = escape(str(task_data.get("target_file") or ""))

        image_block = ""
        if image_url:
            image_src = escape(f"{image_url}?_ts={int(time.time())}")
            image_block = (
                f"<img src='{image_src}' alt='captcha' "
                "style='max-width:420px;width:100%;border:1px solid #e2e8f0;border-radius:8px;background:#fff;'/>"
            )
        else:
            image_block = "<div style='color:#475569;'>当前验证码图直链不可用，请打开详情页查看验证码。</div>"

        detail_block = ""
        if detail_url:
            detail_block = (
                f"<div style='margin-top:8px;'><a href='{escape(detail_url)}' target='_blank'>打开 SubHD 详情页查看验证码</a></div>"
            )

        return f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Subtitle Agent 验证码回填</title>
</head>
<body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;padding:18px;max-width:720px;margin:0 auto;'>
  <h2 style='margin:0 0 10px;'>Subtitle Agent 验证码回填</h2>
  <div style='color:#334155;font-size:14px;line-height:1.6;'>
    <div>媒体: {media_name}</div>
    <div>任务ID: {task_id}</div>
    <div>目标文件: {target_file or '-'}</div>
  </div>
  {result_block}
  <div style='margin:14px 0;'>{image_block}{detail_block}</div>
  <form method='get' action='' style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;'>
    <input type='hidden' name='token' value='{token}' />
    <input type='hidden' name='apikey' value='{apikey}' />
    <input type='text' name='code' placeholder='输入验证码字母' autocomplete='off'
      style='flex:1;min-width:180px;padding:10px;border:1px solid #cbd5e1;border-radius:8px;' />
    <button type='submit' style='padding:10px 14px;border:0;border-radius:8px;background:#2563eb;color:#fff;'>提交验证码</button>
  </form>
  <form method='get' action='' style='margin-top:10px;'>
    <input type='hidden' name='token' value='{token}' />
    <input type='hidden' name='apikey' value='{apikey}' />
    <input type='hidden' name='action' value='refresh' />
    <button type='submit' style='padding:9px 13px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;'>刷新验证码</button>
  </form>
  <div style='margin-top:12px;color:#475569;font-size:13px;'>也可在 Telegram 回复: /subcap {task_id} 图中字母</div>
</body>
</html>"""

    def __load_captcha_tasks(self) -> Dict[str, Dict[str, Any]]:
        self.__cleanup_captcha_tasks()
        raw = self.get_data("captcha_tasks") or {}
        if not isinstance(raw, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for task_id, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            key = str(task_id or "").strip()
            if key:
                clean[key] = dict(payload)
        return clean

    def __save_captcha_tasks(self, tasks: Dict[str, Dict[str, Any]]) -> None:
        self.save_data("captcha_tasks", tasks)

    def __cleanup_captcha_tasks(self) -> None:
        raw = self.get_data("captcha_tasks") or {}
        if not isinstance(raw, dict):
            return

        now = datetime.now()
        valid: Dict[str, Dict[str, Any]] = {}
        changed = False
        for task_id, payload in raw.items():
            if not isinstance(payload, dict):
                changed = True
                continue
            created_text = str(payload.get("created_at") or "").strip()
            try:
                created_at = datetime.strptime(created_text, "%Y-%m-%d %H:%M:%S")
            except Exception:
                changed = True
                continue
            if (now - created_at) > timedelta(hours=self._captcha_task_ttl_hours):
                changed = True
                continue
            valid[str(task_id)] = dict(payload)

        if changed:
            self.__save_captcha_tasks(valid)

    def __load_manual_jobs(self) -> Dict[str, Dict[str, Any]]:
        raw = self.get_data("manual_jobs") or {}
        if not isinstance(raw, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for job_id, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            key = str(job_id or "").strip()
            if not key:
                continue
            item = dict(payload)
            item["job_id"] = key
            clean[key] = item
        return clean

    def __save_manual_jobs(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        self.save_data("manual_jobs", jobs)

    def __cleanup_manual_jobs(self) -> None:
        raw = self.get_data("manual_jobs") or {}
        if not isinstance(raw, dict):
            return
        now = datetime.now()
        valid: Dict[str, Dict[str, Any]] = {}
        changed = False
        for job_id, payload in raw.items():
            if not isinstance(payload, dict):
                changed = True
                continue
            created_text = str(payload.get("created_at") or "").strip()
            try:
                created_at = datetime.strptime(created_text, "%Y-%m-%d %H:%M:%S")
            except Exception:
                changed = True
                continue
            if (now - created_at) > timedelta(hours=self._manual_job_ttl_hours):
                changed = True
                continue
            valid[str(job_id)] = dict(payload)
        if changed:
            self.__save_manual_jobs(valid)

    def __save_manual_job(self, *, job_id: str, payload: Dict[str, Any]) -> None:
        normalized = str(job_id or "").strip()
        if not normalized:
            return
        with self._manual_job_lock:
            self.__cleanup_manual_jobs()
            jobs = self.__load_manual_jobs()
            item = dict(payload or {})
            item["job_id"] = normalized
            jobs[normalized] = item
            self.__save_manual_jobs(jobs)

    def __update_manual_job(self, *, job_id: str, status: str, message: str, result_data: Optional[dict] = None) -> None:
        normalized = str(job_id or "").strip()
        if not normalized:
            return
        with self._manual_job_lock:
            self.__cleanup_manual_jobs()
            jobs = self.__load_manual_jobs()
            payload = jobs.get(normalized)
            if not isinstance(payload, dict):
                return
            payload["status"] = str(status or "").strip() or "running"
            payload["message"] = str(message or "").strip()
            payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(result_data, dict):
                payload["result_data"] = dict(result_data)
            jobs[normalized] = payload
            self.__save_manual_jobs(jobs)

    def __submit_captcha_task(
        self,
        *,
        task_id: str,
        code: str,
        message_context: Optional[Dict[str, Any]] = None,
    ) -> schemas.Response:
        normalized_task_id = str(task_id or "").strip().lower()
        normalized_code = str(code or "").strip()
        if not normalized_task_id or not normalized_code:
            response = schemas.Response(success=False, message="缺少 task_id 或 code")
            if message_context:
                self.__post_message_to_context(
                    title="Subtitle Agent 验证码失败",
                    text=response.message,
                    message_context=message_context,
                )
            return response

        tasks = self.__load_captcha_tasks()
        task = tasks.get(normalized_task_id)
        if not isinstance(task, dict):
            response = schemas.Response(success=False, message="验证码任务不存在或已过期")
            if message_context:
                self.__post_message_to_context(
                    title="Subtitle Agent 验证码失败",
                    text=response.message,
                    message_context=message_context,
                )
            return response

        if normalized_code.lower() in self._refresh_code_keywords:
            response = self.__refresh_captcha_task(task_id=normalized_task_id)
            if message_context:
                lines = [str(response.message or "验证码已刷新，请填写新验证码")]
                if isinstance(response.data, dict):
                    web_url = str(response.data.get("web_url") or "").strip()
                    image_url = str(response.data.get("image_url") or "").strip()
                    detail_url = str(response.data.get("detail_url") or "").strip()
                    if web_url:
                        lines.append(f"网页回填：{web_url}")
                    if image_url:
                        lines.append(f"验证码图：{image_url}")
                    else:
                        lines.append("验证码图：当前直链不可用，请打开详情页查看验证码")
                    if detail_url:
                        lines.append(f"详情页：{detail_url}")
                self.__post_message_to_context(
                    title="Subtitle Agent 验证码已刷新",
                    text="\n".join(lines),
                    message_context=message_context,
                    image=str((response.data or {}).get("image_url") or "").strip() or None if isinstance(response.data, dict) else None,
                )
            return response

        content, subtitle_format, message, error_data = self.__solve_captcha_download(
            challenge_id=str(task.get("challenge_id") or ""),
            code=normalized_code,
        )
        if not content:
            refreshed = self.__extract_captcha_payload(error_data)
            if refreshed:
                task["challenge_id"] = str(refreshed.get("challenge_id") or task.get("challenge_id") or "").strip()
                task["image_path"] = str(refreshed.get("image_path") or task.get("image_path") or "").strip()
                task["image_available"] = bool(refreshed.get("image_available"))
                task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                tasks[normalized_task_id] = task
                self.__save_captcha_tasks(tasks)

            failure_message = self.__normalize_failure_message(message, "验证码提交失败")
            response = schemas.Response(
                success=False,
                message=failure_message,
                data=self.__captcha_task_response_data(
                    error_data=error_data,
                    media_name=str(task.get("media_name") or "未知媒体"),
                    target_file=str(task.get("target_file") or ""),
                    title=str(task.get("title") or ""),
                    media_type=str(task.get("media_type") or ""),
                    year=self.__to_int(task.get("year")),
                    season=self.__to_int(task.get("season")),
                    episode=self.__to_int(task.get("episode")),
                ),
            )
            if message_context:
                image_url = ""
                detail_url = ""
                web_url = ""
                if isinstance(response.data, dict):
                    image_url = str(response.data.get("image_url") or "").strip()
                    detail_url = str(response.data.get("detail_url") or "").strip()
                    web_url = str(response.data.get("web_url") or "").strip()
                retry_lines = [
                    failure_message,
                    f"请重新回复：/subcap {normalized_task_id} 新验证码",
                    "注意: 旧验证码会失效，请按本条消息中的最新验证码图回复",
                ]
                if web_url:
                    retry_lines.append(f"网页回填：{web_url}")
                if image_url:
                    retry_lines.append(f"验证码图：{image_url}")
                else:
                    retry_lines.append("验证码图：当前直链不可用，请打开详情页查看验证码")
                if detail_url:
                    retry_lines.append(f"详情页：{detail_url}")
                self.__post_message_to_context(
                    title="Subtitle Agent 验证码失败",
                    text="\n".join(retry_lines),
                    message_context=message_context,
                    image=image_url or None,
                )
            return response

        target_file = self.__resolve_target_file_for_write(
            target_file=str(task.get("target_file") or "").strip(),
            title=str(task.get("title") or ""),
            media_type=str(task.get("media_type") or ""),
            year=self.__to_int(task.get("year")),
            season=self.__to_int(task.get("season")),
            episode=self.__to_int(task.get("episode")),
        )
        if target_file:
            task["target_file"] = target_file
            tasks[normalized_task_id] = task
            self.__save_captcha_tasks(tasks)
        if not target_file:
            tasks.pop(normalized_task_id, None)
            self.__save_captcha_tasks(tasks)
            response = schemas.Response(
                success=True,
                message="验证码通过，字幕已下载（未写入文件，因 target_file 为空）",
                data={"size": len(content)},
            )
            if message_context:
                self.__post_message_to_context(
                    title="Subtitle Agent 验证码通过",
                    text=response.message,
                    message_context=message_context,
                )
            return response

        subtitle_path = self.__build_subtitle_path(target_file, subtitle_format)
        subtitle_file = Path(subtitle_path)
        sync_note = ""
        try:
            subtitle_file.parent.mkdir(parents=True, exist_ok=True)
            content, sync_note = self.__maybe_auto_sync_timing(
                content=content,
                subtitle_format=subtitle_format,
                media_file=Path(target_file),
                subtitle_file=subtitle_file,
            )
            subtitle_file.write_bytes(content)
        except Exception as err:
            response = schemas.Response(success=False, message=f"写入字幕失败: {str(err)}")
            if message_context:
                self.__post_message_to_context(
                    title="Subtitle Agent 验证码失败",
                    text=response.message,
                    message_context=message_context,
                )
            return response

        tasks.pop(normalized_task_id, None)
        self.__save_captcha_tasks(tasks)
        response_message = f"字幕下载完成: {subtitle_path}"
        if sync_note:
            response_message = f"{response_message}（{sync_note}）"
        response = schemas.Response(
            success=True,
            message=response_message,
            data={"path": subtitle_path, "size": len(content), "sync": sync_note},
        )
        if message_context:
            self.__post_message_to_context(
                title="Subtitle Agent 验证码通过",
                text=f"媒体: {task.get('media_name')}\n{response_message}",
                message_context=message_context,
            )
        return response

    def __refresh_captcha_task(self, *, task_id: str) -> schemas.Response:
        normalized_task_id = str(task_id or "").strip().lower()
        tasks = self.__load_captcha_tasks()
        task = tasks.get(normalized_task_id)
        if not isinstance(task, dict):
            return schemas.Response(success=False, message="验证码任务不存在或已过期")

        _, _, message, error_data = self.__solve_captcha_download(
            challenge_id=str(task.get("challenge_id") or ""),
            code="zzzz",
        )
        refreshed = self.__extract_captcha_payload(error_data)
        if refreshed:
            task["challenge_id"] = str(refreshed.get("challenge_id") or task.get("challenge_id") or "").strip()
            task["image_path"] = str(refreshed.get("image_path") or task.get("image_path") or "").strip()
            task["image_available"] = bool(refreshed.get("image_available"))
            task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tasks[normalized_task_id] = task
            self.__save_captcha_tasks(tasks)

            data = self.__captcha_task_response_data(
                error_data=error_data,
                media_name=str(task.get("media_name") or "未知媒体"),
                target_file=str(task.get("target_file") or ""),
                title=str(task.get("title") or ""),
                media_type=str(task.get("media_type") or ""),
                year=self.__to_int(task.get("year")),
                season=self.__to_int(task.get("season")),
                episode=self.__to_int(task.get("episode")),
            )
            if not isinstance(data, dict):
                data = self.__captcha_task_view_data(
                    task_id=normalized_task_id,
                    task=tasks.get(normalized_task_id),
                )
            return schemas.Response(success=False, message="验证码已刷新，请填写最新验证码", data=data)

        fallback_data = self.__captcha_task_view_data(task_id=normalized_task_id, task=task)
        fallback_message = self.__normalize_failure_message(message, "验证码刷新失败，请稍后重试")
        return schemas.Response(success=False, message=fallback_message, data=fallback_data)

    def __solve_captcha_download(
        self,
        *,
        challenge_id: str,
        code: str,
    ) -> Tuple[Optional[bytes], str, Optional[str], Optional[dict]]:
        solve_url = self.__compose_url("/api/v1/subtitles/captcha/solve")
        body_bytes = json.dumps(
            {
                "challenge_id": challenge_id,
                "code": code,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            request = Request(
                url=solve_url,
                data=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                },
                method="POST",
            )
            with urlopen(request, timeout=self._timeout) as res:
                status_code = int(getattr(res, "status", 200))
                content_type = str(res.headers.get("Content-Type") or "").lower()
                content_disposition = str(res.headers.get("Content-Disposition") or "")
                content = res.read()
        except Exception as err:
            return None, "srt", f"请求验证码接口失败: {str(err)}", None

        if status_code != 200:
            return None, "srt", f"验证码接口返回错误: {status_code}", None

        if "application/json" in content_type:
            try:
                body = json.loads(content.decode("utf-8", errors="ignore"))
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("success") is not True:
                error_data = body.get("data") if isinstance(body.get("data"), dict) else None
                return None, "srt", str(body.get("message") or "验证码处理失败"), error_data

        if not content:
            return None, "srt", "验证码接口返回空内容", None

        subtitle_format = self.__subtitle_format_from_response(
            content_disposition=content_disposition,
            content_type=content_type,
            fallback="srt",
        )
        return content, subtitle_format, None, None

    @staticmethod
    def __subtitle_format_from_response(
        *,
        content_disposition: str,
        content_type: str,
        fallback: str = "srt",
    ) -> str:
        filename_match = re.search(r"filename\\*?=(?:UTF-8''|\"?)([^\";]+)", content_disposition, re.IGNORECASE)
        if filename_match:
            suffix = Path(filename_match.group(1)).suffix.lower().lstrip(".")
            if suffix:
                return suffix
        if "subrip" in content_type:
            return "srt"
        if "ass" in content_type:
            return "ass"
        return fallback

    @staticmethod
    def __extract_message_context(event_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "channel": event_data.get("channel") or event_data.get("source"),
            "userid": event_data.get("userid") or event_data.get("user") or event_data.get("user_id"),
        }

    @staticmethod
    def __extract_user_message_text(event_data: Dict[str, Any]) -> str:
        for key in ("text", "content", "message"):
            value = event_data.get(key)
            if value:
                return str(value).strip()
        return ""

    @staticmethod
    def __extract_command_args(command: str, arg_str: str, fallback_text: str) -> str:
        text = str(arg_str or "").strip()
        if text:
            return text
        fallback = str(fallback_text or "").strip()
        if not fallback:
            return ""
        return re.sub(rf"^\s*/?{re.escape(command)}\b", "", fallback, flags=re.IGNORECASE).strip()

    @staticmethod
    def __parse_captcha_reply(text: str) -> Optional[Tuple[str, str]]:
        matched = re.match(r"^\s*/?subcap(?:tcha)?\s+([A-Za-z0-9]{4,32})\s+([A-Za-z0-9]{3,16})\s*$", text, re.I)
        if not matched:
            return None
        return matched.group(1).lower(), matched.group(2)

    def __post_message_to_context(
        self,
        *,
        title: str,
        text: str,
        message_context: Optional[Dict[str, Any]],
        image: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "mtype": NotificationType.Plugin,
            "title": title,
            "text": text,
        }
        if image:
            payload["image"] = image
        if isinstance(message_context, dict):
            channel = message_context.get("channel")
            userid = message_context.get("userid")
            if channel:
                payload["channel"] = channel
            if userid:
                payload["userid"] = userid
        self.post_message(**payload)

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

    def __compose_web_url(self, path: str) -> str:
        value = str(path or "").strip()
        if value.startswith("http://") or value.startswith("https://"):
            return value
        base = str(self._web_base_url or self._runtime_web_base_url or "").strip()
        if not base:
            return value
        return urljoin(f"{base}/", value.lstrip("/"))

    def __build_captcha_web_path(self, web_token: str) -> str:
        token = str(web_token or "").strip()
        if not token:
            return "/api/v1/plugin/SubtitleAgentBridge/captcha_web"
        return f"/api/v1/plugin/SubtitleAgentBridge/captcha_web?token={token}&apikey={settings.API_TOKEN}"

    def __remember_web_base_url(self, request: Optional[FastAPIRequest]) -> None:
        if self._web_base_url:
            return
        if request is None:
            return
        try:
            host = str((request.headers or {}).get("host") or "").strip()
            if not host:
                return
            scheme = str(getattr(request.url, "scheme", "") or "").strip() or "http"
            runtime = self.__normalize_host(f"{scheme}://{host}")
            if runtime:
                self._runtime_web_base_url = runtime
        except Exception:
            return
