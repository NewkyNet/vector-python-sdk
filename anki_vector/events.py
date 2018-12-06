# Copyright (c) 2018 Anki, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License in the file LICENSE.txt or at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Event handler used to make functions subscribe to robot events.
"""

__all__ = ['EventHandler', 'Events']

import asyncio
from concurrent.futures import CancelledError
from enum import Enum
import threading
from typing import Callable
import uuid

from .connection import Connection
from . import util
from .messaging import protocol


class Events(Enum):
    """List of events available."""

    # Robot
    robot_state = "robot_state"                   #: Robot event containing changes to the robot's state.
    mirror_mode_disabled = "mirror_mode_disabled"  # : Robot event triggered when mirror mode (camera feed displayed on robot's face) is automatically disabled due to SDK no longer having control of the robot.
    vision_modes_auto_disabled = "vision_modes_auto_disabled"  # : Robot event triggered when all vision modes are automatically disabled due to the SDK no longer having control of the robot.

    # Objects
    object_available = "object_available"               #: After the ConnectCube process is started, all available light cubes in range will broadcast an availability message through the Robot.
    object_connection_state = "object_connection_state"  # : Robot event for an object with the ability to connect to the robot digitally changing its connection state.
    object_moved = "object_moved"                       #: Robot event triggered when an object starts moving.
    object_stopped_moving = "object_stopped_moving"     #: Robot event triggered when an object stops moving.
    object_up_axis_changed = "object_up_axis_changed"   #: Robot event triggered when an object's orientation changed.
    object_tapped = "object_tapped"                     #: Robot event triggered when an object is tapped.
    robot_observed_object = "robot_observed_object"     #: Robot event triggered when an object is observed by the robot.
    cube_connection_lost = "cube_connection_lost"       #: Robot event triggered when an object's subscribed connection has been lost.

    robot_observed_face = "robot_observed_face"                       #: Robot event for when a face is observed by the robot.
    robot_changed_observed_face_id = "robot_changed_observed_face_id"  # : Robot event for when a known face changes its id.

    wake_word = "wake_word"                             #: Robot event triggered when Vector hears "Hey Vector"

    # Audio
    audio_send_mode_changed = "audio_send_mode_changed"  #: Robot event containing changes to the robot's audio stream source data processing mode.

    # Generated by SDK
    object_observed = "object_observed"             #: Python event triggered in response to robot_observed_object with sdk metadata.
    object_appeared = "object_appeared"             #: Python event triggered when an object first receives robot_observed_object.
    object_disappeared = "object_disappeared"       #: Python event triggered when an object has not received a robot_observed_object for a specified time.
    object_finished_move = "object_finished_move"   #: Python event triggered in response to object_stopped_moving with duration data.
    nav_map_update = "nav_map_update"               #: Python event containing nav map data.


class _EventCallback:
    def __init__(self, callback, on_connection_thread: bool = False):
        self._callback = callback
        self._on_connection_thread = on_connection_thread

    @property
    def on_connection_thread(self):
        return self._on_connection_thread

    @property
    def callback(self):
        return self._callback

    def __eq__(self, other):
        other_cb = other
        if hasattr(other, "callback"):
            other_cb = other.callback
        return other_cb == self.callback

    def __hash__(self):
        return self._callback.__hash__()


class EventHandler:
    """Listen for Vector events."""

    def __init__(self):
        self.logger = util.get_class_logger(__name__, self)
        self._conn = None
        self._conn_id = None
        self.listening_for_events = False
        self.event_future = None
        self._thread: threading.Thread = None
        self._loop: asyncio.BaseEventLoop = None
        self.subscribers = {}
        self._done_signal: asyncio.Event = None

    def start(self, connection: Connection):
        """Start listening for events. Automatically called by the :class:`anki_vector.robot.Robot` class.

        :param connection: A reference to the connection from the SDK to the robot.
        :param loop: The loop to run the event task on.
        """
        self._conn = connection
        self.listening_for_events = True
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="Event Stream Handler Thread")
        self._thread.start()

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._done_signal = asyncio.Event(loop=self._loop)
        # create an event stream handler on the connection thread
        self.event_future = asyncio.run_coroutine_threadsafe(self._handle_event_stream(), self._conn.loop)
        self._loop.run_until_complete(self._done_signal.wait())

    def close(self):
        """Stop listening for events. Automatically called by the :class:`anki_vector.robot.Robot` class.
        """
        self.listening_for_events = False
        try:
            self.event_future.cancel()
            self.event_future.result()
        except CancelledError:
            pass
        self._loop.call_soon_threadsafe(self._done_signal.set)

    def _notify(self, event_callback, event_name, event_data):
        loop = self._loop
        thread = self._thread
        # For high priority events that shouldn't be blocked by user callbacks
        # they will run directly on the connection thread. This should typically
        # be used when setting robot properties from events.
        if event_callback.on_connection_thread:
            loop = self._conn.loop
            thread = self._conn.thread
        callback = event_callback.callback
        if threading.current_thread() is thread:
            if asyncio.iscoroutinefunction(callback):
                asyncio.ensure_future(callback(event_name, event_data), loop=loop)
            elif asyncio.iscoroutine(callback):
                asyncio.ensure_future(callback, loop=loop)
            else:
                loop.call_soon(callback, event_name, event_data)
        else:
            if asyncio.iscoroutinefunction(callback):
                asyncio.run_coroutine_threadsafe(callback(event_name, event_data), loop=loop)
            elif asyncio.iscoroutine(callback):
                asyncio.run_coroutine_threadsafe(callback, loop=loop)
            else:
                loop.call_soon_threadsafe(callback, event_name, event_data)

    async def dispatch_event_by_name(self, event_data, event_name: str = None):
        """Dispatches event to event listeners by name.

        .. testcode::

            import anki_vector

            def event_listener(name, msg):
                print(name) # will print 'my_event'
                print(msg) # will print 'my_event dispatched'

            with anki_vector.Robot() as robot:
                robot.events.subscribe_by_name(event_listener, event_name='my_event')
                robot.conn.run_coroutine(robot.events.dispatch_event_by_name('my_event dispatched', event_name='my_event'))

        :param event_data: Data to accompany the event.
        :param event_name: The name of the event that will result in func being called.
        """
        if not event_name:
            self.logger.error('Bad event_name in dispatch_event.')

        if event_name in self.subscribers.keys():
            subscribers = self.subscribers[event_name].copy()
            for callback in subscribers:
                self._notify(callback, event_name, event_data)

    async def dispatch_event(self, event_data, event_type: Events):
        """Dispatches event to event listeners."""
        if not event_type:
            self.logger.error('Bad event_type in dispatch_event.')

        event_name = event_type.value

        await self.dispatch_event_by_name(event_data, event_name)

    def _unpackage_event(self, enum_key: str, event):
        event_key = event.WhichOneof(enum_key)
        event_data = getattr(event, event_key)
        if getattr(event_data, 'WhichOneof'):
            # Object events are automatically unpackaged into their sub-event classes.
            try:
                return self._unpackage_event('object_event_type', event_data)
            except ValueError:
                pass
            except TypeError:
                pass

        return event_key, event_data

    async def _handle_event_stream(self):
        self._conn_id = bytes(uuid.uuid4().hex, "utf-8")
        try:
            req = protocol.EventRequest(connection_id=self._conn_id)
            async for evt in self._conn.grpc_interface.EventStream(req):
                if not self.listening_for_events:
                    break
                try:
                    unpackaged_event_key, unpackaged_event_data = self._unpackage_event('event_type', evt.event)
                    await self.dispatch_event_by_name(unpackaged_event_data, unpackaged_event_key)
                except TypeError:
                    self.logger.warning('Unknown Event type')
        except CancelledError:
            self.logger.debug('Event handler task was cancelled. This is expected during disconnection.')

    def subscribe_by_name(self, func: Callable, event_name: str = None, **kwargs):
        """Receive a method call when the specified event occurs.

        .. testcode::

            import anki_vector

            def event_listener(name, msg):
                print(name) # will print 'my_event'
                print(msg) # will print 'my_event dispatched'

            with anki_vector.Robot() as robot:
                robot.events.subscribe_by_name(event_listener, event_name='my_event')
                robot.conn.run_coroutine(robot.events.dispatch_event_by_name('my_event dispatched', event_name='my_event'))

        :param func: A method implemented in your code that will be called when the event is fired.
        :param event_name: The name of the event that will result in func being called.
        """
        if not event_name:
            self.logger.error('Bad event_name in subscribe.')
        on_connection_thread = kwargs["on_connection_thread"] if "on_connection_thread" in kwargs else False

        if event_name not in self.subscribers.keys():
            self.subscribers[event_name] = set()
        self.subscribers[event_name].add(_EventCallback(func, on_connection_thread))

    def subscribe(self, func: Callable, event_type: Events = None, **kwargs):
        """Receive a method call when the specified event occurs.

        .. testcode::

            import anki_vector
            from anki_vector.events import Events
            from anki_vector.util import degrees
            import functools
            import threading

            said_text = False
            evt = threading.Event()

            def on_robot_observed_face(robot, event_type, event):
                print("Vector sees a face")
                global said_text
                if not said_text:
                    said_text = True
                    robot.say_text("I see a face!")
                    evt.set()

            args = anki_vector.util.parse_command_args()
            with anki_vector.Robot(enable_face_detection=True) as robot:

                # If necessary, move Vector's Head and Lift to make it easy to see his face
                robot.behavior.set_head_angle(degrees(50.0))
                robot.behavior.set_lift_height(0.0)

                on_robot_observed_face = functools.partial(on_robot_observed_face, robot)
                robot.events.subscribe(on_robot_observed_face, Events.robot_observed_face)

                print("------ waiting for face events, press ctrl+c to exit early ------")

                try:
                    if not evt.wait(timeout=5):
                        print("------ Vector never saw your face! ------")
                except KeyboardInterrupt:
                    pass

            robot.events.unsubscribe(on_robot_observed_face, Events.robot_observed_face)

        :param func: A method implemented in your code that will be called when the event is fired.
        :param event_type: The enum type of the event that will result in func being called.
        """
        if not event_type:
            self.logger.error('Bad event_type in subscribe.')

        event_name = event_type.value

        self.subscribe_by_name(func, event_name, **kwargs)

    def unsubscribe_by_name(self, func: Callable, event_name: str = None):
        """Unregister a previously subscribed method from an event.

        .. testcode::

            import anki_vector

            def event_listener(name, msg):
                print(name) # will print 'my_event'
                print(msg) # will print 'my_event dispatched'

            with anki_vector.Robot() as robot:
                robot.events.subscribe_by_name(event_listener, event_name='my_event')
                robot.conn.run_coroutine(robot.events.dispatch_event_by_name('my_event dispatched', event_name='my_event'))

        :param func: The method you no longer wish to be called when an event fires.
        :param event_name: The name of the event for which you no longer want to receive a method call.
        """
        if not event_name:
            self.logger.error('Bad event_key in unsubscribe.')

        if event_name in self.subscribers.keys():
            event_subscribers = self.subscribers[event_name]
            if func in event_subscribers:
                event_subscribers.remove(func)
                if not event_subscribers:
                    self.subscribers.pop(event_name, None)
            else:
                self.logger.error(f"The function '{func.__name__}' is not subscribed to '{event_name}'")
        else:
            self.logger.error(f"Cannot unsubscribe from event_type '{event_name}'. "
                              "It has no subscribers.")

    def unsubscribe(self, func: Callable, event_type: Events = None):
        """Unregister a previously subscribed method from an event.

        .. testcode::

            import anki_vector
            from anki_vector.events import Events
            from anki_vector.util import degrees
            import functools
            import threading

            said_text = False
            evt = threading.Event()

            def on_robot_observed_face(robot, event_type, event):
                print("Vector sees a face")
                global said_text
                if not said_text:
                    said_text = True
                    robot.say_text("I see a face!")
                    evt.set()

            args = anki_vector.util.parse_command_args()
            with anki_vector.Robot(enable_face_detection=True) as robot:

                # If necessary, move Vector's Head and Lift to make it easy to see his face
                robot.behavior.set_head_angle(degrees(50.0))
                robot.behavior.set_lift_height(0.0)

                on_robot_observed_face = functools.partial(on_robot_observed_face, robot)
                robot.events.subscribe(on_robot_observed_face, Events.robot_observed_face)

                print("------ waiting for face events, press ctrl+c to exit early ------")

                try:
                    if not evt.wait(timeout=5):
                        print("------ Vector never saw your face! ------")
                except KeyboardInterrupt:
                    pass

            robot.events.unsubscribe(on_robot_observed_face, Events.robot_observed_face)

        :param func: The enum type of the event you no longer wish to be called when an event fires.
        :param event_type: The name of the event for which you no longer want to receive a method call.
        """
        if not event_type:
            self.logger.error('Bad event_type in unsubscribe.')

        event_name = event_type.value

        self.unsubscribe_by_name(func, event_name)
