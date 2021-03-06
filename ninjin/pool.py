import asyncio
import inspect
import json
import re
import uuid
from collections import UserDict

import aio_pika
from aio_pika import (
    DeliveryMode,
    IncomingMessage,
    Message
)

from ninjin.exceptions import (
    ImproperlyConfigured,
    IncorrectMessage,
    UnknownConsumer
)
from ninjin.logger import logger
from ninjin.schema import PayloadSchema

schema = PayloadSchema()
SCHEDULER_RESOURCE_NAME = '_scheduler'


class QueuePool:
    exchange = None
    exchange_delayed = None

    queues = {}
    queue_main = None
    queue_callback = None
    queue_schedule = None

    resources = {}

    futures = {}

    def __init__(self, pool: 'Pool',
                 exchange_name,
                 exchange_type='topic',
                 exchange_durable=True,
                 exchange_auto_delete=False):
        super().__init__()
        self.pool = pool
        self.channel = pool.channel
        self.exchange_name = exchange_name
        self.exchange_type = exchange_type
        self.exchange_durable = exchange_durable
        self.exchange_auto_delete = exchange_auto_delete
        self.rpc_name = '{}.rpc.{}'.format(
            self.pool.service_name,
            str(uuid.uuid4())
        )
        self.delayed_name = '{}.delayed'.format(
            self.pool.service_name
        )

    async def connect(self):
        if self.exchange_name:
            self.exchange = await self.channel.declare_exchange(
                name=self.exchange_name,
                type=self.exchange_type,
                durable=self.exchange_durable,
                auto_delete=self.exchange_auto_delete
            )
        else:
            self.exchange = self.channel.default_exchange

        self.queue_callback = await self.channel.declare_queue(
            name=self.rpc_name,
            durable=False,
            exclusive=True
        )
        await self.queue_callback.bind(self.exchange)

        self.exchange_delayed = await self.channel.declare_exchange(
            name='{}.delayed'.format(
                self.exchange_name
            ),
            type='x-delayed-message',
            arguments={
                'x-delayed-type': 'topic'
            },
            durable=True,
            auto_delete=False
        )

        self.queue_schedule = await self.channel.declare_queue(
            name=self.delayed_name,
            durable=True,
            exclusive=False
        )
        await self.queue_schedule.bind(self.exchange_delayed)

    async def add_handler(self, consumer_key, resource):
        if not self.channel:
            raise ImproperlyConfigured('You must connect the broker first')

        if consumer_key not in self.queues:
            queue = await self.channel.declare_queue(
                name=consumer_key,
                durable=True
            )
            await queue.bind(self.exchange)
            self.queues[consumer_key] = queue

        resource_name = resource.resource_name()

        if resource_name in self.resources:
            if resource_name:
                raise ImproperlyConfigured('{} already registered'.format(resource_name))
        self.resources[resource_name] = resource

        # start scheduled tasks
        for task in resource.periodic_tasks.values():
            await task(resource(deserialized_data={}, message=None))

    async def _on_rpc_response(self, message: IncomingMessage):
        async with message.process(requeue=False):
            logger.debug(msg='Received PRC result: {}'.format(message.body))
            try:
                f = self.futures.pop(message.correlation_id)
                f.set_result(json.loads(message.body.decode()))
            except IndexError:
                pass

    async def _on_delayed_message(self, message: IncomingMessage):
        async with message.process(requeue=False):
            deserialized_data = schema.loads(message.body)
            logger.debug(msg='Received delayed message: {}'.format(deserialized_data))
            unwrapped_payload = deserialized_data.get('payload')
            # publish message to myself or neighbour
            await self.pool.publish(
                service_name=deserialized_data.get('forward'),
                payload=unwrapped_payload.get('payload'),
                remote_handler=unwrapped_payload['handler'],
                remote_resource=unwrapped_payload['resource']
            )

    async def _on_message(self, message: IncomingMessage):
        async with message.process(requeue=False):
            deserialized_data = schema.loads(message.body)
            logger.debug(msg='Received message: {}'.format(deserialized_data))
            resource_name = deserialized_data.get('resource')
            try:
                resource = self.resources[resource_name]
            except KeyError:
                error_msg = 'Resource {} does not registered'.format(resource_name)
                logger.info(error_msg)
                raise UnknownConsumer(error_msg)
            r = resource(deserialized_data, message)
            await r.dispatch()

    async def publish(self, routing_key, data, **kwargs):
        reply_to = self.rpc_name if 'correlation_id' in kwargs else None
        period = data.get('period')
        delay = period or data.get('delay')
        delayed = period or delay

        exchange = self.exchange
        headers = {}
        if delayed:
            exchange = self.exchange_delayed
            headers = {
                'x-delay': delay
            }
            routing_key = self.delayed_name

        await exchange.publish(
            Message(
                body=schema.dumps(data),
                content_type="application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
                reply_to=reply_to,
                headers=headers,
                **kwargs
            ),
            routing_key=routing_key
        )

    async def consume(self):
        await asyncio.gather(
            *[
                self.queue_callback.consume(callback=self._on_rpc_response),
                self.queue_schedule.consume(callback=self._on_delayed_message),
                *[queue.consume(callback=self._on_message) for queue in self.queues.values()],
            ]
        )

    async def future(self):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.futures[correlation_id] = future
        return future, correlation_id


