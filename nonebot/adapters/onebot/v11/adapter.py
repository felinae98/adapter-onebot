import hmac
import json
import asyncio
import inspect
from typing import Any, Dict, List, Type, Union, Callable, Optional, cast

from pygtrie import StringTrie
from nonebot.typing import overrides
from nonebot.utils import DataclassEncoder, escape_tag
from nonebot.drivers import (
    URL,
    Driver,
    Request,
    Response,
    WebSocket,
    ForwardDriver,
    ReverseDriver,
    HTTPServerSetup,
    WebSocketServerSetup,
)

from nonebot.adapters import Adapter as BaseAdapter

from . import event
from .bot import Bot
from .config import Config
from .event import Event, LifecycleMetaEvent
from .message import Message, MessageSegment
from .exception import NetworkError, ApiNotAvailable
from .utils import ResultStore, log, get_auth_bearer, _handle_api_result

RECONNECT_INTERVAL = 3.0


class Adapter(BaseAdapter):
    # init all event models
    event_models: StringTrie = StringTrie(separator=".")
    for model_name in dir(event):
        model = getattr(event, model_name)
        if not inspect.isclass(model) or not issubclass(model, Event):
            continue
        event_models["." + model.__event__] = model

    @overrides(BaseAdapter)
    def __init__(self, driver: Driver, **kwargs: Any):
        super().__init__(driver, **kwargs)
        self.onebot_config: Config = Config(**self.config.dict())
        self.connections: Dict[str, WebSocket] = {}
        self.tasks: List[asyncio.Task] = []
        self.setup()

    @classmethod
    @overrides(BaseAdapter)
    def get_name(cls) -> str:
        return "OneBot V11"

    def setup(self) -> None:
        if isinstance(self.driver, ReverseDriver):
            http_setup = HTTPServerSetup(
                URL("/onebot/v11/http"), "POST", self.get_name(), self._handle_http
            )
            self.setup_http_server(http_setup)
            http_setup = HTTPServerSetup(
                URL("/onebot/v11/"), "POST", self.get_name(), self._handle_http
            )
            self.setup_http_server(http_setup)

            ws_setup = WebSocketServerSetup(
                URL("/onebot/v11/ws"), self.get_name(), self._handle_ws
            )
            self.setup_websocket_server(ws_setup)

        if self.onebot_config.onebot_ws_urls:
            if not isinstance(self.driver, ForwardDriver):
                log(
                    "WARNING",
                    f"Current driver {self.config.driver} don't support forward connections! Ignored",
                )
            else:
                self.driver.on_startup(self.start_forward)
                self.driver.on_shutdown(self.stop_forward)

    @overrides(BaseAdapter)
    async def _call_api(self, bot: Bot, api: str, **data) -> Any:
        websocket = self.connections.get(bot.self_id, None)
        log("DEBUG", f"Calling API <y>{api}</y>")
        if websocket:
            seq = ResultStore.get_seq()
            json_data = json.dumps(
                {"action": api, "params": data, "echo": {"seq": seq}},
                cls=DataclassEncoder,
            )
            await websocket.send(json_data)
            return _handle_api_result(
                await ResultStore.fetch(bot.self_id, seq, self.config.api_timeout)
            )

        elif isinstance(self.driver, ForwardDriver):
            api_root = self.config.api_root.get(bot.self_id)
            if not api_root:
                raise ApiNotAvailable
            elif not api_root.endswith("/"):
                api_root += "/"

            headers = {"Content-Type": "application/json"}
            if self.onebot_config.onebot_access_token is not None:
                headers["Authorization"] = (
                    "Bearer " + self.onebot_config.onebot_access_token
                )

            request = Request(
                "POST",
                api_root + api,
                headers=headers,
                content=json.dumps(data, cls=DataclassEncoder),
                timeout=self.config.api_timeout,
            )

            try:
                response = await self.driver.request(request)

                if 200 <= response.status_code < 300:
                    if not response.content:
                        raise ValueError("Empty response")
                    result = json.loads(response.content)
                    return _handle_api_result(result)
                raise NetworkError(
                    f"HTTP request received unexpected "
                    f"status code: {response.status_code}"
                )
            except NetworkError:
                raise
            except Exception as e:
                raise NetworkError("HTTP request failed") from e
        else:
            raise ApiNotAvailable

    async def _handle_http(self, request: Request) -> Response:
        self_id = request.headers.get("x-self-id")

        # check self_id
        if not self_id:
            log("WARNING", "Missing X-Self-ID Header")
            return Response(400, content="Missing X-Self-ID Header")

        # check signature
        response = self._check_signature(request)
        if response is not None:
            return response

        # check access_token
        response = self._check_access_token(request)
        if response is not None:
            return response

        data = request.content
        if data is not None:
            json_data = json.loads(data)
            event = self.json_to_event(json_data)
            if event:
                bot = self.bots.get(self_id)
                if not bot:
                    bot = Bot(self, self_id)
                    self.bot_connect(bot)
                    log("INFO", f"<y>Bot {escape_tag(self_id)}</y> connected")
                bot = cast(Bot, bot)
                asyncio.create_task(bot.handle_event(event))
        return Response(204)

    async def _handle_ws(self, websocket: WebSocket) -> None:
        self_id = websocket.request.headers.get("x-self-id")

        # check self_id
        if not self_id:
            log("WARNING", "Missing X-Self-ID Header")
            await websocket.close(1008, "Missing X-Self-ID Header")
            return
        elif self_id in self.bots:
            log("WARNING", f"There's already a bot {self_id}, ignored")
            await websocket.close(1008, "Duplicate X-Self-ID")
            return

        # check access_token
        response = self._check_access_token(websocket.request)
        if response is not None:
            content = cast(str, response.content)
            await websocket.close(1008, content)
            return

        await websocket.accept()
        bot = Bot(self, self_id)
        self.connections[self_id] = websocket
        self.bot_connect(bot)

        log("INFO", f"<y>Bot {escape_tag(self_id)}</y> connected")

        try:
            while True:
                data = await websocket.receive()
                json_data = json.loads(data)
                event = self.json_to_event(json_data)
                if event:
                    asyncio.create_task(bot.handle_event(event))
        except Exception as e:
            log(
                "ERROR",
                "<r><bg #f8bbd0>Error while process data from websocket"
                f"for bot {escape_tag(self_id)}.</bg #f8bbd0></r>",
                e,
            )
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
            self.connections.pop(self_id, None)
            self.bot_disconnect(bot)

    def _check_signature(self, request: Request) -> Optional[Response]:
        x_signature = request.headers.get("x-signature")

        secret = self.onebot_config.onebot_secret
        if secret:
            if not x_signature:
                log("WARNING", "Missing Signature Header")
                return Response(401, content="Missing Signature", request=request)

            if request.content is None:
                return Response(400, content="Missing Content", request=request)

            body: bytes = (
                request.content
                if isinstance(request.content, bytes)
                else request.content.encode("utf-8")
            )
            sig = hmac.new(secret.encode("utf-8"), body, "sha1").hexdigest()
            if x_signature != "sha1=" + sig:
                log("WARNING", "Signature Header is invalid")
                return Response(403, content="Signature is invalid")

    def _check_access_token(self, request: Request) -> Optional[Response]:
        token = get_auth_bearer(request.headers.get("authorization"))

        access_token = self.onebot_config.onebot_access_token
        if access_token and access_token != token:
            msg = (
                "Authorization Header is invalid"
                if token
                else "Missing Authorization Header"
            )
            log(
                "WARNING",
                msg,
            )
            return Response(
                403,
                content=msg,
            )

    async def start_forward(self) -> None:
        for url in self.onebot_config.onebot_ws_urls:
            try:
                ws_url = URL(url)
                self.tasks.append(asyncio.create_task(self._forward_ws(ws_url)))
            except Exception as e:
                log(
                    "ERROR",
                    f"<r><bg #f8bbd0>Bad url {escape_tag(url)} "
                    "in onebot forward websocket config</bg #f8bbd0></r>",
                    e,
                )

    async def stop_forward(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _forward_ws(self, url: URL) -> None:
        headers = {}
        if self.onebot_config.onebot_access_token:
            headers[
                "Authorization"
            ] = f"Bearer {self.onebot_config.onebot_access_token}"
        request = Request("GET", url, headers=headers)

        bot: Optional[Bot] = None

        while True:
            try:
                ws = await self.websocket(request)
            except Exception as e:
                log(
                    "ERROR",
                    "<r><bg #f8bbd0>Error while setup websocket to "
                    f"{escape_tag(str(url))}. Trying to reconnect...</bg #f8bbd0></r>",
                    e,
                )
                await asyncio.sleep(RECONNECT_INTERVAL)
                continue

            log("DEBUG", f"WebSocket Connection to {escape_tag(str(url))} established")
            try:
                while True:
                    try:
                        data = await ws.receive()
                        json_data = json.loads(data)
                        event = self.json_to_event(json_data, bot and bot.self_id)
                        if not event:
                            continue
                        if not bot:
                            if (
                                not isinstance(event, LifecycleMetaEvent)
                                or event.sub_type != "connect"
                            ):
                                continue
                            self_id = event.self_id
                            bot = Bot(self, str(self_id))
                            self.connections[str(self_id)] = ws
                            self.bot_connect(bot)
                            log(
                                "INFO",
                                f"<y>Bot {escape_tag(str(self_id))}</y> connected",
                            )
                        asyncio.create_task(bot.handle_event(event))
                    except Exception as e:
                        log(
                            "ERROR",
                            "<r><bg #f8bbd0>Error while process data from websocket"
                            f"{escape_tag(str(url))}. Trying to reconnect...</bg #f8bbd0></r>",
                            e,
                        )
                        break
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
                if bot:
                    self.connections.pop(bot.self_id, None)
                    self.bot_disconnect(bot)
                    bot = None

            await asyncio.sleep(RECONNECT_INTERVAL)

    @classmethod
    def json_to_event(
        cls, json_data: Any, self_id: Optional[str] = None
    ) -> Optional[Event]:
        if not isinstance(json_data, dict):
            return None

        if "post_type" not in json_data:
            if self_id is not None:
                ResultStore.add_result(self_id, json_data)
            return

        try:
            post_type = json_data["post_type"]
            detail_type = json_data.get(f"{post_type}_type")
            detail_type = f".{detail_type}" if detail_type else ""
            sub_type = json_data.get("sub_type")
            sub_type = f".{sub_type}" if sub_type else ""
            models = cls.get_event_model(post_type + detail_type + sub_type)
            for model in models:
                try:
                    event = model.parse_obj(json_data)
                    break
                except Exception as e:
                    log("DEBUG", "Event Parser Error", e)
            else:
                event = Event.parse_obj(json_data)

            return event
        except Exception as e:
            log(
                "ERROR",
                "<r><bg #f8bbd0>Failed to parse event. "
                f"Raw: {escape_tag(str(json_data))}</bg #f8bbd0></r>",
                e,
            )

    @classmethod
    def add_custom_model(cls, model: Type[Event]) -> None:
        if not model.__event__:
            raise ValueError("Event model's `__event__` attribute must be set")
        cls.event_models["." + model.__event__] = model

    @classmethod
    def get_event_model(cls, event_name: str) -> List[Type[Event]]:
        """
        :说明:

          根据事件名获取对应 ``Event Model`` 及 ``FallBack Event Model`` 列表, 不包括基类 ``Event``

        :返回:

          - ``List[Type[Event]]``
        """
        return [model.value for model in cls.event_models.prefixes("." + event_name)][
            ::-1
        ]

    @classmethod
    def custom_send(
        cls,
        send_func: Callable[[Bot, Event, Union[str, Message, MessageSegment]], None],
    ):
        setattr(Bot, "send_handler", send_func)
