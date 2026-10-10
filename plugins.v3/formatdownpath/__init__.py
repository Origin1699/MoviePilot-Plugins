import re
import threading
from abc import ABCMeta, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel

from app import schemas
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.modules.filemanager.transhandler import TransHandler
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.schemas.types import EventType, MediaType, SystemConfigKey
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import Context, MediaInfo, MetaBase, MetaInfo, MetaInfoPath, resolve_media_identity
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper


@dataclass
class TorrentFile:
    """
    文件列表
    """
    # 文件名称(包含子文件夹路径和文件后缀)
    name: str
    # 文件大小
    size: int
    # 文件优先级
    priority: int = 0


@dataclass
class TorrentInfo:
    """
    种子信息
    """
    # 种子名称
    name: str
    # 种子保存路径
    save_path: str
    # 种子大小
    total_size: int
    # 种子哈希
    hash: str
    # Torrent 自动管理
    auto_tmm: bool = False
    # 种子分类
    category: str = ""
    # 种子标签
    tags: list[str] = field(default_factory=list)
    # 种子文件列表
    files: list[TorrentFile] = field(default_factory=list)

    @classmethod
    def from_backup(cls, data: dict[str, Any]) -> "TorrentInfo":
        """
        从插件数据中的备份恢复种子信息
        """
        return cls(**{
            k: v if k != "files" else [TorrentFile(**f) for f in v]
            for k, v in data.items()
        })


class RecoverRequest(BaseModel):
    """
    从备份恢复种子的请求体
    """
    # 种子哈希
    torrent_hash: str
    # 下载器名称
    downloader: str


class TorrentBackupSummary(BaseModel):
    """
    种子备份摘要，供数据页展示
    """
    # 种子名称
    name: str
    # 文件数量
    files_count: int
    # 原保存路径
    save_path: str


class Downloader(metaclass=ABCMeta):
    @abstractmethod
    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        """
        设置种子自动管理(仅qBittorrent支持)
        :param torrent_hash: 种子hash
        :param enable: 是否开启自动管理
        """
        pass

    @abstractmethod
    def set_torrent_save_path(self, torrent_hash: str, location: str, move: bool = True) -> None:
        """
        设置种子保存路径
        :param torrent_hash: 种子hash
        :param location: 路径字符串(绝对路径)
        :param move: 是否移动种子文件(仅transmission有效)
        """
        pass

    @abstractmethod
    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        """
        重命名种子名称(仅qBittorrent支持)
        :param torrent_hash: 种子hash
        :param old_path: 原路径
        :param new_torrent_name: 新种子名称
        """
        pass

    @abstractmethod
    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        """
        重命名种子文件
        :param torrent_hash: 种子hash
        :param old_path: 原路径
        :param new_path: 新路径
        """
        pass

    @abstractmethod
    def torrents_info(self, torrent_hash: Optional[str] = None) -> list[TorrentInfo]:
        """
        获取种子信息
        :param torrent_hash: 种子hash
        """
        pass


class QbittorrentDownloader(Downloader):
    def __init__(self, qbc: Qbittorrent):
        self.qbc = qbc.qbc

    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        self.qbc.torrents_set_auto_management(torrent_hashes=torrent_hash, enable=enable)

    def set_torrent_save_path(self, torrent_hash: str, location: str, move: bool = True) -> None:
        self.qbc.torrents_set_location(torrent_hashes=torrent_hash, location=location)

    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        self.qbc.torrents_rename(torrent_hash=torrent_hash, new_torrent_name=new_torrent_name)

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self.qbc.torrents_rename_file(torrent_hash=torrent_hash, old_path=old_path, new_path=new_path)

    def torrents_info(self, torrent_hash: Optional[str] = None) -> list[TorrentInfo]:
        """
        获取种子信息
        """
        torrents = []
        torrents_info = self.qbc.torrents_info(torrent_hashes=torrent_hash) if torrent_hash else self.qbc.torrents_info()
        if torrents_info:
            for torrent_info in torrents_info:
                torrents.append(TorrentInfo(
                    name=torrent_info.get('name'),
                    save_path=Path(torrent_info.get('save_path')).as_posix(),
                    total_size=torrent_info.get('total_size'),
                    hash=torrent_info.get('hash'),
                    auto_tmm=torrent_info.get('auto_tmm'),
                    category=torrent_info.get('category'),
                    # qBittorrent 以 ", " 分隔标签
                    tags=[tag.strip() for tag in (torrent_info.get('tags') or "").split(",") if tag.strip()],
                    files=[
                        TorrentFile(
                            name=file.get('name'),
                            size=file.get('size'),
                            priority=file.get('priority'))
                        for file in torrent_info.files
                    ]
                ))

        return torrents


