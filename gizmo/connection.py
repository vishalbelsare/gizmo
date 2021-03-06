import asyncio
import copy
import json
import logging
import uuid

import websockets

from .exception import AstronomerConnectionException
from .util import _query_debug, Timer


logger = logging.getLogger(__name__)


class RequestQueryLogger:

    def __init__(self):
        self.queries = []

    def add(self, script, params, query, execution_time):
        self.queries.append({
            'query': query,
            'script': script,
            'params': params,
            'query': query,
            'execution_time': execution_time,
        })

    def reset(self):
        self.queries = []

    @property
    def total_time(self):
        return sum(q['execution_time'] for q in self.queries)

    def __len__(self):
        return len(self.queries)

    def __add__(self, other):
        if isinstance(other, RequestQueryLogger):
            self.queries += copy.deepcopy(other.queries)


class Request:

    def __init__(self, uri, port=8182, three_two=True, username=None,
                 password=None, log_requests=None):
        gremlin = '/gremlin' if three_two else ''
        self.uri = uri
        self.port = port
        self.three_two = three_two
        self._ws_uri = 'ws://{}:{}{}'.format(uri, port, gremlin)
        self.username = username
        self.password = password
        self.connection = None

        if log_requests:
            log_requests = RequestQueryLogger()

        self.request_logger = log_requests

    def connect(self):
        self.connection = websockets.connect(self._ws_uri)

    def message(self, script, params=None, rebindings=None, op='eval',
                processor=None, language='gremlin-groovy', session=None):
        message = {
            'requestId': str(uuid.uuid4()),
            'op': op,
            'processor': processor or '',
            'args': {
                'gremlin': script,
                'bindings': params,
                'language': language,
                'rebindings': rebindings or {},
            }
        }

        # TODO: add session

        return json.dumps(message)

    async def send(self, script=None, params=None, update_entities=None,
                   rebindings=None, op='eval', processor=None,
                   language='gremlin-groovy', session=None):
        data = []
        status = ResponseStatus(500, '')
        request_id = None
        result = None
        params = params  or {}
        update_entities = update_entities or {}
        query = _query_debug(script, params)
        request_logger = self.request_logger

        logger.debug('RUNNING QUERY WITH PARAMS')
        logger.debug(script)
        logger.debug(params)
        logger.debug(query)

        try:
            with Timer() as timer:
                self.connect()

                async with self.connection as ws:
                    message = self.message(script=script, params=params,
                        rebindings=rebindings, op=op, processor=processor,
                            language=language, session=session)

                    await ws.send(message)

                    response = await ws.recv()
                    data = json.loads(response)

                    if data.get('request_id'):
                        request_id = data['request_id']

                    if data.get('result'):
                        result = data['result']

                    if data.get('status'):
                        status = ResponseStatus(**data['status'])

            logger.debug('runtime: {} miliseconds\n'.format(timer.elapsed))

            if request_logger is not None:
                request_logger.add(script, params, query, timer.elapsed)

            return Response(request_id=request_id, result=result,
                            update_entities=update_entities, script=script,
                            params=params)
        except Exception as e:
            raise AstronomerConnectionException(e)


class ResponseStatus:

    def __init__(self, code, message, attributes=None):
        self.code = code
        self.message = message
        self.attributes = attributes


class Response:

    def __init__(self, request_id=None, result=None, update_entities=None,
                 script=None, params=None, status=None):
        self.request_id = request_id
        self.result = result or {}
        self.update_entities = update_entities or {}
        self.script = script
        self.params = params
        self.status = status

        self.translate()

    def _fix_titan_data(self, data):
        """temp method to address a titan bug where it returns maps in a
        different manner than other tinkerpop instances. This will be fixed
        in a later version of titan"""
        if isinstance(data, (list, tuple,)):
            fixed = []

            for ret in data:
                if isinstance(ret, dict):
                    if 'key' in ret and 'value' in ret:
                        fixed.append({ret['key']: ret['value']})

            if len(data) and not len(fixed):
                return data
            else:
                return fixed
        else:
            return data

    def translate(self):
        response = []
        data = self._fix_titan_data(self.result.get('data') or [])
        update_keys = list(self.update_entities.keys())

        def has_update(keys):
            for k in keys:
                if k in update_keys:
                    return True

            return False

        def fix_properties(arg):
            props = copy.deepcopy(arg)

            if 'properties' in arg:
                props.update(arg['properties'])
                del(props['properties'])

            props.update({
                'id': arg.get('id'),
                'type': arg.get('type'),
                'label': arg.get('label'),
            })

            return props

        for arg in data:
            if not hasattr(arg, '__iter__'):
                response = [{'response': arg}]
            elif isinstance(arg, dict):
                if has_update(arg.keys()):
                    for k, v in arg.items():
                        if k in self.update_entities:
                            entity = self.update_entities[k]
                            props = fix_properties(v)

                            response.append(props)
                            entity.empty().hydrate(props, reset_initial=True)
                else:
                    response.append(fix_properties(arg))
            else:
                response.append(arg)

        return response

    @property
    def data(self):
        return self.translate()

    def __getitem__(self, key):
        val = None

        try:
            data = self.data[key]
            val = copy.deepcopy(data)
            #
            # if '_properties' in data:
            #     del val['_properties']
            #     val.update(data['_properties'])
        except:
            pass

        return val

    def update_entities(self, mappings):
        fixed = copy.deepcopy(self.data)

        for var, entity in mappings.items():
            if var in self.data:
                entity.hydrate(self.data[var])

                try:
                    del fixed[var]
                except:
                    pass

                fixed.update(self.data[var])

        self.data = fixed

        return self

    def __setitem__(self, key, val):
        self.data[key] = val

        return self
