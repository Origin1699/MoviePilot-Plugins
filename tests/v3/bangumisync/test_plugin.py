"""BangumiSync V3 的媒体身份解析、识别回退与宿主合同测试。"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.chain.base import ChainBase
from app.plugins import bangumisync as plugin_module
from app.plugins.bangumisync import BangumiAPIClient, BangumiSync
from app.schemas import MediaServerItem, WebhookEventInfo
from app.schemas.exception import ImmediateException
from app.schemas.types import MediaSource, MediaType, MessageType
from app.sdk.media import MetaBase
from app.sdk.queries import QueryPage, SubscriptionFilter, SubscriptionSnapshot


ROOT = Path(__file__).parents[3]
PLUGIN_SOURCE = ROOT / "plugins.v3" / "bangumisync" / "__init__.py"


@pytest.fixture(autouse=True)
def _path_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """用真实 MetaBase 代替依赖宿主识别词配置的路径解析。"""

    def meta_info_path(path: Path) -> MetaBase:
        meta = MetaBase(path.name)
        meta.cn_name = "穹庐下的魔女"
        meta.year = "2026"
        return meta

    monkeypatch.setattr(plugin_module, "MetaInfoPath", meta_info_path)


def _make_plugin() -> BangumiSync:
    """构造不触发宿主插件管理器的最小实例。"""
    plugin = object.__new__(BangumiSync)
    plugin._enable = True
    plugin._user = "kaori"
    plugin._uniqueid_match = False
    plugin._notify = False
    plugin._prefix = "穹庐下的魔女 (2026) S01 E09"
    plugin.bangumi_client = MagicMock()
    plugin.chain = MagicMock()
    plugin.post_message = MagicMock()
    return plugin


def _emby_episode_event(**kwargs) -> WebhookEventInfo:
    """构造 V3 Emby 剧集播放事件：事件身份为剧集，单集 TMDB ID 只在原始报文中。"""
    data = {
        "event": "playback.stop",
        "channel": "emby",
        "server_name": "Emby",
        "item_type": "TV",
        "media_type": "Episode",
        "item_name": "穹庐下的魔女 S1E9 第9话",
        "item_id": "series-1",
        "item_path": "/media/穹庐下的魔女 (2026)/Season 1/穹庐下的魔女 - S01E09.mkv",
        "season_id": "1",
        "episode_id": "9",
        "media_source": MediaSource.TMDB,
        "media_id": "888",
        "percentage": 95,
        "user_name": "kaori",
        "json_object": {"Item": {"Type": "Episode", "SeriesId": "series-1", "ProviderIds": {"Tmdb": "5550009"}}},
    }
    data.update(kwargs)
    return WebhookEventInfo(**data)


def _meta(media_type: MediaType = MediaType.TV) -> SimpleNamespace:
    """构造只包含识别所需字段的元数据。"""
    return SimpleNamespace(type=media_type, media_source=None, media_id=None, begin_season=1, begin_episode=9)


def test_v3_metadata_and_legacy_index_are_consistent() -> None:
    """V3 版本、索引、最低宿主版本与旧索引阻断标记必须保持一致。"""
    package = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
    legacy_package = json.loads((ROOT / "package.v2.json").read_text(encoding="utf-8"))
    metadata = package["BangumiSync"]

    assert BangumiSync.plugin_version == "3.0.0"
    assert metadata["version"] == BangumiSync.plugin_version
    assert metadata["system_version"] == ">=3.0.0"
    assert next(iter(metadata["history"])) == "v3.0.0"
    assert metadata["icon"] == BangumiSync.plugin_icon
    assert (ROOT / "icons" / BangumiSync.plugin_icon).is_file()
    assert legacy_package["BangumiSync"]["v3"] is False


def test_source_uses_sdk_and_no_legacy_media_identity() -> None:
    """插件只依赖稳定 SDK，不导入旧路径或宿主 Model，也不再读取来源专有的 tmdbid 字段。"""
    source = PLUGIN_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert not any(module.startswith(("app.core", "app.helper", "app.utils", "app.db")) for module in modules)
    assert not modules & {"app.log", "app.plugins"}
    assert not attributes & {"tmdbid", "list_by_tmdbid", "get_by_itemid"}
    assert "NotificationType" not in source


def test_host_contract_used_by_plugin_exists() -> None:
    """插件依赖的 V3 宿主模型与查询合同可用：媒体身份为来源与 ID 成对字段。"""
    assert {"media_source", "media_id"} <= set(MediaServerItem.model_fields)
    assert "tmdbid" not in MediaServerItem.model_fields
    assert {"media_source", "media_id", "json_object"} <= set(WebhookEventInfo.model_fields)
    assert {"media_source", "media_id", "season"} <= set(SubscriptionFilter.model_fields)
    assert "episode_group" in SubscriptionSnapshot.model_fields
    assert {"meta", "mtype"} <= set(inspect.signature(ChainBase.recognize_media).parameters)
    assert MessageType.Manual


def test_parse_event_meta_reads_paired_identity_from_mediaserver_item() -> None:
    """剧集通过媒体服务器条目获取成对身份，不再读取已删除的 MediaServerItem.tmdbid。"""
    plugin = _make_plugin()
    item = MediaServerItem(server="emby", item_id="series-1", media_source=MediaSource.TMDB, media_id="123")

    with patch.object(plugin_module, "MediaServerChain") as chain_cls:
        chain_cls.return_value.iteminfo.return_value = item
        meta = plugin.parse_event_meta(_emby_episode_event())

    chain_cls.return_value.iteminfo.assert_called_once_with("Emby", "series-1")
    assert meta.type == MediaType.TV
    assert (meta.begin_season, meta.begin_episode) == (1, 9)
    assert meta.media_source == MediaSource.TMDB
    assert meta.media_id == "123"
    assert "S01" in plugin._prefix and "E09" in plugin._prefix


def test_parse_event_meta_keeps_non_tmdb_source_with_its_id() -> None:
    """媒体服务器只提供豆瓣等其它来源时，来源与 ID 必须一起保留，不能把 ID 当作 TMDB ID。"""
    plugin = _make_plugin()
    item = MediaServerItem(server="emby", item_id="series-1", media_source=MediaSource.Douban, media_id="1295644")

    with patch.object(plugin_module, "MediaServerChain") as chain_cls:
        chain_cls.return_value.iteminfo.return_value = item
        meta = plugin.parse_event_meta(_emby_episode_event(media_source=None, media_id=None))

    assert meta.media_source == MediaSource.Douban
    assert meta.media_id == "1295644"


def test_parse_event_meta_without_identity_leaves_meta_unidentified() -> None:
    """媒体服务器未返回条目时不写入半对身份，后续按名称识别。"""
    plugin = _make_plugin()

    with patch.object(plugin_module, "MediaServerChain") as chain_cls:
        chain_cls.return_value.iteminfo.return_value = None
        meta = plugin.parse_event_meta(_emby_episode_event())

    assert meta.media_source is None
    assert meta.media_id is None


def test_parse_event_meta_uses_movie_event_identity_directly() -> None:
    """电影事件自带的身份可直接使用，无需再查询媒体服务器。"""
    plugin = _make_plugin()
    event = WebhookEventInfo(
        event="playback.stop", channel="jellyfin", server_name="Jellyfin", item_type="MOV", media_type="Movie",
        item_name="魔女之旅 剧场版 (2025)", item_id="movie-1", media_source=MediaSource.TMDB, media_id="456",
        percentage=99, user_name="kaori", json_object={"ItemId": "movie-1", "ItemType": "Movie"},
    )

    with patch.object(plugin_module, "MediaServerChain") as chain_cls:
        meta = plugin.parse_event_meta(event)

    chain_cls.assert_not_called()
    assert meta.type == MediaType.MOVIE
    assert (meta.media_source, meta.media_id) == (MediaSource.TMDB, "456")


def test_parse_event_meta_ignores_episode_level_event_identity() -> None:
    """Jellyfin 剧集事件身份来自单集 Provider_tmdb，不能当作剧集身份，需查询所属剧集。"""
    plugin = _make_plugin()
    event = WebhookEventInfo(
        event="PlaybackStop", channel="jellyfin", server_name="Jellyfin", item_type="TV", media_type="Episode",
        item_name="穹庐下的魔女 S1E9 第9话", item_id="episode-9", season_id="1", episode_id="9",
        media_source=MediaSource.TMDB, media_id="5550009", percentage=95, user_name="kaori",
        json_object={"ItemId": "episode-9", "SeriesId": "series-1", "ItemType": "Episode", "Provider_tmdb": "5550009"},
    )
    item = MediaServerItem(server="jellyfin", item_id="series-1", media_source=MediaSource.TMDB, media_id="123")

    with patch.object(plugin_module, "MediaServerChain") as chain_cls:
        chain_cls.return_value.iteminfo.return_value = item
        meta = plugin.parse_event_meta(event)

    chain_cls.return_value.iteminfo.assert_called_once_with("Jellyfin", "series-1")
    assert (meta.media_source, meta.media_id) == (MediaSource.TMDB, "123")


def test_plex_episode_queries_show_and_plex_movie_is_movie() -> None:
    """Plex 剧集按所属剧集查询身份；Plex 事件没有 media_type，电影按 item_type 识别。"""
    episode = WebhookEventInfo(
        event="media.scrobble", channel="plex", item_type="TV", item_id="/library/metadata/40294",
        json_object={"Metadata": {"type": "episode", "grandparentKey": "/library/metadata/40275",
                                  "Guid": [{"id": "imdb://tt14765720"}, {"id": "tmdb://3087250"}]}},
    )
    movie = WebhookEventInfo(event="media.scrobble", channel="plex", item_type="MOV", item_name="Movie (2024)",
                             item_id="/library/metadata/1", json_object={"Metadata": {"type": "movie"}})

    assert BangumiSync.get_itemid(episode) == "/library/metadata/40275"
    assert BangumiSync.get_episode_tmdb_id(episode) == "3087250"
    assert BangumiSync.get_itemid(movie) == "/library/metadata/1"
    with patch.object(plugin_module, "MediaServerChain") as chain_cls:
        chain_cls.return_value.iteminfo.return_value = None
        assert _make_plugin().parse_event_meta(movie).type == MediaType.MOVIE


def test_episode_tmdb_id_comes_from_raw_episode_payload() -> None:
    """集唯一ID匹配使用单集 TMDB ID：Emby 事件身份已是剧集，必须从原始报文读取。"""
    jellyfin = WebhookEventInfo(channel="jellyfin", json_object={"ItemType": "Episode", "Provider_Tmdb": " 77 "})
    jellyfin_movie = WebhookEventInfo(channel="jellyfin", json_object={"ItemType": "Movie", "Provider_tmdb": "1"})

    assert BangumiSync.get_episode_tmdb_id(_emby_episode_event()) == "5550009"
    assert BangumiSync.get_episode_tmdb_id(jellyfin) == "77"
    assert BangumiSync.get_episode_tmdb_id(jellyfin_movie) is None
    assert BangumiSync.get_episode_tmdb_id(_emby_episode_event(json_object={})) is None


def test_recognize_media_passes_identity_and_type() -> None:
    """识别时显式传入元数据中的成对身份与媒体类型，TMDB 结果直接返回。"""
    plugin = _make_plugin()
    meta = _meta()
    meta.media_source, meta.media_id = MediaSource.TMDB, "123"
    media = SimpleNamespace(tmdb_id=123)
    plugin.chain.recognize_media.return_value = media

    assert plugin.recognize_media(meta) is media
    plugin.chain.recognize_media.assert_called_once_with(meta=meta, mtype=MediaType.TV)


def test_recognize_media_falls_back_to_name_for_non_tmdb_identity() -> None:
    """非 TMDB 身份识别结果缺少 TMDB 数据时清除身份并按名称重新识别。"""
    plugin = _make_plugin()
    meta = _meta()
    meta.media_source, meta.media_id = MediaSource.Bangumi, "400602"
    bangumi_media = SimpleNamespace(tmdb_id=None)
    tmdb_media = SimpleNamespace(tmdb_id=123)
    identities = []

    def recognize(meta, mtype):
        identities.append((meta.media_source, meta.media_id))
        return bangumi_media if meta.media_source else tmdb_media

    plugin.chain.recognize_media.side_effect = recognize

    assert plugin.recognize_media(meta) is tmdb_media
    assert identities == [(MediaSource.Bangumi, "400602"), (None, None)]


def test_recognize_media_does_not_retry_tmdb_identity() -> None:
    """TMDB 身份识别失败时不再按名称猜测，避免匹配到其它条目。"""
    plugin = _make_plugin()
    meta = _meta()
    meta.media_source, meta.media_id = MediaSource.TMDB, "123"
    plugin.chain.recognize_media.return_value = None

    assert plugin.recognize_media(meta) is None
    plugin.chain.recognize_media.assert_called_once()


def test_hook_syncs_tv_episode_with_episode_unique_id() -> None:
    """完整剧集事件：识别后按单集 TMDB ID 匹配条目并同步状态。"""
    plugin = _make_plugin()
    meta = _meta()
    media = SimpleNamespace(type=MediaType.TV, genre_ids=[16], tmdb_id=123)
    plugin.parse_event_meta = MagicMock(return_value=meta)
    plugin.recognize_media = MagicMock(return_value=media)
    plugin._match_tv_subject = MagicMock(return_value=(1, 2, False))
    plugin.sync_subject_status = MagicMock()

    plugin.hook(SimpleNamespace(event_data=_emby_episode_event()))

    plugin.recognize_media.assert_called_once_with(meta)
    plugin._match_tv_subject.assert_called_once_with(media, meta, "5550009")
    plugin.sync_subject_status.assert_called_once_with(1, 2, False)


def test_hook_notifies_with_message_type_on_immediate_exception() -> None:
    """匹配失败时按配置发送手动处理通知。"""
    plugin = _make_plugin()
    plugin._notify = True
    media = SimpleNamespace(type=MediaType.TV, genre_ids=[16], get_message_image=lambda: "img")
    plugin.parse_event_meta = MagicMock(return_value=_meta())
    plugin.recognize_media = MagicMock(return_value=media)
    plugin._match_tv_subject = MagicMock(side_effect=ImmediateException("未能找到匹配的Bangumi条目"))

    plugin.hook(SimpleNamespace(event_data=_emby_episode_event()))

    kwargs = plugin.post_message.call_args.kwargs
    assert kwargs["mtype"] == MessageType.Manual
    assert kwargs["image"] == "img"
    assert "未能找到匹配的Bangumi条目" in kwargs["text"]


@pytest.mark.parametrize("changes", [
    {"user_name": "other"},
    {"percentage": 50},
])
def test_hook_skips_other_users_and_unfinished_playback(changes: dict) -> None:
    """非指定用户或播放进度不足时不处理。"""
    plugin = _make_plugin()
    plugin.parse_event_meta = MagicMock()

    plugin.hook(SimpleNamespace(event_data=_emby_episode_event(**changes)))

    plugin.parse_event_meta.assert_not_called()


def test_hook_without_token_skips_processing() -> None:
    """启用但未配置 Token 时跳过处理，而不是因缺少客户端抛出异常。"""
    plugin = _make_plugin()
    plugin.bangumi_client = None
    plugin.parse_event_meta = MagicMock()

    plugin.hook(SimpleNamespace(event_data=_emby_episode_event()))

    plugin.parse_event_meta.assert_not_called()


def test_init_plugin_resets_client_when_token_removed() -> None:
    """重新加载配置时按当前 Token 重建或清除 Bangumi 客户端。"""
    plugin = _make_plugin()

    plugin.init_plugin({"enable": True, "token": "token"})
    assert isinstance(plugin.bangumi_client, BangumiAPIClient)

    plugin.init_plugin({"enable": True, "token": ""})
    assert plugin.bangumi_client is None


def test_episode_group_uses_v3_subscription_query_and_original_language() -> None:
    """TMDB 季详情缺失时按 V3 订阅查询的剧集组取集信息，期间临时切换为原语言。"""
    plugin = _make_plugin()
    season_obj = SimpleNamespace(language="zh-CN")
    tv = SimpleNamespace(language="zh-CN")
    group_episodes = [{"order": 0, "episodes": []}, {"order": 1, "episodes": [{"id": 9, "order": 8}]}]
    tv.group_episodes = MagicMock(return_value=group_episodes)
    plugin.chain.modulemanager.get_running_module.return_value = SimpleNamespace(
        tmdb=SimpleNamespace(tv=tv, season_obj=season_obj)
    )
    languages = []

    def tmdb_info(tmdbid, mtype, season):
        languages.append((tv.language, season_obj.language))
        return {}

    plugin.chain.tmdb_info.side_effect = tmdb_info
    media = SimpleNamespace(tmdb_id=123, type=MediaType.TV, original_language="ja", episode_groups=[])
    page = QueryPage[SubscriptionSnapshot](items=[
        SubscriptionSnapshot(id=1, media_source=MediaSource.TMDB, media_id="123", season=1, episode_group="grp"),
    ])

    with patch.object(plugin_module, "list_subscriptions", return_value=page) as list_subs:
        episodes = plugin.get_original_language_tmdb_episodes(mediainfo=media, season=1)

    filters = list_subs.call_args.args[0]
    assert (filters.media_source, filters.media_id, filters.season) == (MediaSource.TMDB, "123", 1)
    tv.group_episodes.assert_called_once_with("grp")
    assert episodes == [{"id": 9, "order": 8}]
    assert languages == [("ja", "ja")]
    assert (tv.language, season_obj.language) == ("zh-CN", "zh-CN")


def test_tmdb_episodes_empty_when_tmdb_module_not_running() -> None:
    """TheMovieDb 模块未运行时返回空剧集列表。"""
    plugin = _make_plugin()
    plugin.chain.modulemanager.get_running_module.return_value = None

    media = SimpleNamespace(tmdb_id=123, type=MediaType.TV, original_language="ja", episode_groups=[])
    assert plugin.get_original_language_tmdb_episodes(mediainfo=media, season=1) == []
    plugin.chain.tmdb_info.assert_not_called()


def test_find_matching_episode_without_tmdb_episode_uses_episode_number() -> None:
    """未匹配到 TMDB 单集时仍可按集号匹配 Bangumi 单集，而不是因空值崩溃。"""
    plugin = _make_plugin()
    episodes = [
        {"id": 101, "sort": 8, "ep": 8, "name": "", "airdate": "2026-08-01"},
        {"id": 102, "sort": 9, "ep": 9, "name": "", "airdate": "2026-08-08"},
        {"id": 103, "sort": 10, "ep": 10, "name": "", "airdate": "2026-08-15"},
    ]

    assert plugin._find_matching_episode(episodes, None, 9) == (102, False)
    assert plugin._find_matching_episode(episodes, None, 10) == (103, True)


def test_validate_episode_matching_without_aired_bangumi_episodes() -> None:
    """Bangumi 条目尚无已播出的集时匹配率为 0，而不是除零异常。"""
    assert BangumiSync._validate_episode_matching([], [{"id": 1, "airdate": ""}]) == 0