class TransmissionDownloader(Downloader):
    def __init__(self, trc: Transmission):
        self.trc = trc.trc

    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        """
        transmission_rpc 没有`Torrent自动管理`功能
        """
        pass

    def set_torrent_save_path(self, torrent_hash: str, location: str, move: bool = True) -> None:
        self.trc.move_torrent_data(ids=torrent_hash, location=location, move=move)

    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        """
        transmission_rpc 没有`重命名种子`功能
        """
        pass

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self.trc.rename_torrent_path(torrent_id=torrent_hash, location=old_path, name=new_path)

    def torrents_info(self, torrent_hash: Optional[str] = None) -> list[TorrentInfo]:
        torrents = []
        try:
            if torrent_hash:
                # 单个种子查询需要异常处理
                try:
                    torrent_info = self.trc.get_torrent(torrent_id=torrent_hash)
                    torrents_info = [torrent_info] if torrent_info else []
                except KeyError:
                    # 种子未找到，返回空列表
                    logger.debug(f"Transmission 中未找到种子: {torrent_hash}")
                    torrents_info = []
            else:
                # 获取所有种子
                torrents_info = self.trc.get_torrents()

            if torrents_info:
                for torrent_info in torrents_info:
                    torrents.append(TorrentInfo(
                        name=torrent_info.name,
                        save_path=Path(torrent_info.download_dir).as_posix(),
                        tags=list(torrent_info.labels or []),
                        total_size=torrent_info.total_size,
                        hash=torrent_info.hashString,
                        # 种子文件列表
                        files=[
                            TorrentFile(
                                name=file.get('name'),
                                size=file.get('length'))
                            for file in torrent_info.fields.get('files')
                            if '_____padding_file_' not in file.get('name')  # 排除padding文件
                        ]
                    ))
        except Exception as e:
            logger.error(f"获取 Transmission 种子信息时出错: {str(e)}")

        return torrents


