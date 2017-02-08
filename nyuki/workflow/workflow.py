import pickle
import asyncio
import logging
from datetime import datetime
from aioredis import create_redis
from tukio import Engine, TaskRegistry, get_broker, EXEC_TOPIC
from tukio.workflow import (
    TemplateGraphError, Workflow, WorkflowTemplate, WorkflowExecState
)

from nyuki import Nyuki
from nyuki.bus import reporting
from nyuki.websocket import WebsocketResource

from .api.factory import (
    ApiFactoryRegex, ApiFactoryRegexes, ApiFactoryLookup, ApiFactoryLookups,
    ApiFactoryLookupCSV
)
from .api.templates import (
    ApiTasks, ApiTemplates, ApiTemplate, ApiTemplateVersion, ApiTemplateDraft
)
from .api.workflows import (
    ApiWorkflow, ApiWorkflows, ApiWorkflowsHistory, ApiWorkflowHistory,
    ApiWorkflowTriggers, ApiWorkflowTrigger, serialize_wflow_exec
)

from .storage import MongoStorage
from .tasks import *
from .tasks.utils import runtime


log = logging.getLogger(__name__)


class BadRequestError(Exception):
    pass


def sanitize_workflow_exec(obj):
    """
    Replace any object value by 'internal data' string to store in Mongo.
    """
    types = [dict, list, tuple, str, int, float, bool, type(None), datetime]
    if type(obj) not in types:
        obj = 'Internal server data: {}'.format(type(obj))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            obj[key] = sanitize_workflow_exec(value)
    elif isinstance(obj, list):
        for item in obj:
            item = sanitize_workflow_exec(item)
    return obj


class WorkflowInstance(WebsocketResource):

    """
    Holds a workflow pair of template/instance.
    Allows retrieving a workflow exec state at any moment.
    """

    ALLOWED_EXEC_KEYS = ['requester', 'track']

    def __init__(self, template, instance, **kwargs):
        super().__init__(
            '/exec/{}'.format(instance.uid),
            serializer=serialize_wflow_exec
        )
        self._template = template
        self._instance = instance
        self._exec = {
            key: kwargs[key]
            for key in kwargs
            if key in self.ALLOWED_EXEC_KEYS
        }

    @property
    def template(self):
        return self._template

    @property
    def instance(self):
        return self._instance

    @property
    def exec(self):
        return self._exec

    async def ready(self, client):
        """
        Overrides WebsocketResource's method.
        """
        return self.report()

    def report(self):
        """
        Merge a workflow exec instance report and its template.
        """
        template = self._template.copy()
        inst = self._instance.report()
        tasks = {task['id']: task for task in template['tasks']}

        inst['exec'].update(self._exec)
        for task in inst['tasks']:
            # Stored template contains more info than tukio's (title...),
            # so we add it to the report.
            tasks[task['id']] = {**tasks[task['id']], **task}

        return {
            **self._template,
            'exec': inst['exec'],
            'tasks': [task for task in tasks.values()]
        }


class GlobalExec(WebsocketResource):

    def __init__(self, nyuki, *args, **kwargs):
        super().__init__(*args, serializer=serialize_wflow_exec, **kwargs)
        self.nyuki = nyuki

    async def ready(self, client):
        running = []
        for workflow in self.nyuki.running_workflows.values():
            report = workflow.report()
            del report['graph']
            del report['tasks']
            running.append(report)
        return running

    async def broadcast(self, data, *args, **kwargs):
        if 'template' in data:
            del data['template']['graph']
            del data['template']['tasks']
        return await super().broadcast(data, *args, **kwargs)


