"""FormatDownPath V3 的宿主边界、媒体身份与种子处理流程测试。"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.plugins import formatdownpath as plugin_module
from app.plugins.formatdownpath import (
    FormatDownPath,
    QbittorrentDownloader,
    RecoverRequest,
    TorrentBackupSummary,
    TorrentFile,
    TorrentInfo,
    TransmissionDownloader,
)
from app.schemas.types import MediaSource, MediaType


ROOT = Path(__file__).parents[3]
PLUGIN_SOURCE = ROOT / "plugins.v3" / "formatdownpath" / "__init__.py"
SELF = "self"


def _make_plugin(store: dict | None = None) -> FormatDownPath:
    """构造不触发宿主插件管理器的最小实例，插件数据保存在内存字典中。"""
    plugin = object.__new__(FormatDownPath)
    store = {} if store is None else store
    plugin._store = store
    plugin.get_data = lambda key=None, plugin_id=None: store.get((plugin_id or SELF, key))
    plugin.save_data = lambda key, value, plugin_id=None: store.__setitem__((plugin_id or SELF, key), value)
    plugin.del_data = lambda key, plugin_id=None: store.pop((plugin_id or SELF, key), None)
    plugin._cron = ""
    plugin._cron_enabled = False
    plugin._event_enabled = True
    plugin._rename_torrent = False
    plugin._rename_file = False
    plugin._downloader = []
    plugin._exclude_tags = ""
    plugin._exclude_dirs = ""
    plugin._format_save_path = ""
    plugin._format_torrent_name = ""
    plugin._format_movie_path = ""
    plugin._format_tv_path = ""
    plugin.chain = MagicMock()
    plugin.systemconfig = MagicMock()
    plugin.downloadhis = MagicMock()
    plugin.transferhis = MagicMock()
    return plugin


def _torrent(torrent_hash: str = "h1", name: str = "Show", save_path: str = "/data",
             files: list[TorrentFile] | None = None) -> TorrentInfo:
    """构造插件内部使用的种子信息。"""
    return TorrentInfo(name=name, save_path=save_path, total_size=1, hash=torrent_hash, files=files or [])


def test_v3_metadata_and_legacy_index_are_consistent() -> None:
    """V3 版本、索引、最低宿主版本与旧索引阻断标记必须保持一致。"""
    package = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
    legacy_package = json.loads((ROOT / "package.v2.json").read_text(encoding="utf-8"))
    metadata = package["FormatDownPath"]

    assert FormatDownPath.plugin_version == "2.0.0"
    assert metadata["version"] == FormatDownPath.plugin_version
    assert metadata["system_version"] == ">=3.0.0"
    assert next(iter(metadata["history"])) == "v2.0.0"
    assert legacy_package["FormatDownPath"]["v3"] is False


def test_source_uses_sdk_and_oper_without_host_models() -> None:
    """插件只依赖稳定 SDK 与公开 Oper，不导入旧路径、宿主 Model 或会话工厂。"""
    tree = ast.parse(PLUGIN_SOURCE.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert not any(module.startswith(("app.core", "app.helper", "app.utils", "app.db.models")) for module in modules)
    assert "app.log" not in modules
    assert "app.plugins" not in modules
    assert not imported_names & {"SessionFactory", "AsyncSessionFactory", "ScopedSession", "db_update", "db_query"}
    assert "app.db.oper.downloadhistory" in modules


def test_host_oper_contract_used_by_plugin_exists() -> None:
    """插件依赖的宿主 Oper 方法签名在当前 V3 宿主中可用。"""
    assert callable(getattr(DownloadHistoryOper, "_stage_update", None))
    assert "state" in inspect.signature(DownloadHistoryOper.get_files_by_hash).parameters
    assert {"media_source", "media_id", "mtype"} <= set(
        inspect.signature(TransferHistoryOper.get_by_media_identity).parameters
    )


def test_api_declares_response_models() -> None:
    """普通 JSON 接口必须显式声明输出模型，恢复接口使用统一 envelope。"""
    plugin = _make_plugin()
    models = {api["path"]: api["response_model"] for api in plugin.get_api()}

    assert models["/recover_from_history"].model_fields["data"] is not None
    assert models["/recover_from_history"].__name__.startswith("Response")
    assert models["/processed_data"] == dict[str, str]
    assert TorrentBackupSummary in getattr(models["/torrent_data"], "__args__", ())


def test_recognize_by_history_passes_paired_media_identity() -> None:
    """下载历史中的来源与 ID 成对规范化后传给通用识别链。"""
    plugin = _make_plugin()
    plugin.chain.recognize_media.return_value = "media"
    history = SimpleNamespace(torrent_name="Show.S01E01", torrent_description="desc", type="电视剧",
                              media_source="themoviedb", media_id=" 123 ", episode_group="group")

    with patch.object(plugin_module, "MetaInfo", return_value="meta") as meta_info:
        meta, media = plugin.recognize_by_history(history)

    meta_info.assert_called_once_with(title="Show.S01E01", subtitle="desc")
    assert (meta, media) == ("meta", "media")
    kwargs = plugin.chain.recognize_media.call_args.kwargs
    assert kwargs["media_source"] == MediaSource.TMDB
    assert kwargs["media_id"] == "123"
    assert kwargs["mtype"] == MediaType.TV
    assert kwargs["episode_group"] == "group"
    assert not {"tmdbid", "doubanid"} & set(kwargs)


def test_recognize_by_history_drops_invalid_identity_and_type() -> None:
    """半对或零值身份不得进入识别链，未知类型按名称识别。"""
    plugin = _make_plugin()
    history = SimpleNamespace(torrent_name="Show", torrent_description=None, type="未定义",
                              media_source="themoviedb", media_id="0")

    with patch.object(plugin_module, "MetaInfo", return_value="meta"):
        plugin.recognize_by_history(history)

    kwargs = plugin.chain.recognize_media.call_args.kwargs
    assert kwargs["media_source"] is None
    assert kwargs["media_id"] is None
    assert kwargs["mtype"] is None


def test_main_skips_non_video_media() -> None:
    """音乐等非影视媒体不套用影视命名模板。"""
    plugin = _make_plugin()
    plugin.chain.recognize_media.return_value = SimpleNamespace(type=MediaType.MUSIC)
    plugin.format_torrent_all = MagicMock()

    assert plugin.main(downloader=MagicMock(), torrent_info=_torrent(), meta="meta") is True
    plugin.format_torrent_all.assert_not_called()


def test_main_follows_transfer_history_title_by_media_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """不跟随 TMDB 标题时按统一媒体身份查询整理历史。"""
    plugin = _make_plugin()
    media = SimpleNamespace(type=MediaType.MOVIE, media_source=MediaSource.TMDB, media_id="550", title="Remote")
    plugin.transferhis.get_by_media_identity.return_value = SimpleNamespace(title="Local")
    plugin.format_torrent_all = MagicMock(return_value=(True, False))
    monkeypatch.setattr(plugin_module.settings, "SCRAP_FOLLOW_TMDB", False)

    assert plugin.main(downloader=MagicMock(), torrent_info=_torrent(), meta="meta", media_info=media) is True
    plugin.transferhis.get_by_media_identity.assert_called_once_with(
        media_source=MediaSource.TMDB, media_id="550", mtype=MediaType.MOVIE.value
    )
    assert media.title == "Local"


def test_format_torrent_all_skips_files_hit_by_exclude_words() -> None:
    """命中整理屏蔽词的文件不重命名，其余文件重命名并同步数据库。"""
    plugin = _make_plugin()
    plugin._rename_file = True
    plugin._format_tv_path = "{{title}}{{fileExt}}"
    plugin.systemconfig.get.return_value = ["sample"]
    plugin.fetch_data = MagicMock(return_value=({}, {}))
    plugin.update_db = MagicMock()
    plugin.format_path = MagicMock(return_value=Path("Season 1/Show - S01E01.mkv"))
    downloader = MagicMock()
    torrent = _torrent(files=[TorrentFile("Show/sample.mkv", 1), TorrentFile("Show/S01E01.mkv", 1)])

    with patch.object(plugin_module, "MetaInfoPath", return_value="file_meta"):
        result = plugin.format_torrent_all(downloader=downloader, torrent_info=torrent, meta="meta",
                                           media_info=SimpleNamespace(type=MediaType.TV))

    assert result == (True, True)
    downloader.rename_file.assert_called_once_with(
        torrent_hash="h1", old_path="Show/S01E01.mkv", new_path="Season 1/Show - S01E01.mkv"
    )
    plugin.update_db.assert_called_once()


def test_is_excluded_by_tag_and_dir() -> None:
    """排除标签与排除目录均按去除空白后的配置匹配。"""
    plugin = _make_plugin()
    plugin._exclude_tags = "keep, B"
    assert plugin.is_excluded(TorrentInfo(name="n", save_path="/d", total_size=1, hash="h", tags=["B"]))

    plugin._exclude_tags = ""
    plugin._exclude_dirs = "/skip\r\n"
    assert plugin.is_excluded(_torrent(save_path="/skip/movie"))
    assert not plugin.is_excluded(_torrent(save_path="/data/movie"))


def test_qbittorrent_tags_are_split_and_stripped() -> None:
    """qBittorrent 以 ", " 分隔的标签需去除空白后再参与排除匹配。"""

    class _QbTorrent(dict):
        """模拟 qbittorrent-api 的种子字典。"""

        files = [{"name": "a.mkv", "size": 1, "priority": 1}]

    qbc = SimpleNamespace(torrents_info=MagicMock(return_value=[_QbTorrent(
        name="n", save_path="/d/", total_size=1, hash="h", auto_tmm=False, category="", tags="A, B",
    )]))

    info = QbittorrentDownloader(SimpleNamespace(qbc=qbc)).torrents_info()[0]

    assert info.tags == ["A", "B"]
    assert info.save_path == "/d"
    assert info.files == [TorrentFile(name="a.mkv", size=1, priority=1)]


def test_get_downloader_selects_wrapper_by_service_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """按服务类型选择下载器封装，未连接或不支持的下载器返回 None。"""
    services = {
        "qb": SimpleNamespace(type="qbittorrent", instance=SimpleNamespace(qbc=MagicMock())),
        "tr": SimpleNamespace(type="transmission", instance=SimpleNamespace(trc=MagicMock())),
        "offline": SimpleNamespace(type="qbittorrent", instance=SimpleNamespace(qbc=None)),
        "rt": SimpleNamespace(type="rtorrent", instance=SimpleNamespace()),
    }

    class _Helper:
        """只提供插件所需方法的下载器帮助类。"""

        def get_service(self, name):
            return services.get(name)

        def is_downloader(self, service_type, service=None, name=None):
            return service.type == service_type

    monkeypatch.setattr(plugin_module, "DownloaderHelper", _Helper)

    assert isinstance(FormatDownPath.get_downloader("qb"), QbittorrentDownloader)
    assert isinstance(FormatDownPath.get_downloader("tr"), TransmissionDownloader)
    assert FormatDownPath.get_downloader("offline") is None
    assert FormatDownPath.get_downloader("rt") is None
    assert FormatDownPath.get_downloader("missing") is None
    assert FormatDownPath.get_downloader(None) is None


def test_update_db_updates_each_record_by_id_with_changed_fields() -> None:
    """每条文件记录只更新自身变化的字段，不能把一条记录的路径写到同种子的其它记录。"""
    plugin = _make_plugin()
    history = SimpleNamespace(id=1, path="/d/old")
    files = [
        SimpleNamespace(id=10, fullpath="/d/old/a.mkv", savepath="/d/old", filepath="a.mkv"),
        SimpleNamespace(id=11, fullpath="/d/old/b.mkv", savepath="/d/old", filepath="b.mkv"),
    ]
    plugin.downloadhis.get_by_hash.return_value = history
    plugin.downloadhis.get_files_by_hash.return_value = files

    plugin.update_db(
        torrent_hash="h1",
        downloadhis={1: {"path": "/d/new"}},
        downfiles={
            10: {"fullpath": "/d/new/a.mkv", "savepath": "/d/new", "filepath": "a.mkv"},
            11: {"fullpath": "/d/new/b.mkv", "savepath": "/d/new", "filepath": "b.mkv"},
        },
    )

    assert plugin.downloadhis._stage_update.call_args_list == [
        call(history, {"path": "/d/new"}),
        call(files[0], {"fullpath": "/d/new/a.mkv", "savepath": "/d/new"}),
        call(files[1], {"fullpath": "/d/new/b.mkv", "savepath": "/d/new"}),
    ]


def test_update_download_record_skips_when_host_has_no_update_entry() -> None:
    """宿主移除更新入口时只跳过数据库同步，不中断种子处理。"""
    plugin = _make_plugin()
    plugin.downloadhis = SimpleNamespace()

    plugin.update_download_record(record=SimpleNamespace(id=1, path="/old"), payload={"path": "/new"})


def test_update_path_replaces_old_path_and_ignores_empty_values() -> None:
    """路径替换只作用于包含旧路径的非空字段。"""
    his, files = FormatDownPath.update_path(
        downloadhis={1: {"path": "/d/old"}},
        downfiles={10: {"fullpath": "/d/old/a.mkv", "savepath": None, "filepath": "a.mkv"}},
        old_path="/d/old",
        new_path="/d/new",
    )

    assert his == {1: {"path": "/d/new"}}
    assert files == {10: {"fullpath": "/d/new/a.mkv", "savepath": None, "filepath": "a.mkv"}}


def test_recover_from_history_restores_and_returns_envelope() -> None:
    """恢复成功时按备份还原文件、路径和名称，并删除处理记录与备份。"""
    backup = _torrent(name="Old", save_path="/data/old", files=[TorrentFile("Old/a.mkv", 1)])
    store = {(SELF, "h1"): asdict(backup), (SELF, "processed"): {"h1": "qb"}}
    plugin = _make_plugin(store)
    downloader = MagicMock()
    downloader.torrents_info.return_value = [
        _torrent(name="New", save_path="/data/new", files=[TorrentFile("New/a.mkv", 1)])
    ]
    plugin.get_downloader = MagicMock(return_value=downloader)
    plugin.fetch_data = MagicMock(return_value=({}, {}))
    plugin.update_db = MagicMock()

    response = plugin.recover_from_history(RecoverRequest(torrent_hash="h1", downloader="qb"))

    assert response.success is True
    assert response.message == "恢复完成, 记录已删除"
    downloader.rename_file.assert_called_once_with(torrent_hash="h1", old_path="New/a.mkv", new_path="Old/a.mkv")
    downloader.set_torrent_save_path.assert_called_once_with(torrent_hash="h1", location="/data/old")
    downloader.torrents_rename.assert_called_once_with(torrent_hash="h1", old_path="New", new_torrent_name="Old")
    assert (SELF, "h1") not in store
    assert store[(SELF, "processed")] == {}


def test_recover_from_history_keeps_record_on_failure() -> None:
    """恢复失败时返回业务失败并保留处理记录，便于再次恢复。"""
    backup = _torrent(name="Show", save_path="/data/old")
    store = {(SELF, "h1"): asdict(backup), (SELF, "processed"): {"h1": "qb"}}
    plugin = _make_plugin(store)
    downloader = MagicMock()
    downloader.torrents_info.return_value = [_torrent(name="Show", save_path="/data/new")]
    downloader.set_torrent_save_path.side_effect = RuntimeError("boom")
    plugin.get_downloader = MagicMock(return_value=downloader)
    plugin.fetch_data = MagicMock(return_value=({}, {}))

    response = plugin.recover_from_history(RecoverRequest(torrent_hash="h1", downloader="qb"))

    assert response.success is False
    assert "保存路径恢复失败" in response.message
    assert store[(SELF, "processed")] == {"h1": "qb"}
    assert (SELF, "h1") in store


def test_recover_from_history_without_backup_fails() -> None:
    """没有备份时返回业务失败。"""
    plugin = _make_plugin()

    response = plugin.recover_from_history(RecoverRequest(torrent_hash="h1", downloader="qb"))

    assert response.success is False
    assert "h1" in response.message


def test_cron_uses_each_downloader_and_keeps_only_current_failures() -> None:
    """定时任务按下载器显式传递实例，待处理数据只保留本次仍失败的种子。"""
    store = {(SELF, "pending"): {"h1": "qb1", "gone": "qb1"}, (SELF, "processed"): {"h1": "qb1"}}
    plugin = _make_plugin(store)
    plugin._downloader = ["qb1", "qb2"]
    qb1, qb2 = MagicMock(), MagicMock()
    qb1.torrents_info.return_value = [_torrent("h1"), _torrent("done")]
    qb2.torrents_info.return_value = [_torrent("h2")]
    store[(SELF, "processed")]["done"] = "qb1"
    plugin.get_downloader = lambda name: {"qb1": qb1, "qb2": qb2}[name]
    plugin.downloadhis.get_by_hash.return_value = None
    calls = []

    def fake_main(downloader, torrent_info=None, downloadhis=None, **kwargs):
        calls.append((downloader, torrent_info.hash))
        return torrent_info.hash == "h1"

    plugin.main = fake_main

    plugin.cron_process_main()

    assert calls == [(qb1, "h1"), (qb2, "h2")]
    assert store[(SELF, "pending")] == {"h2": "qb2"}
    assert store[(SELF, "processed")] == {"h1": "qb1", "done": "qb1"}


def test_event_skips_processed_torrent() -> None:
    """下载事件重放时已处理的种子不再重复处理。"""
    plugin = _make_plugin({(SELF, "processed"): {"h1": "qb"}})
    plugin.main = MagicMock()
    plugin.get_downloader = MagicMock()

    plugin.event_process_main(SimpleNamespace(event_data={"hash": "h1", "downloader": "qb", "context": None}))

    plugin.main.assert_not_called()


def test_event_processes_new_torrent_with_context() -> None:
    """新下载事件使用事件上下文中的元数据与媒体信息处理，并记录下载器。"""
    store = {}
    plugin = _make_plugin(store)
    downloader = MagicMock()
    plugin.get_downloader = MagicMock(return_value=downloader)
    plugin.main = MagicMock(return_value=True)
    context = SimpleNamespace(meta_info="meta", media_info="media")

    plugin.event_process_main(SimpleNamespace(event_data={"hash": "h1", "downloader": "qb", "context": context}))

    plugin.main.assert_called_once_with(downloader=downloader, torrent_hash="h1", meta="meta", media_info="media")
    assert store[(SELF, "processed")] == {"h1": "qb"}


def test_torrent_data_returns_backup_summary() -> None:
    """数据页摘要来自种子备份，不存在时返回空。"""
    backup = _torrent(name="Show", save_path="/data", files=[TorrentFile("a.mkv", 1), TorrentFile("b.mkv", 1)])
    plugin = _make_plugin({(SELF, "h1"): asdict(backup)})

    assert plugin.get_torrent_data("h1") == TorrentBackupSummary(name="Show", files_count=2, save_path="/data")
    assert plugin.get_torrent_data("missing") is None
