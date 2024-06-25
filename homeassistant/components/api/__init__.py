"""Rest API for Home Assistant."""

import asyncio
import json
import logging
import time
from asyncio import shield, timeout
from datetime import timedelta
from functools import lru_cache
from http import HTTPStatus
from typing import Any

import paho.mqtt.client as mqtt
import voluptuous as vol
from aiohttp import web
from aiohttp.web_exceptions import HTTPBadRequest

import homeassistant.core as ha
from homeassistant.auth import InvalidAuthError
from homeassistant.auth.models import User, TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import person
from homeassistant.components.http import (
    KEY_HASS,
    KEY_HASS_USER,
    HomeAssistantView,
    require_admin,
)
from homeassistant.components.onboarding.views import _async_get_hass_provider
from homeassistant.const import (
    CONTENT_TYPE_JSON,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    KEY_DATA_LOGGING as DATA_LOGGING,
    MATCH_ALL,
    URL_API,
    URL_API_COMPONENTS,
    URL_API_CONFIG,
    URL_API_CORE_STATE,
    URL_API_ERROR_LOG,
    URL_API_EVENTS,
    URL_API_SERVICES,
    URL_API_STATES,
    URL_API_STREAM,
    URL_API_TEMPLATE,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.exceptions import (
    InvalidEntityFormatError,
    InvalidStateError,
    ServiceNotFound,
    TemplateError,
    Unauthorized,
)
from homeassistant.helpers import config_validation as cv, template
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import async_get
from homeassistant.helpers.json import json_dumps, json_fragment
from homeassistant.helpers.service import async_get_all_descriptions
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.event_type import EventType
from homeassistant.util.json import json_loads

_LOGGER = logging.getLogger(__name__)

ATTR_BASE_URL = "base_url"
ATTR_EXTERNAL_URL = "external_url"
ATTR_INTERNAL_URL = "internal_url"
ATTR_LOCATION_NAME = "location_name"
ATTR_INSTALLATION_TYPE = "installation_type"
ATTR_REQUIRES_API_PASSWORD = "requires_api_password"
ATTR_UUID = "uuid"
ATTR_VERSION = "version"

DOMAIN = "api"
STREAM_PING_PAYLOAD = "ping"
STREAM_PING_INTERVAL = 50  # seconds
SERVICE_WAIT_TIMEOUT = 10

DEVICE_NAME = "zhongkongshebei"
PRODUCT_KEY = "pLY9oG0qrDhh"
USERNAME = "zhongkongshebei&pLY9oG0qrDhh"
PASSWORD = "67f3350d21004b109d9bb11bfb62a139"
TOPIC = f"/sys/{DEVICE_NAME}/{PRODUCT_KEY}/device/"
HOST = "175.27.250.231"
PORT = 1883

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the API with the HTTP interface."""
    hass.http.register_view(APIStatusView)
    hass.http.register_view(APICoreStateView)
    hass.http.register_view(APIEventStream)
    hass.http.register_view(APIConfigView)
    hass.http.register_view(APIStatesView)
    hass.http.register_view(APIEntityStateView)
    hass.http.register_view(APIEventListenersView)
    hass.http.register_view(APIEventView)
    hass.http.register_view(APIServicesView)
    hass.http.register_view(APIDomainServicesView)
    hass.http.register_view(APIComponentsView)
    hass.http.register_view(APITemplateView)
    # add
    hass.http.register_view(APIAuthView)
    hass.http.register_view(APIDeviceView)
    hass.http.register_view(APIEntityView)
    hass.http.register_view(APISonosPlaybackMetadataView)
    hass.http.register_view(APISonosGetDeviceListView)
    hass.http.register_view(APISonosFavoritesListView)
    hass.http.register_view(APISonosPlayerVolumeView)
    hass.http.register_view(APISonosPlaybackStatusView)
    hass.http.register_view(APIAutomationView)
    hass.http.register_view(APISceneView)

    if DATA_LOGGING in hass.data:
        hass.http.register_view(APIErrorLog)

    return True


class APIAuthView(HomeAssistantView):
    """获取用户和新增用户接口."""

    url = "/api/auth"
    name = "api:auth"
    # 取消权限认证
    requires_auth = False

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        msg_json = {"code": 200, "message": "success"}
        auth_dict = request.app[KEY_HASS].auth._store._users
        return_li = []
        for id, user in auth_dict.items():
            token_li = []
            for key, value in user.refresh_tokens.items():
                token_dict = {
                    "client_id": value.client_id,
                    "client_name": value.client_name,
                    "client_icon": value.client_icon,
                    "token_type": value.token_type,
                    "id": value.id,
                    "created_at": value.created_at,
                    "token": value.token,
                    "jwt_key": value.jwt_key,
                    "last_used_at": value.last_used_at,
                    "last_used_ip": value.last_used_ip
                }
                token_li.append(token_dict)
            return_json = {
                "id": id,
                "name": user.name,
                "is_owner": user.is_owner,
                "is_active": user.is_active,
                "system_generated": user.system_generated,
                "local_only": user.local_only,
                "groups": [group.id for group in user.groups],
                "refresh_tokens": token_li
            }
            return_li.append(return_json)
        """Retrieve if API is running."""
        msg_json["data"] = return_li
        msg_json["count"] = len(return_li)
        return self.json(msg_json)

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON specified.", HTTPStatus.BAD_REQUEST)

        if (username := data.get("username")) is None:
            return self.json_message("No username specified.", HTTPStatus.BAD_REQUEST)
        if (password := data.get("password")) is None:
            return self.json_message("No password specified.", HTTPStatus.BAD_REQUEST)
        # 判断用户是否存在
        hass = request.app[KEY_HASS]
        for user in hass.auth._store._users.values():
            if username == user.name:
                return self.json_message("username already exists.", HTTPStatus.BAD_REQUEST)
        # 创建用户
        msg_json = {"code": 200, "message": "success"}
        provider = _async_get_hass_provider(hass)
        await provider.async_initialize()
        # 创建用户
        user = await request.app[KEY_HASS].auth.async_create_user(
            name=username, group_ids=["system-admin"], local_only=False
        )
        await provider.async_add_auth(username, password)
        # 创建长期访问令牌
        credentials = await provider.async_get_or_create_credentials(
            {"username": username}
        )
        await hass.auth.async_link_user(user, credentials)
        if "person" in hass.config.components:
            await person.async_create_person(hass, username, user_id=user.id)
        # 创建refresh token和long-lived access token
        refresh_token = await hass.auth.async_create_refresh_token(
            user,
            client_name="",
            client_icon="",
            token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
            access_token_expiration=timedelta(days=365),
        )

        try:
            access_token = hass.auth.async_create_access_token(refresh_token)
        except InvalidAuthError as exc:
            return self.json_message("InvalidAuthError.", HTTPStatus.BAD_REQUEST)

        msg_json["data"] = {
            "id": user.id,
            "name": user.name,
            "access_token": access_token
        }
        msg_json["count"] = 1
        return self.json(msg_json)


class APIDeviceView(HomeAssistantView):
    """获取设备列表信息接口和注册设备列表接口"""

    url = "/api/device"
    name = "api:device"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        registry = async_get(hass)
        msg_json_prefix = (
            f'{{"code":200,"message":"success","data": ['
        ).encode()
        # Concatenate cached entity registry item JSON serializations
        device_li = []
        for entry in registry.devices.values():
            if entry.json_repr is not None:
                device_li.append(entry.json_repr)
        inner = b",".join(device_li)
        msg_json = b"".join((msg_json_prefix, inner, b"]}"))
        msg_json = json.loads(msg_json.decode('utf-8'))
        msg_json["count"] = len(device_li)
        return self.json(msg_json)

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON specified.", HTTPStatus.BAD_REQUEST)

        if (device_ids := data.get("deviceId")) is None:
            return self.json_message("No name specified.", HTTPStatus.BAD_REQUEST)

        try:
            hass = request.app[KEY_HASS]
            registry = async_get(hass)

            # 设备上线
            def connected():
                topic = TOPIC + "connected"
                data = {
                    "ipaddress": "",
                    "clientid": client_id,
                    "keepalive": 60,
                    "connack": 0,
                    "proto_ver": 4,
                    "proto_name": "MQTT",
                    "username": USERNAME,
                    "ts": request_ts
                }
                result = client.publish(topic, json.dumps(data))
                status = result[0]
                if status == 0:
                    print(f"Send `{data}` to topic `{topic}`")
                else:
                    print(f"Failed to send message to topic {topic}")

            # 设备下线
            def disconnected():
                topic = TOPIC + "disconnected"
                data = {
                    "reason": "normal",
                    "clientid": client_id,
                    "username": USERNAME,
                    "ts": request_ts
                }
                result = client.publish(topic, json.dumps(data))
                status = result[0]
                if status == 0:
                    print(f"Send `{data}` to topic `{topic}`")
                else:
                    print(f"Failed to send message to topic {topic}")

            # 网关子设备注册
            def sub_register(device_json):
                topic = TOPIC + "sub/register"
                data = {

                    "deviceName": DEVICE_NAME,
                    "productKey": PRODUCT_KEY,
                    "ts": request_ts,
                    "device": {
                        "deviceNickname": device_json.get("name"),
                        "deviceName": device_json.get("id"),
                        "productKey": "gvSLjghQjD5n",
                        "thirdDevCode": "",
                        "location": {
                            "address": "",
                            "location": ""
                        },
                        "label": [
                            {
                                "name": "型号",
                                "id": "model",
                                "val": "" if device_json.get("model") is None else device_json.get("model")
                            }
                        ],
                        "versions": {
                            "MCU": "",
                            "MDM": ""
                        },
                        "desc": ""
                    }
                }
                result = client.publish(topic, json.dumps(data))
                status = result[0]
                if status == 0:
                    print(f"Send `{data}` to topic `{topic}`")
                else:
                    print(f"Failed to send message to topic {topic}")
                    return self.json_message("Failed to send message to topic", HTTPStatus.BAD_REQUEST)

            # 连接成功回调函数
            def on_connect(client, userdata, flags, rc):
                print("Connected with result code " + str(rc))
                if rc == 0:
                    print("MQTT Connection successful")
                    # 具体业务
                    # 连接中控网关
                    # connected()
                    # 获取全部设备，将选中设备进行创建
                    for entry in registry.devices.values():
                        if entry.json_repr is not None:
                            device_json = json.loads(entry.json_repr.decode('utf-8'))
                            for device_id in device_ids:
                                if device_id == device_json.get("id"):
                                    print(device_id)
                                    print(device_json.get("name"))
                                    # 开始新增中控网关子设备
                                    sub_register(device_json)
                    # 断开中控网关连接
                    # disconnected()
                else:
                    print("MQTT connection failed")
                    return self.json_message("MQTT connection failed!.", HTTPStatus.BAD_REQUEST)

            # 创建客户端实例
            request_ts = int(time.time())
            client_id = DEVICE_NAME + str(request_ts)
            client = mqtt.Client(client_id=client_id)
            client.username_pw_set(username=USERNAME, password=PASSWORD)

            # 设置连接成功回调函数
            client.on_connect = on_connect

            # 连接到 MQTT 代理
            client.connect(host=HOST, port=PORT)

            # 循环处理网络流量
            client.loop_start()
            client.loop_stop()  # 停止网络循环
            client.disconnect()  # 断开连接
            return_json = {"message": "success", "code": 200}
            return self.json(return_json)
        except ValueError:
            return self.json_message("Invalid Request!.", HTTPStatus.BAD_REQUEST)


class APIEntityView(HomeAssistantView):
    """获取中控实体列表接口"""

    url = "/api/entity"
    name = "api:entity"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        registry = er.async_get(hass)
        msg_json = {"code": 200, "message": "success"}
        entity_li = []
        # Concatenate cached entity registry item JSON serializations
        for entry in registry.entities.values():
            if entry.disabled_by is None and entry.display_json_repr is not None:
                entity_li.append(entry.as_partial_dict)
        msg_json["data"] = entity_li
        msg_json["count"] = len(entity_li)
        return self.json(msg_json)


class APISonosPlaybackMetadataView(HomeAssistantView):
    """获取sonos当前播放歌曲信息接口"""

    url = "/api/sonos/playbackMetadata"
    name = "api:sonos-playback"

    async def post(self, request: web.Request) -> web.Response:
        try:
            data_json = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON specified.", HTTPStatus.BAD_REQUEST)

        msg_json = {"code": 200, "message": "success"}
        hass = request.app[KEY_HASS]
        data = hass.data["sonos_media_player"]
        return_li = []

        for key, value in data.discovered.items():  # SonosSpeaker
            print("play_mode", value.media.play_mode)  # 播放模式 随机or顺序
            print("playback_status", value.media.playback_status)  # 播放状态

            print("album_name", value.media.album_name)  # 专辑名称
            print("artist", value.media.artist)  # 歌曲作者

            print("channel", value.media.channel)
            print("duration", value.media.duration)  # 歌曲时长

            print("image_url", value.media.image_url)  # 歌曲图片
            print("queue_position", value.media.queue_position)  # 歌曲所在顺序

            print("queue_size", value.media.queue_size)  # 播放列表长度
            print("playlist_name", value.media.playlist_name)  # 播放列表名称

            print("source_name", value.media.source_name)
            print("title", value.media.title)  # 歌曲名称

            print("uri", value.media.uri)  # 歌曲在sonos上uri
            print("position", value.media.position)  # 歌曲播放进度
            print("position_updated_at", value.media.position_updated_at)  # 歌曲最后播放时间

            if data_json.get("groupId") in value.sonos_group_entities:
                return_json = {
                    "container": {  # 音乐的来源。容器表示电台上的节目、播放列表、线路源或其他音频源。
                        "book": {
                            "author": {
                                "id": {
                                    "accountId": "",
                                    "objectId": "",
                                    "serviceId": ""
                                },
                                "name": "",
                                "tags": []
                            },
                            "chapterCount": 0,
                            "id": {
                                "accountId": "",
                                "objectId": "",
                                "serviceId": ""
                            },
                            "name": "",
                            "narrator": {}
                        },
                        "id": {  # （可选）此曲目的唯一音乐服务对象ID；
                            "accountId": "",
                            "objectId": "",
                            "serviceId": ""
                        },
                        "imageUrl": "",  # （可选）表示音乐服务的图像（通常是JPG或PNG）的URL
                        "name": "",  # 容器的名称
                        "podcast": {
                            "id": {
                                "accountId": "",
                                "objectId": "",
                                "serviceId": ""
                            },
                            "name": "",
                            "producer": {}
                        },
                        "service": {  # （可选）描述此音乐源的音乐服务的服务对象。
                            "id": "",
                            "imageUrl": "",
                            "name": ""
                        },
                        "tags": [],  # （可选）容器的标签。当前唯一的值是“TAG_EXPLICIT”，表示容器的内容包括显式内容
                        "type": ""  # （可选）源的类型
                    },
                    "currentItem": {
                        "deleted": False,  # （可选）曲目是否被删除
                        "id": "",  # （可选）歌曲的云队列项目ID
                        "policies": {  # （可选）某些策略可以被项覆盖
                            "canCrossfade": True,
                            "canRepeat": True,
                            "canRepeatOne": True,
                            "canResume": True,
                            "canSeek": True,
                            "canShuffle": True,
                            "canSkip": True,
                            "canSkipBack": True,
                            "canSkipToItem": True,
                            "isVisible": True,
                            "limitedSkips": True,
                            "notifyUserIntent": True,
                            "pauseAtEndOfQueue": True,
                            "pauseOnDuck": True,
                            "pauseTtlSec": 0,
                            "playTtlSec": 0,
                            "refreshAuthWhilePaused": True,
                            "showNNextTracks": 0,
                            "showNPreviousTracks": 0
                        },
                        "track": {  # 歌曲信息
                            "album": {  # 专辑
                                "artist": {},
                                "id": {
                                    "accountId": "",
                                    "objectId": "",
                                    "serviceId": ""
                                },
                                "name": value.media.album_name,  # 专辑名称
                                "tags": []
                            },
                            "artist": {},  # 艺术家
                            "author": value.media.artist,  # 作者
                            "book": {},
                            "chapterNumber": 0,
                            "contentType": "",  # 内容的类型，例如“audio/mpeg”
                            "durationMillis": value.media.duration,
                            "episodeNumber": 0,
                            "id": {  # 该曲目的唯一音乐服务对象ID
                                "accountId": "",  # （可选）要在Sonos上用于在会话中播放的音乐服务帐户
                                "objectId": "",  # 音乐服务中特定内容片段的唯一标识符
                                "serviceId": ""  # （可选）音乐服务的唯一标识符
                            },
                            "imageUrl": value.media.image_url,  # 曲目图像的URL，例如专辑封面
                            "mediaUrl": value.media.uri,  # 歌曲地址
                            "name": value.media.title,  # 歌曲名称
                            "narrator": {},
                            "podcast": {},
                            "producer": {},
                            "quality": {
                                "bitDepth": 0,
                                "codec": "",
                                "immersive": False,
                                "lossless": False,
                                "sampleRate": 0
                            },
                            "releaseDate": "",
                            "replayGain": 0,  # 播放器将此规范化应用于跟踪音频，覆盖在实际媒体中找到的任何值
                            "service": {  # 在本地库的情况下，音乐服务标识符或伪服务标识符
                                "id": "",
                                "imageUrl": "",
                                "name": ""
                            },
                            "tags": [],
                            "trackNumber": value.media.queue_position,  # 歌曲
                            "type": ""  # 类型 默认值track
                        }
                    },
                    "currentShow": {
                        "id": {
                            "accountId": "",
                            "objectId": "",
                            "serviceId": ""
                        },
                        "imageUrl": value.media.image_url,
                        "name": value.media.title,
                        "tags": []
                    },
                    "nextItem": {},
                    "streamInfo": ""
                }

                return_li.append(return_json)

            msg_json["data"] = return_li
            msg_json["count"] = len(return_li)
            return self.json(msg_json)


class APISonosGetDeviceListView(HomeAssistantView):
    """获取sonos设备列表接口"""

    url = "/api/sonos/getDeviceList"
    name = "api:sonos-getDeviceList"

    async def post(self, request: web.Request) -> web.Response:
        msg_json = {"code": 200, "message": "success"}

        hass = request.app[KEY_HASS]
        registry = er.async_get(hass)
        data = hass.data["sonos_media_player"]
        device_list = []
        entyti_name = ""
        for key, value in data.discovered.items():
            # 获取全部实体，根据实体id获取实体名称
            for entry in registry.entities.values():
                if entry.disabled_by is None and entry.display_json_repr is not None:
                    entity_dict = entry.as_partial_dict
                    if entity_dict.get("unique_id") == key:
                        entyti_name = entity_dict.get("name")
            device = {"groupId": value.sonos_group_entities[0] if value.sonos_group_entities else "",
                      "householdId": value.household_id,
                      "id": key,
                      "name": entyti_name}
            device_list.append(device)

        msg_json["data"] = {"deviceDTOList": device_list}
        msg_json["count"] = len(device_list)
        return self.json(msg_json)


class APISonosFavoritesListView(HomeAssistantView):
    """获取sonos我收藏的专辑接口"""

    url = "/api/sonos/favoritesList"
    name = "api:sonos-favoritesList"

    async def post(self, request: web.Request) -> web.Response:

        try:
            data_json = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON specified.", HTTPStatus.BAD_REQUEST)

        msg_json = {"code": 200, "message": "success"}
        hass = request.app[KEY_HASS]
        data = hass.data["sonos_media_player"]
        count = 0
        return_li = []

        media_player = hass.data["media_player"]
        print(media_player.config)
        for key, value in media_player._entities.items():
            print(key)
            print(value)

        for key, value in data.favorites.items():  # Sonos默认播放列表favorites
            print(key)  # householdId
            print("_favorites", value._favorites)
            print("last_polled_ids", value.last_polled_ids)  # 最后播放的设备id和歌曲所在顺序
            for item in value._favorites:
                print(item)
                print(item.type)  # 播放类型 instantPlay 即时播放
                print(item.description)  # 表演者 Sonos Radio 或者 表演者 Jam 或者 QQ音乐
                print(item.favorite_nr)  # 顺序
                print(item.resource_meta_data)  # item id parentID restricted title

        # 根据传入的household_id获取收藏的专辑信息
        for key, value in data.favorites.items():
            if data_json.get("householdId") == key:
                for item in value._favorites:
                    var = {
                        "description": "",
                        "id": "",
                        "imageUrl": "",
                        "name": "",
                        "resource": {
                            "id": {
                                "accountId": "",
                                "objectId": "",
                                "serviceId": ""
                            },
                            "name": "",
                            "type": ""
                        },
                        "service": {
                            "id": "",
                            "imageUrl": "",
                            "name": ""
                        }
                    }
                    return_li.append(var)
        msg_json["data"] = {"items": return_li, "version": ""}
        msg_json["count"] = count
        return self.json(msg_json)


class APISonosPlayerVolumeView(HomeAssistantView):
    """获取sonos当前音量接口"""

    url = "/api/sonos/playerVolume"
    name = "api:sonos-playerVolume"

    async def post(self, request: web.Request) -> web.Response:

        try:
            data_json = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON specified.", HTTPStatus.BAD_REQUEST)
        if (play_id := data_json.get("playId")) is None:
            return self.json_message("No playId specified.", HTTPStatus.BAD_REQUEST)

        msg_json = {"code": 200, "message": "success"}
        hass = request.app[KEY_HASS]
        data = hass.data["sonos_media_player"]
        count = 0
        # 根据传入的播放器id获取音量信息
        for key, value in data.discovered.items():
            if key == play_id:
                count = 1
                msg_json["data"] = {
                    "fixed": False,  # 指示播放器的音量是固定的还是可变的
                    "muted": value.muted,
                    "volume": value.volume
                }

        msg_json["count"] = count
        return self.json(msg_json)


class APISonosPlaybackStatusView(HomeAssistantView):
    """获取sonos播放器当前状态接口"""

    url = "/api/sonos/playbackStatus"
    name = "api:sonos-playback"

    async def post(self, request: web.Request) -> web.Response:
        try:
            data_json = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON specified.", HTTPStatus.BAD_REQUEST)

        msg_json = {"code": 200, "message": "success"}
        hass = request.app[KEY_HASS]
        data = hass.data["sonos_media_player"]
        return_li = []
        for key, value in data.discovered.items():
            return_json = {}
            if (key == data_json.get("playId") or data_json.get("householdId") == value.household_id or
                    data_json.get("groupId") in value.sonos_group_entities):
                return_json = {
                    "playbackState": value.media.playback_status,
                    "itemId": value.media.uri,
                    "positionMillis": value.media.position,
                    "previousItemId": "",
                    "previousPositionMillis": "",
                    "isDucking": False,
                    "queueVersion": "",
                    "availablePlaybackActions": {
                        "canCrossfade": True,
                        "canPause": True,
                        "canRepeat": True,
                        "canRepeatOne": True,
                        "canSeek": True,
                        "canShuffle": True,
                        "canSkip": True,
                        "canSkipBack": True,
                        "canStop": True
                    }}

            if value.media.play_mode == "NORMAL":
                return_json["playModes"] = {
                    "repeat": False,
                    "repeatOne": False,
                    "crossfade": value.cross_fade,
                    "shuffle": False
                }
            elif value.media.play_mode == "SHUFFLE":
                return_json["playModes"] = {
                    "repeat": False,
                    "repeatOne": False,
                    "crossfade": value.cross_fade,
                    "shuffle": True
                }
            elif value.media.play_mode == "REPEAT_ALL":
                return_json["playModes"] = {
                    "repeat": True,
                    "repeatOne": False,
                    "crossfade": value.cross_fade,
                    "shuffle": False
                }
            elif value.media.play_mode == "REPEAT_ONE":
                return_json["playModes"] = {
                    "repeat": False,
                    "repeatOne": True,
                    "crossfade": value.cross_fade,
                    "shuffle": False
                }
            elif value.media.play_mode == "SHUFFLE_NOREPEAT":
                return_json["playModes"] = {
                    "repeat": False,
                    "repeatOne": False,
                    "crossfade": value.cross_fade,
                    "shuffle": True
                }
            elif value.media.play_mode == "SHUFFLE_REPEAT_ONE":
                return_json["playModes"] = {
                    "repeat": False,
                    "repeatOne": True,
                    "crossfade": value.cross_fade,
                    "shuffle": True
                }
                return_li.append(return_json)

        msg_json["data"] = return_li
        msg_json["count"] = len(return_li)
        return self.json(msg_json)


class APIAutomationView(HomeAssistantView):
    """获取自动化信息"""

    url = "/api/automation"
    name = "api:automation"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        data = hass.data["automation"]
        msg_json = {"code": 200, "message": "success"}
        automation_li = []
        for key, value in data._entities.items():
            automation_li.append(value.raw_config)
        msg_json["data"] = automation_li
        msg_json["count"] = len(automation_li)
        return self.json(msg_json)


class APISceneView(HomeAssistantView):
    """获取场景信息"""

    url = "/api/scene"
    name = "api:scene"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        data = hass.data["scene"].config.get("scene")
        msg_json = {"code": 200, "message": "success", "data": data, "count": len(data)}

        return self.json(msg_json)


class APIStatusView(HomeAssistantView):
    """View to handle Status requests."""

    url = URL_API
    name = "api:status"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        """Retrieve if API is running."""
        return self.json_message("API auth running.")


class APICoreStateView(HomeAssistantView):
    """View to handle core state requests."""

    url = URL_API_CORE_STATE
    name = "api:core:state"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        """Retrieve the current core state.

        This API is intended to be a fast and lightweight way to check if the
        Home Assistant core is running. Its primary use case is for supervisor
        to check if Home Assistant is running.
        """
        hass = request.app[KEY_HASS]
        return self.json({"state": hass.state.value})


class APIEventStream(HomeAssistantView):
    """View to handle EventStream requests."""

    url = URL_API_STREAM
    name = "api:stream"

    @require_admin
    async def get(self, request: web.Request) -> web.StreamResponse:
        """Provide a streaming interface for the event bus."""
        hass = request.app[KEY_HASS]
        stop_obj = object()
        to_write: asyncio.Queue[object | str] = asyncio.Queue()

        restrict: list[EventType[Any] | str] | None = None
        if restrict_str := request.query.get("restrict"):
            restrict = [*restrict_str.split(","), EVENT_HOMEASSISTANT_STOP]

        async def forward_events(event: Event) -> None:
            """Forward events to the open request."""
            if restrict and event.event_type not in restrict:
                return

            _LOGGER.debug("STREAM %s FORWARDING %s", id(stop_obj), event)

            if event.event_type == EVENT_HOMEASSISTANT_STOP:
                data = stop_obj
            else:
                data = json_dumps(event)

            await to_write.put(data)

        response = web.StreamResponse()
        response.content_type = "text/event-stream"
        await response.prepare(request)

        unsub_stream = hass.bus.async_listen(MATCH_ALL, forward_events)

        try:
            _LOGGER.debug("STREAM %s ATTACHED", id(stop_obj))

            # Fire off one message so browsers fire open event right away
            await to_write.put(STREAM_PING_PAYLOAD)

            while True:
                try:
                    async with timeout(STREAM_PING_INTERVAL):
                        payload = await to_write.get()

                    if payload is stop_obj:
                        break

                    msg = f"data: {payload}\n\n"
                    _LOGGER.debug("STREAM %s WRITING %s", id(stop_obj), msg.strip())
                    await response.write(msg.encode("UTF-8"))
                except TimeoutError:
                    await to_write.put(STREAM_PING_PAYLOAD)

        except asyncio.CancelledError:
            _LOGGER.debug("STREAM %s ABORT", id(stop_obj))

        finally:
            _LOGGER.debug("STREAM %s RESPONSE CLOSED", id(stop_obj))
            unsub_stream()

        return response


class APIConfigView(HomeAssistantView):
    """View to handle Configuration requests."""

    url = URL_API_CONFIG
    name = "api:config"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        """Get current configuration."""
        return self.json(request.app[KEY_HASS].config.as_dict())


class APIStatesView(HomeAssistantView):
    """View to handle States requests."""

    url = URL_API_STATES
    name = "api:states"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        """Get current states."""
        user: User = request[KEY_HASS_USER]
        hass = request.app[KEY_HASS]
        if user.is_admin:
            states = (state.as_dict_json for state in hass.states.async_all())
        else:
            entity_perm = user.permissions.check_entity
            states = (
                state.as_dict_json
                for state in hass.states.async_all()
                if entity_perm(state.entity_id, "read")
            )
        response = web.Response(
            body=b"".join((b"[", b",".join(states), b"]")),
            content_type=CONTENT_TYPE_JSON,
            zlib_executor_size=32768,
        )
        response.enable_compression()
        return response


class APIEntityStateView(HomeAssistantView):
    """View to handle EntityState requests."""

    url = "/api/states/{entity_id}"
    name = "api:entity-state"

    @ha.callback
    def get(self, request: web.Request, entity_id: str) -> web.Response:
        """Retrieve state of entity."""
        user: User = request[KEY_HASS_USER]
        hass = request.app[KEY_HASS]
        if not user.permissions.check_entity(entity_id, POLICY_READ):
            raise Unauthorized(entity_id=entity_id)

        if state := hass.states.get(entity_id):
            return web.Response(
                body=state.as_dict_json,
                content_type=CONTENT_TYPE_JSON,
            )
        return self.json_message("Entity not found.", HTTPStatus.NOT_FOUND)

    async def post(self, request: web.Request, entity_id: str) -> web.Response:
        """Update state of entity."""
        user: User = request[KEY_HASS_USER]
        if not user.is_admin:
            raise Unauthorized(entity_id=entity_id)
        hass = request.app[KEY_HASS]
        try:
            data = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON specified.", HTTPStatus.BAD_REQUEST)

        if (new_state := data.get("state")) is None:
            return self.json_message("No state specified.", HTTPStatus.BAD_REQUEST)

        attributes = data.get("attributes")
        force_update = data.get("force_update", False)

        is_new_state = hass.states.get(entity_id) is None

        # Write state
        try:
            hass.states.async_set(
                entity_id, new_state, attributes, force_update, self.context(request)
            )
        except InvalidEntityFormatError:
            return self.json_message(
                "Invalid entity ID specified.", HTTPStatus.BAD_REQUEST
            )
        except InvalidStateError:
            return self.json_message("Invalid state specified.", HTTPStatus.BAD_REQUEST)

        # Read the state back for our response
        status_code = HTTPStatus.CREATED if is_new_state else HTTPStatus.OK
        state = hass.states.get(entity_id)
        assert state
        resp = self.json(state.as_dict(), status_code)

        resp.headers.add("Location", f"/api/states/{entity_id}")

        return resp

    @ha.callback
    def delete(self, request: web.Request, entity_id: str) -> web.Response:
        """Remove entity."""
        if not request[KEY_HASS_USER].is_admin:
            raise Unauthorized(entity_id=entity_id)
        if request.app[KEY_HASS].states.async_remove(entity_id):
            return self.json_message("Entity removed.")
        return self.json_message("Entity not found.", HTTPStatus.NOT_FOUND)


class APIEventListenersView(HomeAssistantView):
    """View to handle EventListeners requests."""

    url = URL_API_EVENTS
    name = "api:event-listeners"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        """Get event listeners."""
        return self.json(async_events_json(request.app[KEY_HASS]))


class APIEventView(HomeAssistantView):
    """View to handle Event requests."""

    url = "/api/events/{event_type}"
    name = "api:event"

    @require_admin
    async def post(self, request: web.Request, event_type: str) -> web.Response:
        """Fire events."""
        body = await request.text()
        try:
            event_data: Any = json_loads(body) if body else None
        except ValueError:
            return self.json_message(
                "Event data should be valid JSON.", HTTPStatus.BAD_REQUEST
            )

        if event_data is not None and not isinstance(event_data, dict):
            return self.json_message(
                "Event data should be a JSON object", HTTPStatus.BAD_REQUEST
            )

        # Special case handling for event STATE_CHANGED
        # We will try to convert state dicts back to State objects
        if event_type == EVENT_STATE_CHANGED and event_data:
            for key in ("old_state", "new_state"):
                state = ha.State.from_dict(event_data[key])

                if state:
                    event_data[key] = state

        request.app[KEY_HASS].bus.async_fire(
            event_type, event_data, ha.EventOrigin.remote, self.context(request)
        )

        return self.json_message(f"Event {event_type} fired.")


class APIServicesView(HomeAssistantView):
    """View to handle Services requests."""

    url = URL_API_SERVICES
    name = "api:services"

    async def get(self, request: web.Request) -> web.Response:
        """Get registered services."""
        services = await async_services_json(request.app[KEY_HASS])
        return self.json(services)


class APIDomainServicesView(HomeAssistantView):
    """View to handle DomainServices requests."""

    url = "/api/services/{domain}/{service}"
    name = "api:domain-services"

    async def post(
            self, request: web.Request, domain: str, service: str
    ) -> web.Response:
        """Call a service.

        Returns a list of changed states.
        """
        hass = request.app[KEY_HASS]
        body = await request.text()
        try:
            data = json_loads(body) if body else None
        except ValueError:
            return self.json_message(
                "Data should be valid JSON.", HTTPStatus.BAD_REQUEST
            )

        context = self.context(request)
        changed_states: list[json_fragment] = []

        @ha.callback
        def _async_save_changed_entities(
                event: Event[EventStateChangedData],
        ) -> None:
            if event.context == context and (state := event.data["new_state"]):
                changed_states.append(state.json_fragment)

        cancel_listen = hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            _async_save_changed_entities,
        )

        try:
            # shield the service call from cancellation on connection drop
            await shield(
                hass.services.async_call(
                    domain,
                    service,
                    data,  # type: ignore[arg-type]
                    blocking=True,
                    context=context,
                )
            )
        except (vol.Invalid, ServiceNotFound) as ex:
            raise HTTPBadRequest from ex
        finally:
            cancel_listen()

        return self.json(changed_states)


class APIComponentsView(HomeAssistantView):
    """View to handle Components requests."""

    url = URL_API_COMPONENTS
    name = "api:components"

    @ha.callback
    def get(self, request: web.Request) -> web.Response:
        """Get current loaded components."""
        return self.json(request.app[KEY_HASS].config.components)


@lru_cache
def _cached_template(template_str: str, hass: HomeAssistant) -> template.Template:
    """Return a cached template."""
    return template.Template(template_str, hass)


class APITemplateView(HomeAssistantView):
    """View to handle Template requests."""

    url = URL_API_TEMPLATE
    name = "api:template"

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        """Render a template."""
        try:
            data = await request.json()
            tpl = _cached_template(data["template"], request.app[KEY_HASS])
            return tpl.async_render(variables=data.get("variables"), parse_result=False)  # type: ignore[no-any-return]
        except (ValueError, TemplateError) as ex:
            return self.json_message(
                f"Error rendering template: {ex}", HTTPStatus.BAD_REQUEST
            )


class APIErrorLog(HomeAssistantView):
    """View to fetch the API error log."""

    url = URL_API_ERROR_LOG
    name = "api:error_log"

    @require_admin
    async def get(self, request: web.Request) -> web.FileResponse:
        """Retrieve API error log."""
        hass = request.app[KEY_HASS]
        response = web.FileResponse(hass.data[DATA_LOGGING])
        response.enable_compression()
        return response


async def async_services_json(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Generate services data to JSONify."""
    descriptions = await async_get_all_descriptions(hass)
    return [{"domain": key, "services": value} for key, value in descriptions.items()]


@ha.callback
def async_events_json(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Generate event data to JSONify."""
    return [
        {"event": key, "listener_count": value}
        for key, value in hass.bus.async_listeners().items()
    ]