class WorkflowNyuki(Nyuki):

    """
    Generic workflow nyuki allowing data storage and manipulation
    of tukio's workflows.
    https://github.com/optiflows/tukio
    """

    CONF_SCHEMA = {
        'type': 'object',
        'required': ['mongo'],
        'properties': {
            'mongo': {
                'type': 'object',
                'required': ['host'],
                'properties': {
                    'host': {'type': 'string', 'minLength': 1},
                    'database': {'type': 'string', 'minLength': 1}
                }
            },
            'topics': {
                'type': 'array',
                'items': {'type': 'string', 'minLength': 1}
            }
        }
    }
    HTTP_RESOURCES = Nyuki.HTTP_RESOURCES + [
        ApiTasks,  # /v1/workflows/tasks
        ApiTemplates,  # /v1/workflows/templates
        ApiTemplate,  # /v1/workflows/templates/{uid}
        ApiTemplateDraft,  # /v1/workflows/templates/{uid}/draft
        ApiTemplateVersion,  # /v1/workflows/templates/{uid}/{version}
        ApiWorkflows,  # /v1/workflows
        ApiWorkflow,  # /v1/workflows/{uid}
        ApiWorkflowsHistory,  # /v1/workflows/history
        ApiWorkflowHistory,  # /v1/workflows/history/{uid}
        ApiFactoryRegexes,  # /v1/workflows/regexes
        ApiFactoryRegex,  # /v1/workflows/regexes/{uid}
        ApiFactoryLookups,  # /v1/workflows/lookups
        ApiFactoryLookup,  # /v1/workflows/lookups/{uid}
        ApiFactoryLookupCSV,  # /v1/workflows/lookups/{uid}/csv
        ApiWorkflowTriggers,  # /v1/workflows/triggers
        ApiWorkflowTrigger,  # /v1/workflows/triggers/{tid}
    ]

    DEFAULT_POLICY = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.register_schema(self.CONF_SCHEMA)
        self.migrate_config()
        self.engine = None
        self.storage = None
        self.memory = None

        self.AVAILABLE_TASKS = {}
        for name, value in TaskRegistry.all().items():
            self.AVAILABLE_TASKS[name] = getattr(value[0], 'SCHEMA', {})

        # Stores workflow instances with their template data
        self.running_workflows = {}
        self.global_exec = GlobalExec(self, '/exec')

        runtime.bus = self.bus
        runtime.config = self.config
        runtime.workflows = self.running_workflows

    @property
    def mongo_config(self):
        return self.config['mongo']

    @property
    def topics(self):
        return self.config.get('topics', [])

    async def setup(self):
        self.engine = Engine(loop=self.loop)
        asyncio.ensure_future(self.reload_from_storage())
        for topic in self.topics:
            asyncio.ensure_future(self.bus.subscribe(
                topic, self.workflow_event
            ))
        # Enable workflow exec follow-up
        get_broker().register(self.report_workflow, topic=EXEC_TOPIC)
        await self.setup_memory()

    async def reload(self):
        asyncio.ensure_future(self.reload_from_storage())
        await self.setup_memory()

    async def teardown(self):
        self.global_exec.end()
        if self.engine:
            await self.engine.stop()
        if self.memory:
            await self.memory.close()

    def new_workflow(self, template, instance, **kwargs):
        """
        Keep in memory a workflow template/instance pair.
        """
        wflow = WorkflowInstance(template, instance, **kwargs)
        self.running_workflows[instance.uid] = wflow
        if self.memory:
            asyncio.ensure_future(self.share_report(wflow.report(), False))
        return wflow

    async def report_workflow(self, event):
        """
        Send all worklfow updates to the clients.
        """
        source = event.source.as_dict()
        exec_id = source['workflow_exec_id']
        wflow = self.running_workflows[exec_id]
        source['workflow_exec_requester'] = wflow.exec.get('requester')

        report = None
        if self.memory:
            report = wflow.report()
            asyncio.ensure_future(self.share_report(report))

        payload = {
            'type': event.data['type'],
            'data': event.data.get('content') or {},
            'source': source,
            'timestamp': datetime.utcnow().isoformat()
        }

        # Workflow begins, also send the full template.
        if event.data['type'] == WorkflowExecState.begin.value:
            payload['template'] = dict(wflow.template)
            asyncio.ensure_future(self.global_exec.broadcast(payload))
        # Workflow ended, clear it from memory
        elif event.data['type'] in [
            WorkflowExecState.end.value,
            WorkflowExecState.error.value
        ]:
            asyncio.ensure_future(self.global_exec.broadcast(payload))
            # Sanitize objects to store the finished workflow instance
            asyncio.ensure_future(self.storage.instances.insert(
                sanitize_workflow_exec(report or wflow.report())
            ))
            wflow.end()
            del self.running_workflows[exec_id]
            if self.memory:
                self.memory.delete(self.memory_key('instances', exec_id))
        # Workflow suspended/resumed
        elif event.data['type'] in [
            WorkflowExecState.suspend.value,
            WorkflowExecState.resume.value
        ]:
            asyncio.ensure_future(self.global_exec.broadcast(payload))

        await wflow.broadcast(payload)

    async def workflow_event(self, efrom, data):
        """
        New bus event received, trigger workflows if needed.
        """
        templates = {}
        # Retrieve full workflow templates
        wf_templates = self.engine.selector.select(efrom)
        for wftmpl in wf_templates:
            template = await self.storage.templates.get(
                wftmpl.uid,
                draft=False,
                with_metadata=True
            )
            templates[wftmpl.uid] = template[0]
        # Trigger workflows
        instances = await self.engine.data_received(data, efrom)
        for instance in instances:
            self.new_workflow(templates[instance.template.uid], instance)

    async def reload_from_storage(self):
        """
        Check mongo, retrieve and load all templates
        """
        self.storage = MongoStorage(**self.mongo_config)

        templates = await self.storage.templates.get_all(
            full=True,
            latest=True,
            with_metadata=False
        )

        for template in templates:
            try:
                await self.engine.load(WorkflowTemplate.from_dict(template))
            except Exception as exc:
                # Means a bad workflow is in database, report it
                reporting.exception(exc)

    async def setup_memory(self):
        config = self.config.get('redis')

        if not config:
            if self.memory:
                self.memory.close()
                await self.memory.wait_closed()
            self.memory = None
            return

        if not self.config.get('service'):
            log.error("Can't enable 'redis': 'service' config key is missing")
            return

        self.memory = await create_redis(
            (config.get('host', 'localhost'), config.get('port', 6379)),
            db=config.get('database', 'nyuki'),
            ssl=config.get('ssl'),
            encoding='utf-8',
            loop=self.loop
        )
        log.info("Connection made with Redis")

    def memory_key(self, resource, entry):
        return '{service}.workflows.{resource}.{entry}'.format(
            service=self.config['service'],
            resource=resource,
            entry=entry
        )

    async def share_report(self, report, replace=True):
        """
        Store an instance report into shared memory.
        A simple 'set' is used againts a 'hset' (hash storage), even though the
        'hset' seems more appropriate because a field in a hash can't have TTL.
        """
        uid = report['exec']['id']
        response = await self.memory.set(
            key=self.memory_key('instances', uid),
            value=pickle.dumps(report),
            expire=86400,
            exist=None if replace else False
        )

        if not response:
            log.error("Can't share workflow id %s context in memory", uid)