class FormatDownPath(_PluginBase):
    # 插件名称
    plugin_name = "路径名称格式化"
    # 插件描述
    plugin_desc = "根据自定义格式修改MP下载种子的保存路径、种子名称、种子文件名(实验功能)"
    # 插件图标
    plugin_icon = "alter_1.png"
    # 插件版本
    plugin_version = "2.0.0"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "formatdownpath_"
    # 加载顺序
    plugin_order = 33
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _lock = threading.Lock()

    # 配置属性
    _cron: str = ""
    _cron_enabled: bool = False
    _event_enabled: bool = False
    _rename_torrent: bool = False
    _rename_file: bool = False
    _downloader: list = []
    _exclude_tags: str = ""
    _exclude_dirs: str = ""
    _format_save_path: str = ""
    _format_torrent_name: str = ""
    _format_movie_path: str = ""
    _format_tv_path: str = ""

    def init_plugin(self, config: Optional[dict] = None):

        self.downloadhis = DownloadHistoryOper()
        self.transferhis = TransferHistoryOper()
        # 停止现有任务
        self.stop_service()
        self.load_config(config)

    def load_config(self, config: Optional[dict]):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "cron",
                "cron_enabled",
                "event_enabled",
                "rename_torrent",
                "rename_file",
                "downloader",
                "exclude_dirs",
                "exclude_tags",
                "format_save_path",
                "format_torrent_name",
                "format_movie_path",
                "format_tv_path",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    @staticmethod
    def get_render_mode() -> tuple[str, Optional[str]]:
        """
        获取插件渲染模式
        :return: 1、渲染模式，支持：vue/vuetify，默认vuetify；2、vue模式下编译后文件的相对路径，默认为`dist/assets`，vuetify模式下为None
        """
        return "vue", "dist/assets"

    def get_form(self):
        return [], {}

    def get_service(self) -> list[dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._cron_enabled:
            return [
                {
                    "id": self.__class__.__name__,
                    "name": "转种/辅种自定义格式化",
                    "trigger": CronTrigger.from_crontab(self._cron or "0 8 * * *"),
                    "func": self.cron_process_main,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        """退出插件"""
        pass

    def get_api(self) -> list[dict[str, Any]]:
        return [
            {
                "path": "/recover_from_history",
                "endpoint": self.recover_from_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "从记录中恢复",
                "description": "根据记录恢复修改的种子",
                "response_model": schemas.Response[None],
            },
            {
                "path": "/processed_data",
                "endpoint": self.get_processed_data,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取种子记录",
                "description": "获取已处理成功的种子记录, 键为种子hash, 值为下载器名称",
                "response_model": dict[str, str],
            },
            {
                "path": "/torrent_data",
                "endpoint": self.get_torrent_data,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取种子信息",
                "description": "根据种子hash获取种子备份信息",
                "response_model": Optional[TorrentBackupSummary],
            },
        ]

    def get_page(self):
        pass

    def get_state(self):
        return self._event_enabled or self._cron_enabled

    def get_module(self) -> dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        """
        return {
            "remove_torrents": self.on_remove_torrents,
        }

    @eventmanager.register(EventType.DownloadAdded)
    def event_process_main(self, event: Event):
        """
        处理事件
        """
        if not self._event_enabled \
            or not event:
            return
        event_data = event.event_data or {}
        torrent_hash = event_data.get("hash")
        downloader_name = event_data.get("downloader")
        if not torrent_hash or not downloader_name:
            return
        # 下载事件可能在宿主重启后重放, 已处理的种子不再重复处理
        if torrent_hash in (self.get_data(key="processed") or {}):
            logger.debug(f"种子 {torrent_hash} 已处理，跳过")
            return
        downloader = self.get_downloader(downloader_name)
        if downloader is None:
            logger.warn(f"下载器: {downloader_name} 不存在或未启用")
            return
        # 获取待处理数据
        context: Optional[Context] = event_data.get("context")
        if self.main(downloader=downloader, torrent_hash=torrent_hash,
                     meta=context.meta_info if context else None,
                     media_info=context.media_info if context else None):
            # 保存已处理数据
            self.update_data(key="processed", value={torrent_hash: downloader_name})

    def on_remove_torrents(self, hashs: Union[str, list], delete_file: bool = True,
                           downloader: Optional[str] = None):
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file: 是否删除文件
        :param downloader:  下载器
        :return: None, 继续交由系统模块删除种子
        """
        if isinstance(hashs, str):
            hashs = [hashs]
        for torrent_hash in hashs:
            self.delete_data(key="processed", torrent_hash=torrent_hash)
            if delete_file:
                logger.warn(f"清理种子: {torrent_hash} 相关下载记录")
                # 只清理标记为已删除的文件记录
                self.purge_download_records(*self.fetch_data(torrent_hash=torrent_hash, state=0))

    def cron_process_main(self):
        """
        定时任务处理下载器中的种子
        """
        # 失败数据列表
        _failures: dict[str, str] = {}
        # 获取待处理数据
        pending: dict[str, str] = self.get_data(key="pending") or {}
        # 获取已处理数据
        processed: dict[str, str] = self.get_data(key="processed") or {}
        _processed_num = 0

        def create_hash_mapping() -> dict[str, list[str]]:
            """
            生成源种子hash表
            """
            # 辅种插件数据
            assist = self.get_data(plugin_id="IYUUAutoSeed") or []
            # 辅种数据映射表 key: 源种hash, value: 辅种hash列表
            _mapping: dict[str, list[str]] = {}

            for seed_data in assist:
                hashes = []
                for a in seed_data.value or []:
                    if isinstance(a, dict):
                        hashes.extend(a.get("torrents", []))
                if seed_data.key in _mapping:
                    # 辅种插件中使用的源种子hash是下载的, 将辅种hash列表合并
                    _mapping[seed_data.key].extend(hashes)
                else:
                    # 辅种插件中使用的源种子hash不是字典的键, 需要再次判断是不是辅种产生的种子
                    for _current_hash, _hashes in _mapping.items():
                        if seed_data.key in _hashes:
                            _mapping[_current_hash].extend(hashes)
                            break
                    else:  # 不是辅种产生的种子, 作为源种子添加
                        _mapping[seed_data.key] = hashes
            return _mapping

        # 预处理集合
        processed_hashes = set(processed.keys())
        pending_hashes = set(pending.keys())
        # 预处理下载器列表
        valid_downloaders: list[tuple[str, Downloader]] = []
        for d in self._downloader or []:
            downloader = self.get_downloader(d)
            if downloader is not None:
                valid_downloaders.append((d, downloader))
            else:
                logger.warning(f"下载器: {d} 不存在或未启用")
        if not valid_downloaders:
            logger.warning("没有有效的下载器")
            return

        # 获取源种子hash表
        assist_mapping = create_hash_mapping()
        # 构建反向映射：种子哈希到源哈希
        seed_to_source_hash = {}
        for source_hash, seeds in assist_mapping.items():
            for seed in seeds:
                seed_to_source_hash[seed] = source_hash

        # 遍历有效下载器并处理种子
        for d_name, downloader in valid_downloaders:
            torrents_info = [
                torrent
                for torrent in downloader.torrents_info()
                if torrent.hash not in processed_hashes or torrent.hash in pending_hashes
            ]
            if not torrents_info:
                logger.info(f"下载器 {d_name} 没有待处理的种子")
                continue

            logger.info(f"下载器 {d_name} 待处理种子数量: {len(torrents_info)}")
            for torrent_info in torrents_info:
                _hash = seed_to_source_hash.get(torrent_info.hash, torrent_info.hash)
                downloadhis = self.downloadhis.get_by_hash(_hash or torrent_info.hash)
                # 执行处理
                if self.main(downloader=downloader, torrent_info=torrent_info, downloadhis=downloadhis):
                    # 添加到已处理数据库
                    processed[torrent_info.hash] = d_name
                    # 本次处理成功计数
                    _processed_num += 1
                else:
                    # 添加到失败数据库
                    _failures[torrent_info.hash] = d_name
        # 更新数据库, 待处理数据只保留本次仍失败的种子
        self.save_data(key="pending", value=_failures)
        if _failures:
            logger.info(f"失败 {len(_failures)} 个")
        if processed:
            # 保存已处理数据库
            self.update_data("processed", processed)
            logger.info(f"成功 {_processed_num} 个, 合计 {len(processed)} 个种子已保存至历史")

    def main(self, downloader: Downloader, downloadhis: Optional[Any] = None,
             torrent_hash: Optional[str] = None, torrent_info: Optional[TorrentInfo] = None,
             meta: Optional[MetaBase] = None, media_info: Optional[MediaInfo] = None) -> bool:
        """
        处理单个种子
        :param downloader: 下载器
        :param downloadhis: 下载历史记录
        :param torrent_hash: 种子哈希
        :param torrent_info: 种子信息
        :param meta: 文件元数据
        :param media_info: 媒体信息
        :return: 处理结果, 无需处理(排除、种子不存在)时同样返回True
        """
        # 全程加锁
        with self._lock:
            if not torrent_info:
                if not torrent_hash:
                    logger.warn("未提供种子信息")
                    return False
                torrents = downloader.torrents_info(torrent_hash)
                # 种子被手动删除或转移
                if not torrents:
                    logger.warn(f"下载器中不存在该种子: {torrent_hash}")
                    return True
                # 取第一个种子
                torrent_info = torrents[0]
            if self.is_excluded(torrent_info):
                return True
            if downloadhis:
                # 使用历史记录的识别信息
                meta, media_info = self.recognize_by_history(downloadhis)
            if not meta:
                logger.warn(f"未找到与之关联的下载种子 {torrent_info.name} 元数据识别可能不准确")
                meta = MetaInfo(torrent_info.name)
                if not meta:
                    logger.error(f"元数据获取失败，种子名称：{torrent_info.name}")
                    return False
            if not media_info:
                media_info = self.chain.recognize_media(meta=meta)
                if not media_info:
                    logger.error(f"识别媒体信息失败，种子名称：{torrent_info.name}")
                    return False
            if media_info.type not in (MediaType.MOVIE, MediaType.TV):
                logger.info(f"{torrent_info.name} 媒体类型为 {media_info.type}，不处理")
                return True
            if not settings.SCRAP_FOLLOW_TMDB and media_info.media_source and media_info.media_id:
                transfer_history = self.transferhis.get_by_media_identity(
                    media_source=media_info.media_source,
                    media_id=media_info.media_id,
                    mtype=media_info.type.value,
                )
                if transfer_history:
                    media_info.title = transfer_history.title
            success, can_backup = self.format_torrent_all(downloader=downloader, torrent_info=torrent_info,
                                                          meta=meta, media_info=media_info)
            if can_backup:
                # 备份种子数据
                self.save_data(key=torrent_info.hash, value=asdict(torrent_info))
                logger.info(f"种子 {torrent_info.name} 处理完成")
            return success

    def is_excluded(self, torrent_info: TorrentInfo) -> bool:
        """
        判断种子是否命中排除目录或排除标签
        """
        # 保存目录排除
        if self._exclude_dirs:
            for exclude_dir in self._exclude_dirs.split("\n"):
                exclude_dir = exclude_dir.strip()
                if exclude_dir and exclude_dir in str(torrent_info.save_path):
                    logger.info(f"{torrent_info.name} 保存路径: {torrent_info.save_path} 命中排除目录：{exclude_dir}")
                    return True
        # 标签排除
        if self._exclude_tags:
            exclude_tags = {tag.strip() for tag in self._exclude_tags.split(",") if tag.strip()}
            if common_tags := exclude_tags & set(torrent_info.tags):
                logger.info(f"{torrent_info.tags} 命中排除标签：{common_tags}")
                return True
        return False

    def recognize_by_history(self, downloadhis: Any) -> tuple[MetaBase, Optional[MediaInfo]]:
        """
        使用下载历史记录中的种子信息和媒体身份识别媒体
        :param downloadhis: 下载历史记录
        :return: 元数据, 媒体信息
        """
        meta = MetaInfo(title=downloadhis.torrent_name, subtitle=downloadhis.torrent_description)
        try:
            mtype = MediaType(downloadhis.type) if downloadhis.type else None
        except ValueError:
            mtype = None
        # 媒体来源与ID必须成对有效, 否则按名称识别
        media_source, media_id = resolve_media_identity(media=downloadhis)
        media_info = self.chain.recognize_media(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            episode_group=getattr(downloadhis, "episode_group", None),
        )
        return meta, media_info

    def format_torrent_all(self, downloader: Downloader, torrent_info: TorrentInfo,
                           meta: MetaBase, media_info: MediaInfo) -> tuple[bool, bool]:
        _torrent_hash = torrent_info.hash
        _torrent_name = torrent_info.name
        _format_file_path = self._format_movie_path if media_info.type == MediaType.MOVIE else self._format_tv_path
        need_update = False
        success = True
        # 关闭 Torrent自动管理
        if success and torrent_info.auto_tmm:
            try:
                logger.info(f"正在为种子 {_torrent_name} 关闭 Torrent自动管理")
                downloader.set_auto_tmm(torrent_hash=_torrent_hash, enable=False)
                logger.info(f"Torrent自动管理 关闭成功 - {_torrent_name}")
            except Exception as e:
                logger.error(f"Torrent自动管理 关闭失败，种子：{_torrent_name}，hash: {_torrent_hash}，错误：{str(e)}")
                success = False
        # 查询数据库
        downloadhis, downfiles = self.fetch_data(torrent_hash=_torrent_hash)
        # 附加并格式化种子保存路径
        if success and self._format_save_path:
            try:
                # 种子当前保存路径
                save_path = Path(torrent_info.save_path)
                # 种子新保存路径
                new_file_path = self.format_path(
                    template_string=self._format_save_path,
                    meta=meta,
                    mediainfo=media_info)
                _original_parts = save_path.parts
                _new_parts = new_file_path.parts
                # 计算公共路径长度
                common_length = 0
                for i in range(min(len(_original_parts), len(_new_parts))):
                    if _original_parts[-(i + 1)] == _new_parts[i]:
                        common_length += 1
                    else:
                        break
                # 去除重复部分
                if common_length:
                    new_file_path = Path(*_new_parts[common_length:])
                    logger.info(f"存在 {common_length} 个公共路径，去除重复部分：{_original_parts[-common_length:]}")
                new_path = save_path / new_file_path
                if new_path != save_path:
                    new_path = new_path.as_posix()
                    downloader.set_torrent_save_path(torrent_hash=_torrent_hash, location=new_path)
                    # 更新路径信息
                    downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles,
                                                              old_path=torrent_info.save_path, new_path=new_path)
                    need_update = True
                    logger.info(f"更改种子保存路径成功：{torrent_info.save_path} ==> {new_path}")
            except Exception as e:
                logger.error(f"更改种子保存路径失败：{str(e)}")
                success = False
        # 重命名种子文件
        if success and self._rename_file and _format_file_path:
            logger.info(f"{_torrent_name} 开始重命名种子文件...")
            # 使用系统整理屏蔽词
            transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords) or []
            torrent_files: list[TorrentFile] = torrent_info.files
            for file in torrent_files:
                _file_name = file.name
                if hit_word := next((keyword for keyword in transfer_exclude_words
                                     if keyword and re.search(r"%s" % keyword, _file_name, re.IGNORECASE)), None):
                    logger.info(f"{_file_name} 命中屏蔽词 {hit_word}，跳过")
                    continue
                try:
                    file_path = Path(_file_name)
                    file_suffix = file_path.suffix
                    # 种子内的音轨等附加文件同样按影视规则解析
                    file_meta = MetaInfoPath(file_path, force_video=True)
                    _file_new_path = self.format_path(
                        template_string=_format_file_path,
                        meta=file_meta,
                        mediainfo=media_info,
                        file_ext=file_suffix)
                    # 种子内路径固定使用 / 分隔, 与宿主运行平台无关
                    new_file_path = _file_new_path.as_posix()
                    old_path = file_path.as_posix()
                    # 跳过已重命名的文件
                    if new_file_path in old_path:
                        continue
                    downloader.rename_file(torrent_hash=_torrent_hash, old_path=_file_name, new_path=new_file_path)
                    # 更新路径信息
                    downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles,
                                                              old_path=_file_name, new_path=new_file_path)
                    need_update = True
                    logger.info(f"种子文件重命名成功：{_file_name} ==> {new_file_path}")
                except Exception as e:
                    logger.error(f"种子文件 {_file_name} 重命名失败：{str(e)}")
                    success = False
        # 重命名种子名称
        if success and self._rename_torrent:
            try:
                new_name = self.format_path(
                    template_string=self._format_torrent_name,
                    meta=meta,
                    mediainfo=media_info).as_posix()
                if new_name != _torrent_name:
                    downloader.torrents_rename(torrent_hash=_torrent_hash, old_path=_torrent_name,
                                               new_torrent_name=new_name)
                    logger.info(f"种子重命名成功：{_torrent_name} ==> {new_name}")
            except Exception as e:
                logger.error(f"种子重命名失败：{str(e)}")
                success = False
        # 更新数据库
        if need_update:
            self.update_db(torrent_hash=_torrent_hash, downloadhis=downloadhis, downfiles=downfiles)
        return success, need_update

    def recover_from_history(self, request: RecoverRequest) -> schemas.Response[None]:
        """
        从处理历史中恢复
        :param request: 下载器与种子哈希
        """
        # 全程加锁
        with self._lock:
            success, message = self.recover_torrent(torrent_hash=request.torrent_hash,
                                                    downloader_name=request.downloader)
        return schemas.Response(success=success, message=message)

    def recover_torrent(self, torrent_hash: str, downloader_name: str) -> tuple[bool, str]:
        """
        根据备份恢复种子的保存路径、文件名和种子名称
        :param torrent_hash: 种子哈希
        :param downloader_name: 下载器名称
        :return: 是否成功, 提示信息
        """
        backup = self.get_data(key=torrent_hash)
        if not backup:
            msg = f"未找到种子 {torrent_hash} 的处理历史"
            logger.warn(msg)
            return False, msg
        his_info = TorrentInfo.from_backup(backup)
        # 查询转种记录
        transfer_history = self.get_data(key=f"{downloader_name}-{torrent_hash}",
                                         plugin_id="TorrentTransfer")
        if transfer_history and isinstance(transfer_history, dict):
            logger.info(f"查询到转种记录 {transfer_history}")
            downloader_name = transfer_history.get("to_download") or downloader_name
            torrent_hash = transfer_history.get("to_download_id") or torrent_hash
        downloader = self.get_downloader(downloader_name)
        if downloader is None:
            msg = f"下载器: {downloader_name} 不存在或未启用"
            logger.warn(msg)
            return False, msg
        torrents = downloader.torrents_info(torrent_hash=his_info.hash)
        if not torrents:
            self.delete_data(key="processed", torrent_hash=torrent_hash)
            msg = f"下载器 {downloader_name} 不存在该种子: {torrent_hash}, 记录已删除"
            logger.warn(msg)
            return True, msg
        new_info = torrents[0]
        if new_info == his_info:
            self.delete_data(key="processed", torrent_hash=torrent_hash)
            msg = "与备份一致，跳过恢复, 记录已删除"
            logger.warn(msg)
            return True, msg
        # 查询数据库
        downloadhis, downfiles = self.fetch_data(torrent_hash=torrent_hash)
        success = True
        need_update = False
        msg = ""
        # 恢复种子文件
        if len(new_info.files) == len(his_info.files):
            for n, o in zip(new_info.files, his_info.files):
                if n.name == o.name:
                    continue
                try:
                    downloader.rename_file(torrent_hash=new_info.hash, old_path=n.name, new_path=o.name)
                    downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles,
                                                              old_path=n.name, new_path=o.name)
                    need_update = True
                    logger.info(f"种子文件恢复成功：{n.name} ==> {o.name}")
                except Exception as e:
                    msg = f"种子文件：{n.name} 恢复失败: {str(e)}"
                    logger.error(msg)
                    success = False
        else:
            logger.warn(f"种子 {new_info.name} 文件数量与备份不一致，跳过文件恢复")
        # 恢复种子保存路径
        if success and new_info.save_path != his_info.save_path:
            try:
                downloader.set_torrent_save_path(torrent_hash=new_info.hash, location=his_info.save_path)
                downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles,
                                                          old_path=new_info.save_path, new_path=his_info.save_path)
                need_update = True
                logger.info(f"保存路径恢复成功：{new_info.save_path} ==> {his_info.save_path}")
            except Exception as e:
                msg = f"保存路径恢复失败: {str(e)}"
                logger.error(msg)
                success = False
        # 恢复种子名称
        if success and new_info.name != his_info.name:
            try:
                downloader.torrents_rename(torrent_hash=new_info.hash, old_path=new_info.name,
                                           new_torrent_name=his_info.name)
                logger.info(f"恢复种子名称成功：{new_info.name} ==> {his_info.name}")
            except Exception as e:
                msg = f"种子名称：{new_info.name} 恢复失败: {str(e)}"
                logger.error(msg)
                success = False

        if need_update:
            self.update_db(torrent_hash=torrent_hash, downloadhis=downloadhis, downfiles=downfiles)
        if not success:
            return False, msg
        # 删除处理记录
        self.delete_data(key="processed", torrent_hash=torrent_hash)
        msg = "恢复完成, 记录已删除"
        logger.info(msg)
        return True, msg

    def get_processed_data(self) -> dict[str, str]:
        """
        获取已处理的种子哈希列表
        """
        return self.get_data(key="processed") or {}

    def get_torrent_data(self, torrent_hash: str) -> Optional[TorrentBackupSummary]:
        """
        获取种子数据
        """
        data: dict = self.get_data(key=torrent_hash)
        if data:
            return TorrentBackupSummary(
                name=data.get('name'),
                files_count=len(data.get('files') or []),
                save_path=data.get('save_path'),
            )
        return None

    def fetch_data(self, torrent_hash: str, state: Optional[int] = None) -> tuple[dict[int, dict], dict[int, dict]]:
        """
        使用哈希查询数据库中的下载记录和文件记录
        """
        # 查询下载历史记录
        download_history = self.downloadhis.get_by_hash(download_hash=torrent_hash)
        his = {download_history.id: {"path": download_history.path}} if download_history else {}
        # 查询文件下载记录
        download_files = self.downloadhis.get_files_by_hash(download_hash=torrent_hash, state=state)
        downfiles = {
            file.id: {
                "fullpath": file.fullpath,
                "savepath": file.savepath,
                "filepath": file.filepath,
            }
            for file in download_files or []
        }
        return his, downfiles

    @staticmethod
    def get_downloader(name: Optional[str]) -> Optional[Downloader]:
        """
        获取下载器
        :param name: 下载器名称
        :return: 下载器封装, 不存在、未连接或不支持时返回None
        """
        if not name:
            return None
        helper = DownloaderHelper()
        service = helper.get_service(name=name)
        if not service or not service.instance:
            return None
        if helper.is_downloader("qbittorrent", service=service):
            if getattr(service.instance, "qbc", None):
                return QbittorrentDownloader(qbc=service.instance)
        elif helper.is_downloader("transmission", service=service):
            if getattr(service.instance, "trc", None):
                return TransmissionDownloader(trc=service.instance)
        else:
            logger.warn(f"下载器: {name} 类型 {service.type} 暂不支持")
        return None

    def update_data(self, key: str, value: dict = None):
        """
        更新插件数据
        """
        if not value:
            return
        plugin_data: dict = self.get_data(key=key) or {}
        if plugin_data:
            plugin_data.update(value)
            self.save_data(key=key, value=plugin_data)
        else:
            self.save_data(key=key, value=value)

    def delete_data(self, key: str, torrent_hash: str):
        """
        删除插件数据
        :param key: 插件数据键
        :param torrent_hash: 种子哈希
        """
        # 删除种子备份数据
        self.del_data(key=torrent_hash)
        # 从完成记录中移除
        plugin_data: dict = self.get_data(key=key) or {}
        if torrent_hash in plugin_data:
            del plugin_data[torrent_hash]
            self.save_data(key=key, value=plugin_data)

    def update_db(self, torrent_hash: str, downloadhis: Optional[dict[int, dict]], downfiles: Optional[dict[int, dict]]):
        """
        同步下载记录与文件记录中的路径
        """
        if downloadhis:
            record = self.downloadhis.get_by_hash(download_hash=torrent_hash)
            if record and record.id in downloadhis:
                self.update_download_record(record=record, payload=downloadhis[record.id])
        if downfiles:
            records = {file.id: file for file in self.downloadhis.get_files_by_hash(download_hash=torrent_hash) or []}
            for db_id, payload in downfiles.items():
                if record := records.get(db_id):
                    self.update_download_record(record=record, payload=payload)

    def update_download_record(self, record: Any, payload: dict[str, Any]):
        """
        通过下载历史 Oper 更新单条下载记录或文件记录
        MoviePilot V3 的 DownloadHistoryOper 未公开路径更新方法, 这里复用其按模型更新的事务入口,
        不直接导入宿主 Model 或会话工厂; 宿主提供公开方法后应改用公开方法
        :param record: 通过 DownloadHistoryOper 查询得到的记录
        :param payload: 需要更新的字段
        """
        changes = {k: v for k, v in payload.items() if v is not None and getattr(record, k, None) != v}
        if not changes:
            return
        stage_update = getattr(self.downloadhis, "_stage_update", None)
        if not callable(stage_update):
            logger.warn("当前 MoviePilot 版本不支持更新下载记录，跳过同步下载记录路径")
            return
        try:
            stage_update(record, changes)
        except Exception as e:
            logger.error(f"更新下载记录 {record.id} 失败：{str(e)}")

    def purge_download_records(self, downloadhis: Optional[dict[int, dict]], downfiles: Optional[dict[int, dict]]):
        """
        清除下载记录与文件记录
        """
        if not downloadhis and not downfiles:
            logger.warn("无相关下载记录")
            return
        if downloadhis:
            for db_id in downloadhis.keys():
                self.downloadhis.delete_history(db_id)
            logger.warn(f"相关下载记录已删除 {len(downloadhis)} 条")
        if downfiles:
            for db_id in downfiles.keys():
                self.downloadhis.delete_downloadfile(db_id)
            logger.warn(f"相关下载文件记录已删除 {len(downfiles)} 条")

    @staticmethod
    def format_path(
        template_string: str,
        meta: MetaBase,
        mediainfo: MediaInfo,
        file_ext: str = None,
    ) -> Path:
        """
        根据媒体信息，返回Format字典
        :param template_string: Jinja2 模板字符串
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param file_ext: 文件扩展名
        """
        return TransHandler.get_rename_path(
            template_string=template_string,
            rename_dict=TransHandler.get_naming_dict(
                meta=meta, mediainfo=mediainfo, file_ext=file_ext
            ),
        )

    @staticmethod
    def update_path(downloadhis: dict[int, dict], downfiles: dict, old_path: str, new_path: str) -> tuple[dict[int, dict], dict[int, dict]]:

        def safe_replace(d: dict[str, str], old: str, new: str):
            """
            替换路径
            """
            for k, v in d.items():
                if v and old in v:
                    p = d[k]
                    d[k] = v.replace(old, new)
                    logger.debug(f"替换: {p} ==> {d[k]}")

        # 更新下载历史记录
        if downloadhis:
            for d in downloadhis.values():
                safe_replace(d, old_path, new_path)

        # 更新下载文件记录
        if downfiles:
            for d in downfiles.values():
                safe_replace(d, old_path, new_path)
        return downloadhis, downfiles
