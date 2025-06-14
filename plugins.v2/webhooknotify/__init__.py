from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.schemas import NotificationType
from app import schemas


class WebHookNotify(_PluginBase):
    # 插件名称
    plugin_name = "Webhook通知"
    # 插件描述
    plugin_desc = "接收webhook通知并推送。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/webhook.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "Origin1699"
    # 作者主页
    author_url = "https://github.com/Origin1699"
    # 插件配置项ID前缀
    plugin_config_prefix = "webhooknotify_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 任务执行间隔
    _enabled = False
    _notify = False
    _msgtype = None
    _title = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._msgtype = config.get("msgtype")
            self._title = config.get("title")

    def send_notify(self, request: Request):
        # 1. 先尝试从 URL 查询参数获取 text
        query_text = request.query_params.get("text")

        # 2. 再尝试从 body 读取原始文本（不是 JSON 的键值）
        try:
            body_text = (await request.body()).decode("utf-8").strip()
        except Exception:
            body_text = ""

        # 3. 优先使用 body 的内容，再 fallback 到 query 参数
        text = body_text or query_text or ""
        """
        发送通知
        """
        logger.info(f"收到webhook消息啦。。。  {text}")
        if self._enabled and self._notify:
            mtype = NotificationType.Manual
            if self._msgtype:
                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
            self.post_message(title=self._title,
                              mtype=mtype,
                              text=text)

        return schemas.Response(
            success=True,
            message="发送成功"
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def NotificationType() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [{
            "path": "/webhook",
            "endpoint": self.send_notify,
            "methods": ["GET","POST"],
            "summary": "webhook通知",
            "description": "接受webhook通知并推送",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 编历 NotificationType 枚举，生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'msgtype',
                                            'label': '消息类型',
                                            'items': MsgTypeOptions
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'title',
                                            'label': '通知标题',
                                            'placeholder': '',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '群辉webhook配置http://ip:3001/api/v1/plugin/WebHookNotify/webhook?apikey=*****&text=hello world。'
                                                    'text参数类型是消息内容。此插件安装完需要重启生效api。消息类型默认为手动处理通知。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '如安装完插件后，群晖发送webhook提示404，重启MoviePilot即可。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                         'component': 'VRow',
                         'content': [
                             {
                                 'component': 'VCol',
                                 'props': {
                                     'cols': 12,
                                 },
                                 'content': [
                                     {
                                         'component': 'VAlert',
                                         'props': {
                                             'type': 'info',
                                             'variant': 'tonal',
                                             'text': '由thsrite佬群晖WebHook插件改编而来, 感谢thsrite佬。'
                                         }
                                     }
                                 ]
                             }
                         ]
                     }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "msgtype": "",
            "title": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass