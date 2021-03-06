# -*- coding: utf-8 -*-
"""
    celery.worker.bootsteps
    ~~~~~~~~~~~~~~~~~~~~~~~

    The boot-step components.

"""
from __future__ import absolute_import

import socket

from collections import defaultdict
from importlib import import_module
from threading import Event

from celery.datastructures import DependencyGraph
from celery.utils.imports import instantiate, qualname
from celery.utils.log import get_logger

try:
    from greenlet import GreenletExit
    IGNORE_ERRORS = (GreenletExit, )
except ImportError:  # pragma: no cover
    IGNORE_ERRORS = ()

#: Default socket timeout at shutdown.
SHUTDOWN_SOCKET_TIMEOUT = 5.0

#: States
RUN = 0x1
CLOSE = 0x2
TERMINATE = 0x3

logger = get_logger(__name__)


class Namespace(object):
    """A namespace containing components.

    Every component must belong to a namespace.

    When component classes are created they are added to the
    mapping of unclaimed components.  The components will be
    claimed when the namespace they belong to is created.

    :keyword name: Set the name of this namespace.
    :keyword app: Set the Celery app for this namespace.

    """
    name = None
    state = None
    started = 0

    _unclaimed = defaultdict(dict)

    def __init__(self, name=None, app=None, on_start=None,
            on_close=None, on_stopped=None):
        self.app = app
        self.name = name or self.name
        self.on_start = on_start
        self.on_close = on_close
        self.on_stopped = on_stopped
        self.services = []
        self.shutdown_complete = Event()

    def start(self, parent):
        self.state = RUN
        if self.on_start:
            self.on_start()
        for i, component in enumerate(parent.components):
            if component:
                logger.debug('Starting %s...', qualname(component))
                self.started = i + 1
                component.start(parent)
                logger.debug('%s OK!', qualname(component))

    def close(self, parent):
        if self.on_close:
            self.on_close()
        for component in parent.components:
            try:
                close = component.close
            except AttributeError:
                pass
            else:
                close(parent)

    def stop(self, parent, terminate=False):
        what = 'Terminating' if terminate else 'Stopping'
        socket_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(SHUTDOWN_SOCKET_TIMEOUT)  # Issue 975

        if self.state in (CLOSE, TERMINATE):
            return

        self.close(parent)

        if self.state != RUN or self.started != len(parent.components):
            # Not fully started, can safely exit.
            self.state = TERMINATE
            self.shutdown_complete.set()
            return
        self.state = CLOSE

        for component in reversed(parent.components):
            if component:
                logger.debug('%s %s...', what, qualname(component))
                (component.terminate if terminate else component.stop)(parent)

        if self.on_stopped:
            self.on_stopped()
        self.state = TERMINATE
        socket.setdefaulttimeout(socket_timeout)
        self.shutdown_complete.set()

    def join(self, timeout=None):
        try:
            # Will only get here if running green,
            # makes sure all greenthreads have exited.
            self.shutdown_complete.wait(timeout=timeout)
        except IGNORE_ERRORS:
            pass

    def modules(self):
        """Subclasses can override this to return a
        list of modules to import before components are claimed."""
        return []

    def load_modules(self):
        """Will load the component modules this namespace depends on."""
        for m in self.modules():
            self.import_module(m)

    def apply(self, parent, **kwargs):
        """Apply the components in this namespace to an object.

        This will apply the ``__init__`` and ``include`` methods
        of each components with the object as argument.

        For ``StartStopComponents`` the services created
        will also be added the the objects ``components`` attribute.

        """
        self._debug('Loading modules.')
        self.load_modules()
        self._debug('Claiming components.')
        self.components = self._claim()
        self._debug('Building boot step graph.')
        self.boot_steps = [self.bind_component(name, parent, **kwargs)
                                for name in self._finalize_boot_steps()]
        self._debug('New boot order: {%s}',
                ', '.join(c.name for c in self.boot_steps))

        for component in self.boot_steps:
            component.include(parent)
        return self

    def bind_component(self, name, parent, **kwargs):
        """Bind component to parent object and this namespace."""
        comp = self[name](parent, **kwargs)
        comp.namespace = self
        return comp

    def import_module(self, module):
        return import_module(module)

    def __getitem__(self, name):
        return self.components[name]

    def _find_last(self):
        for C in self.components.itervalues():
            if C.last:
                return C

    def _finalize_boot_steps(self):
        G = self.graph = DependencyGraph((C.name, C.requires)
                            for C in self.components.itervalues())
        last = self._find_last()
        if last:
            for obj in G:
                if obj != last.name:
                    G.add_edge(last.name, obj)
        return G.topsort()

    def _claim(self):
        return self._unclaimed[self.name]

    def _debug(self, msg, *args):
        return logger.debug('[%s] ' + msg,
                            *(self.name.capitalize(), ) + args)


class ComponentType(type):
    """Metaclass for components."""

    def __new__(cls, name, bases, attrs):
        abstract = attrs.pop('abstract', False)
        if not abstract:
            try:
                cname = attrs['name']
            except KeyError:
                raise NotImplementedError('Components must be named')
            namespace = attrs.get('namespace', None)
            if not namespace:
                attrs['namespace'], _, attrs['name'] = cname.partition('.')
        cls = super(ComponentType, cls).__new__(cls, name, bases, attrs)
        if not abstract:
            Namespace._unclaimed[cls.namespace][cls.name] = cls
        return cls


class Component(object):
    """A component.

    The :meth:`__init__` method is called when the component
    is bound to a parent object, and can as such be used
    to initialize attributes in the parent object at
    parent instantiation-time.

    """
    __metaclass__ = ComponentType

    #: The name of the component, or the namespace
    #: and the name of the component separated by dot.
    name = None

    #: List of component names this component depends on.
    #: Note that the dependencies must be in the same namespace.
    requires = ()

    #: can be used to specify the namespace,
    #: if the name does not include it.
    namespace = None

    #: if set the component will not be registered,
    #: but can be used as a component base class.
    abstract = True

    #: Optional obj created by the :meth:`create` method.
    #: This is used by StartStopComponents to keep the
    #: original service object.
    obj = None

    #: This flag is reserved for the workers Consumer,
    #: since it is required to always be started last.
    #: There can only be one object marked with lsat
    #: in every namespace.
    last = False

    #: This provides the default for :meth:`include_if`.
    enabled = True

    def __init__(self, parent, **kwargs):
        pass

    def create(self, parent):
        """Create the component."""
        pass

    def include_if(self, parent):
        """An optional predicate that decided whether this
        component should be created."""
        return self.enabled

    def instantiate(self, qualname, *args, **kwargs):
        return instantiate(qualname, *args, **kwargs)

    def include(self, parent):
        if self.include_if(parent):
            self.obj = self.create(parent)
            return True


class StartStopComponent(Component):
    abstract = True

    def start(self, parent):
        return self.obj.start()

    def stop(self, parent):
        return self.obj.stop()

    def close(self, parent):
        pass

    def terminate(self, parent):
        self.stop(parent)

    def include(self, parent):
        if super(StartStopComponent, self).include(parent):
            parent.components.append(self)