class Pool(UserDict):
    connection = None
    queues = None
    channel = None

    def __init__(self,
                 service_name,
                 host='localhost',
                 port=5672,
                 login='guest',
                 password='guest',
                 exchange_name=None,
                 *args, **kwargs):
        """
        :return:

        :param service_name:
        :param host:
        :param port:
        :param login:
        :param password:
        :param exchange_name:
        :param exchange_type:
        :param exchange_durable:
        :param exchange_auto_delete:
        :param requeue:
        :param args:
        :param kwargs:
        """
        super(Pool, self).__init__()
        self.service_name = service_name
        self.host = host
        self.port = port
        self.login = login
        self.password = password
        self.exchange_name = exchange_name

    async def __aenter__(self):
        # TODO
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, exc_traceback):
        # TODO
        await self.close()

    async def connect(self):
        loop = asyncio.get_event_loop()
        credentials = dict(
            host=self.host,
            port=self.port,
            login=self.login
        )
        try:
            connection = await aio_pika.connect_robust(
                password=self.password,
                loop=loop,
                **credentials
            )
        except ConnectionError as e:
            logger.error(msg='{e}, {login}@{host}:{port}'.format(
                e=e, **credentials
            ))
            await asyncio.sleep(5)
            return await self.connect()

        self.connection = connection
        self.channel = await connection.channel()
        self.queues = QueuePool(
            pool=self,
            exchange_name=self.exchange_name
        )
        await self.queues.connect()

    async def close(self):
        await self.connection.close()

    def register_function(self, handler, consumer_key=None, handler_name=None):
        if not inspect.iscoroutinefunction(handler):
            raise ImproperlyConfigured('Only coroutine can be registered')
        resource_name = None
        consumer_key = consumer_key or self.service_name
        # TODO
        # self[consumer_key][resource_name] = type('SimpleResource', (Resource,), {handler_name: handler})

    async def register(self, resource: 'Resource', consumer_key=None):
        actors = {}
        periodic_tasks = {}

        for att in map(lambda x: getattr(resource, x), dir(resource)):
            if getattr(att, 'is_actor', False) is True:
                actors[att.__name__] = att
            if getattr(att, 'is_periodic_task', False) is True:
                periodic_tasks[att.__name__] = att
        resource = type(resource.__name__, (resource,), {
            'pool': self,
            'actors': actors,
            'periodic_tasks': periodic_tasks
        })
        consumer_key = consumer_key or resource.consumer_key or self.service_name
        await self.queues.add_handler(consumer_key, resource)

    async def start(self):
        await self.queues.consume()

    async def publish(
            self,
            payload,
            service_name: str,
            remote_resource=None,
            remote_handler='default',
            correlation_id=None,
            pagination=None,
    ):
        """
        publish message to queue.

        Delayed messages cannot reply to listener
        :param payload:
        :param service_name:
        :param remote_resource:
        :param remote_handler:
        :param correlation_id:
        :param pagination:
        :return:
        """
        if payload is None:
            raise IncorrectMessage('Cannot publish empty message from')

        data = dict(
            payload=payload,
            resource=remote_resource,
            handler=remote_handler,
            pagination=pagination,
        )
        await self.queues.publish(
            routing_key=service_name,
            data=data,
            correlation_id=correlation_id,
        )

    async def rpc(
            self,
            payload,
            service_name: str = None,
            remote_resource=None,
            remote_handler='default'
    ):
        future, correlation_id = await self.queues.future()
        await self.publish(
            payload,
            service_name=service_name,
            remote_resource=remote_resource,
            remote_handler=remote_handler,
            correlation_id=correlation_id
        )
        return await future

    async def schedule(
            self,
            payload,
            service_name: str = None,
            remote_resource=None,
            remote_handler='default',
            delay=None,
            period=None
    ):
        if not (period or delay):
            return
        service_name = service_name or self.service_name
        message_to_proceed = dict(
            payload=payload,
            resource=remote_resource,
            handler=remote_handler,
        )

        wrapped_message = dict(
            payload=message_to_proceed,
            resource=SCHEDULER_RESOURCE_NAME,
            handler=SCHEDULER_RESOURCE_NAME,

            delay=period or delay,
            forward=service_name,
            period=period
        )
        # publish message to myself
        await self.queues.publish(
            routing_key=self.service_name,
            data=wrapped_message
        )
