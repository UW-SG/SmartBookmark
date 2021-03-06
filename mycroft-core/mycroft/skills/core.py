# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import imp
import operator
import sys
import time
from functools import wraps
from inspect import getargspec

import abc
import re
from adapt.intent import Intent, IntentBuilder
from os import listdir
from os.path import join, abspath, dirname, splitext, basename, exists

from mycroft.client.enclosure.api import EnclosureAPI
from mycroft.configuration import Configuration
from mycroft.dialog import DialogLoader
from mycroft.filesystem import FileSystemAccess
from mycroft.messagebus.message import Message
from mycroft.skills.settings import SkillSettings
from mycroft.util.log import LOG


MainModule = '__init__'


def load_vocab_from_file(path, vocab_type, emitter):
    """
        Load mycroft vocabulary from file. and send it on the message bus for
        the intent handler.

        Args:
            path:       path to vocabulary file (*.voc)
            vocab_type: keyword name
            emitter:    emitter to access the message bus
    """
    if path.endswith('.voc'):
        with open(path, 'r') as voc_file:
            for line in voc_file.readlines():
                parts = line.strip().split("|")
                entity = parts[0]

                emitter.emit(Message("register_vocab", {
                    'start': entity, 'end': vocab_type
                }))
                for alias in parts[1:]:
                    emitter.emit(Message("register_vocab", {
                        'start': alias, 'end': vocab_type, 'alias_of': entity
                    }))


def load_regex_from_file(path, emitter):
    """
        Load regex from file and send it on the message bus for
        the intent handler.

        Args:
            path:       path to vocabulary file (*.voc)
            emitter:    emitter to access the message bus
    """
    if path.endswith('.rx'):
        with open(path, 'r') as reg_file:
            for line in reg_file.readlines():
                re.compile(line.strip())
                emitter.emit(
                    Message("register_vocab", {'regex': line.strip()}))


def load_vocabulary(basedir, emitter):
    for vocab_type in listdir(basedir):
        if vocab_type.endswith(".voc"):
            load_vocab_from_file(
                join(basedir, vocab_type), splitext(vocab_type)[0], emitter)


def load_regex(basedir, emitter):
    for regex_type in listdir(basedir):
        if regex_type.endswith(".rx"):
            load_regex_from_file(
                join(basedir, regex_type), emitter)


def open_intent_envelope(message):
    """ Convert dictionary received over messagebus to Intent. """
    intent_dict = message.data
    return Intent(intent_dict.get('name'),
                  intent_dict.get('requires'),
                  intent_dict.get('at_least_one'),
                  intent_dict.get('optional'))


def load_skill(skill_descriptor, emitter, skill_id, BLACKLISTED_SKILLS=None):
    """
        load skill from skill descriptor.

        Args:
            skill_descriptor: descriptor of skill to load
            emitter:          messagebus emitter
            skill_id:         id number for skill
    """
    BLACKLISTED_SKILLS = BLACKLISTED_SKILLS or []
    try:
        LOG.info("ATTEMPTING TO LOAD SKILL: " + skill_descriptor["name"] +
                 " with ID " + str(skill_id))
        if skill_descriptor['name'] in BLACKLISTED_SKILLS:
            LOG.info("SKILL IS BLACKLISTED " + skill_descriptor["name"])
            return None
        skill_module = imp.load_module(
            skill_descriptor["name"] + MainModule, *skill_descriptor["info"])
        if (hasattr(skill_module, 'create_skill') and
                callable(skill_module.create_skill)):
            # v2 skills framework
            skill = skill_module.create_skill()
            skill.bind(emitter)
            skill.skill_id = skill_id
            skill.load_data_files(dirname(skill_descriptor['info'][1]))
            # Set up intent handlers
            skill.initialize()
            skill._register_decorated()
            LOG.info("Loaded " + skill_descriptor["name"])
            return skill
        else:
            LOG.warning(
                "Module %s does not appear to be skill" % (
                    skill_descriptor["name"]))
    except:
        LOG.error(
            "Failed to load skill: " + skill_descriptor["name"],
            exc_info=True)
    return None


def create_skill_descriptor(skill_folder):
    info = imp.find_module(MainModule, [skill_folder])
    return {"name": basename(skill_folder), "info": info}


def get_handler_name(handler):
    """
        Return name (including class if available) of handler
        function.

        Args:
            handler (function): Function to be named

        Returns: handler name as string
    """
    name = ''
    if '__self__' in dir(handler) and 'name' in dir(handler.__self__):
        name += handler.__self__.name + '.'
    name += handler.__name__
    return name


# Lists used when adding skill handlers using decorators
_intent_list = []
_intent_file_list = []


def intent_handler(intent_parser):
    """ Decorator for adding a method as an intent handler. """

    def real_decorator(func):
        @wraps(func)
        def handler_method(*args, **kwargs):
            return func(*args, **kwargs)

        _intent_list.append((intent_parser, func))
        return handler_method

    return real_decorator


def intent_file_handler(intent_file):
    """ Decorator for adding a method as an intent file handler. """

    def real_decorator(func):
        @wraps(func)
        def handler_method(*args, **kwargs):
            return func(*args, **kwargs)

        _intent_file_list.append((intent_file, func))
        return handler_method

    return real_decorator


class MycroftSkill(object):
    """
    Abstract base class which provides common behaviour and parameters to all
    Skills implementation.
    """

    def __init__(self, name=None, emitter=None):
        self.name = name or self.__class__.__name__
        # Get directory of skill
        self._dir = dirname(abspath(sys.modules[self.__module__].__file__))

        self.bind(emitter)
        self.config_core = Configuration.get()
        self.config = self.config_core.get(self.name)
        self.dialog_renderer = None
        self.vocab_dir = None
        self.file_system = FileSystemAccess(join('skills', self.name))
        self.registered_intents = []
        self.log = LOG.create_logger(self.name)
        self.reload_skill = True
        self.events = []
        self.skill_id = 0

    @property
    def location(self):
        """ Get the JSON data struction holding location information. """
        # TODO: Allow Enclosure to override this for devices that
        # contain a GPS.
        return self.config_core.get('location')

    @property
    def location_pretty(self):
        """ Get a more 'human' version of the location as a string. """
        loc = self.location
        if type(loc) is dict and loc["city"]:
            return loc["city"]["name"]
        return None

    @property
    def location_timezone(self):
        """ Get the timezone code, such as 'America/Los_Angeles' """
        loc = self.location
        if type(loc) is dict and loc["timezone"]:
            return loc["timezone"]["code"]
        return None

    @property
    def lang(self):
        return self.config_core.get('lang')

    @property
    def settings(self):
        """ Load settings if not already loaded. """
        try:
            return self._settings
        except:
            self._settings = SkillSettings(self._dir, self.name)
            return self._settings

    def bind(self, emitter):
        """ Register emitter with skill. """
        if emitter:
            self.emitter = emitter
            self.enclosure = EnclosureAPI(emitter, self.name)
            self.__register_stop()

    def __register_stop(self):
        self.stop_time = time.time()
        self.stop_threshold = self.config_core.get("skills").get(
            'stop_threshold')
        self.add_event('mycroft.stop', self.__handle_stop, False)

    def detach(self):
        for (name, intent) in self.registered_intents:
            name = str(self.skill_id) + ':' + name
            self.emitter.emit(Message("detach_intent", {"intent_name": name}))

    def initialize(self):
        """
        Initialization function to be implemented by all Skills.

        Usually used to create intents rules and register them.
        """
        LOG.debug("No initialize function implemented")

    def converse(self, utterances, lang="en-us"):
        """
            Handle conversation. This method can be used to override the normal
            intent handler after the skill has been invoked once.

            To enable this override thise converse method and return True to
            indicate that the utterance has been handled.

            Args:
                utterances: The utterances from the user
                lang:       language the utterance is in

            Returns:    True if an utterance was handled, otherwise False
        """
        return False

    def make_active(self):
        """
            Bump skill to active_skill list in intent_service
            this enables converse method to be called even without skill being
            used in last 5 minutes
        """
        self.emitter.emit(Message('active_skill_request',
                                  {"skill_id": self.skill_id}))

    def _register_decorated(self):
        """
        Register all intent handlers that has been decorated with an intent.
        """
        global _intent_list, _intent_file_list
        for intent_parser, handler in _intent_list:
            self.register_intent(intent_parser, handler, need_self=True)
        for intent_file, handler in _intent_file_list:
            self.register_intent_file(intent_file, handler, need_self=True)
        _intent_list = []
        _intent_file_list = []

    def add_event(self, name, handler, need_self=False):
        """
            Create event handler for executing intent

            Args:
                name:       IntentParser name
                handler:    method to call
                need_self:     optional parameter, when called from a decorated
                               intent handler the function will need the self
                               variable passed as well.
        """

        def wrapper(message):
            try:
                # Indicate that the skill handler is starting
                name = get_handler_name(handler)
                self.emitter.emit(Message("mycroft.skill.handler.start",
                                          data={'handler': name}))
                if need_self:
                    # When registring from decorator self is required
                    if len(getargspec(handler).args) == 2:
                        handler(self, message)
                    elif len(getargspec(handler).args) == 1:
                        handler(self)
                    elif len(getargspec(handler).args) == 0:
                        # Zero may indicate multiple decorators, trying the
                        # usual call signatures
                        try:
                            handler(self, message)
                        except TypeError:
                            handler(self)
                    else:
                        raise TypeError
                else:
                    if len(getargspec(handler).args) == 2:
                        handler(message)
                    elif len(getargspec(handler).args) == 1:
                        handler()
                    else:
                        raise TypeError
                self.settings.store()  # Store settings if they've changed
            except Exception as e:
                # TODO: Localize
                self.speak(
                    "An error occurred while processing a request in " +
                    self.name)
                LOG.error(
                    "An error occurred while processing a request in " +
                    self.name, exc_info=True)
                # indicate completion with exception
                self.emitter.emit(Message('mycroft.skill.handler.complete',
                                          data={'handler': name,
                                                'exception': e.message}))
            # Indicate that the skill handler has completed
            self.emitter.emit(Message('mycroft.skill.handler.complete',
                                      data={'handler': name}))

        if handler:
            self.emitter.on(name, wrapper)
            self.events.append((name, wrapper))

    def register_intent(self, intent_parser, handler, need_self=False):
        """
            Register an Intent with the intent service.

            Args:
                intent_parser: Intent or IntentBuilder object to parse
                               utterance for the handler.
                handler:       function to register with intent
                need_self:     optional parameter, when called from a decorated
                               intent handler the function will need the self
                               variable passed as well.
        """
        if type(intent_parser) == IntentBuilder:
            intent_parser = intent_parser.build()
        elif type(intent_parser) != Intent:
            raise ValueError('intent_parser is not an Intent')

        name = intent_parser.name
        intent_parser.name = str(self.skill_id) + ':' + intent_parser.name
        self.emitter.emit(Message("register_intent", intent_parser.__dict__))
        self.registered_intents.append((name, intent_parser))
        self.add_event(intent_parser.name, handler, need_self)

    def register_intent_file(self, intent_file, handler, need_self=False):
        """
            Register an Intent file with the intent service.
            For example:

            === food.order.intent ===
            Order some {food}.
            Order some {food} from {place}.
            I'm hungry.
            Grab some {food} from {place}.

            Optionally, you can also use <register_entity_file>
            to specify some examples of {food} and {place}

            In addition, instead of writing out multiple variations
            of the same sentence you can write:

            === food.order.intent ===
            (Order | Grab) some {food} (from {place} | ).
            I'm hungry.

            Args:
                intent_file: name of file that contains example queries
                             that should activate the intent
                handler:     function to register with intent
                need_self:   use for decorator. See <register_intent>
        """
        name = str(self.skill_id) + ':' + intent_file
        self.emitter.emit(Message("padatious:register_intent", {
            "file_name": join(self.vocab_dir, intent_file),
            "name": name
        }))
        self.add_event(name, handler, need_self)

    def register_entity_file(self, entity_file):
        """
            Register an Entity file with the intent service.
            And Entity file lists the exact values that an entity can hold.
            For example:

            === ask.day.intent ===
            Is it {weekday}?

            === weekday.entity ===
            Monday
            Tuesday
            ...

            Args:
                entity_file: name of file that contains examples
                             of an entity. Must end with .entity
        """
        if '.entity' not in entity_file:
            raise ValueError('Invalid entity filename: ' + entity_file)
        name = str(self.skill_id) + ':' + entity_file.replace('.entity', '')
        self.emitter.emit(Message("padatious:register_entity", {
            "file_name": join(self.vocab_dir, entity_file),
            "name": name
        }))

    def disable_intent(self, intent_name):
        """Disable a registered intent"""
        LOG.debug('Disabling intent ' + intent_name)
        name = str(self.skill_id) + ':' + intent_name
        self.emitter.emit(Message("detach_intent", {"intent_name": name}))

    def enable_intent(self, intent_name):
        """Reenable a registered intent"""
        for (name, intent) in self.registered_intents:
            if name == intent_name:
                self.registered_intents.remove((name, intent))
                intent.name = name
                self.register_intent(intent, None)
                LOG.debug('Enabling intent ' + intent_name)
                break
            else:
                LOG.error('Could not enable ' + intent_name +
                          ', it hasn\'t been registered.')

    def set_context(self, context, word=''):
        """
            Add context to intent service

            Args:
                context:    Keyword
                word:       word connected to keyword
        """
        if not isinstance(context, basestring):
            raise ValueError('context should be a string')
        if not isinstance(word, basestring):
            raise ValueError('word should be a string')
        self.emitter.emit(Message('add_context',
                                  {'context': context, 'word': word}))

    def remove_context(self, context):
        """
            remove_context removes a keyword from from the context manager.
        """
        if not isinstance(context, basestring):
            raise ValueError('context should be a string')
        self.emitter.emit(Message('remove_context', {'context': context}))

    def register_vocabulary(self, entity, entity_type):
        """ Register a word to an keyword

            Args:
                entity:         word to register
                entity_type:    Intent handler entity to tie the word to
        """
        self.emitter.emit(Message('register_vocab', {
            'start': entity, 'end': entity_type
        }))

    def register_regex(self, regex_str):
        re.compile(regex_str)  # validate regex
        self.emitter.emit(Message('register_vocab', {'regex': regex_str}))

    def speak(self, utterance, expect_response=False):
        """
            Speak a sentence.

            Args:
                utterance:          sentence mycroft should speak
                expect_response:    set to True if Mycroft should expect a
                                    response from the user and start listening
                                    for response.
        """
        # registers the skill as being active
        self.enclosure.register(self.name)
        data = {'utterance': utterance,
                'expect_response': expect_response}
        self.emitter.emit(Message("speak", data))

    def speak_dialog(self, key, data=None, expect_response=False):
        """
            Speak sentance based of dialog file.

            Args
                key: dialog file key (filname without extension)
                data: information to populate sentence with
                expect_response:    set to True if Mycroft should expect a
                                    response from the user and start listening
                                    for response.
        """
        data = data or {}
        self.speak(self.dialog_renderer.render(key, data), expect_response)

    def init_dialog(self, root_directory):
        dialog_dir = join(root_directory, 'dialog', self.lang)
        if exists(dialog_dir):
            self.dialog_renderer = DialogLoader().load(dialog_dir)
        else:
            LOG.debug('No dialog loaded, ' + dialog_dir + ' does not exist')

    def load_data_files(self, root_directory):
        self.init_dialog(root_directory)
        self.load_vocab_files(join(root_directory, 'vocab', self.lang))
        regex_path = join(root_directory, 'regex', self.lang)
        if exists(regex_path):
            self.load_regex_files(regex_path)

    def load_vocab_files(self, vocab_dir):
        self.vocab_dir = vocab_dir
        if exists(vocab_dir):
            load_vocabulary(vocab_dir, self.emitter)
        else:
            LOG.debug('No vocab loaded, ' + vocab_dir + ' does not exist')

    def load_regex_files(self, regex_dir):
        load_regex(regex_dir, self.emitter)

    def __handle_stop(self, event):
        """
            Handler for the "mycroft.stop" signal. Runs the user defined
            `stop()` method.
        """
        self.stop_time = time.time()
        try:
            self.stop()
        except:
            LOG.error("Failed to stop skill: {}".format(self.name),
                      exc_info=True)

    @abc.abstractmethod
    def stop(self):
        pass

    def is_stop(self):
        passed_time = time.time() - self.stop_time
        return passed_time < self.stop_threshold

    def shutdown(self):
        """
        This method is intended to be called during the skill
        process termination. The skill implementation must
        shutdown all processes and operations in execution.
        """
        # Store settings
        self.settings.store()
        self.settings.is_alive = False
        # removing events
        for e, f in self.events:
            self.emitter.remove(e, f)
        self.events = None  # Remove reference to wrappers

        self.emitter.emit(
            Message("detach_skill", {"skill_id": str(self.skill_id) + ":"}))
        try:
            self.stop()
        except:
            LOG.error("Failed to stop skill: {}".format(self.name),
                      exc_info=True)

    def _unique_name(self, name):
        """
            Return a name unique to this skill using the format
            [skill_id]:[name].

            Args:
                name:   Name to use internally

            returns: name unique to this skill
        """
        return str(self.skill_id) + ':' + name

    def _schedule_event(self, handler, when, data=None, name=None,
                        repeat=None):
        """
            Underlying method for schedle_event and schedule_repeating_event.
            Takes scheduling information and sends it of on the message bus.
        """
        data = data or {}
        if not name:
            name = self.name + handler.__name__
        name = self._unique_name(name)

        self.add_event(name, handler, False)
        event_data = {}
        event_data['time'] = time.mktime(when.timetuple())
        event_data['event'] = name
        event_data['repeat'] = repeat
        event_data['data'] = data
        self.emitter.emit(Message('mycroft.scheduler.schedule_event',
                                  data=event_data))

    def schedule_event(self, handler, when, data=None, name=None):
        """
            Schedule a single event.

            Args:
                handler:               method to be called
                when (datetime):       when the handler should be called
                data (dict, optional): data to send when the handler is called
                name (str, optional):  friendly name parameter
        """
        data = data or {}
        self._schedule_event(handler, when, data, name)

    def schedule_repeating_event(self, handler, when, frequency,
                                 data=None, name=None):
        """
            Schedule a repeating event.

            Args:
                handler:                method to be called
                when (datetime):        time for calling the handler
                frequency (float/int):  time in seconds between calls
                data (dict, optional):  data to send along to the handler
                name (str, optional):   friendly name parameter
        """
        data = data or {}
        self._schedule_event(handler, when, data, name, frequency)

    def update_event(self, name, data=None):
        """
            Change data of event.

            Args:
                name (str):   Name of event
        """
        data = data or {}
        data = {
            'event': self._unique_name(name),
            'data': data
        }
        self.emitter.emit(Message('mycroft.schedule.update_event', data=data))

    def cancel_event(self, name):
        """
            Cancel a pending event. The event will no longer be scheduled
            to be executed

            Args:
                name (str):   Name of event
        """
        data = {'event': self._unique_name(name)}
        self.emitter.emit(Message('mycroft.scheduler.remove_event', data=data))


class FallbackSkill(MycroftSkill):
    """
        FallbackSkill is used to declare a fallback to be called when
        no skill is matching an intent. The fallbackSkill implements a
        number of fallback handlers to be called in an order determined
        by their priority.
    """
    fallback_handlers = {}

    def __init__(self, name=None, emitter=None):
        MycroftSkill.__init__(self, name, emitter)

        #  list of fallback handlers registered by this instance
        self.instance_fallback_handlers = []

    @classmethod
    def make_intent_failure_handler(cls, ws):
        """Goes through all fallback handlers until one returns True"""

        def handler(message):
            # indicate fallback handling start
            ws.emit(Message("mycroft.skill.handler.start",
                            data={'handler': "fallback"}))
            for _, handler in sorted(cls.fallback_handlers.items(),
                                     key=operator.itemgetter(0)):
                try:
                    if handler(message):
                        #  indicate completion
                        ws.emit(Message(
                            'mycroft.skill.handler.complete',
                            data={'handler': "fallback",
                                  "fallback_handler": get_handler_name(
                                      handler)}))
                        return
                except Exception as e:
                    LOG.info('Exception in fallback: ' + str(e))
            ws.emit(Message('complete_intent_failure'))
            LOG.warning('No fallback could handle intent.')
            #  indicate completion with exception
            ws.emit(Message('mycroft.skill.handler.complete',
                            data={'handler': "fallback",
                                  'exception':
                                      "No fallback could handle intent."}))

        return handler

    @classmethod
    def _register_fallback(cls, handler, priority):
        """
        Register a function to be called as a general info fallback
        Fallback should receive message and return
        a boolean (True if succeeded or False if failed)

        Lower priority gets run first
        0 for high priority 100 for low priority
        """
        while priority in cls.fallback_handlers:
            priority += 1

        cls.fallback_handlers[priority] = handler

    def register_fallback(self, handler, priority):
        """
            register a fallback with the list of fallback handlers
            and with the list of handlers registered by this instance
        """
        self.instance_fallback_handlers.append(handler)
        self._register_fallback(handler, priority)

    @classmethod
    def remove_fallback(cls, handler_to_del):
        """
            Remove a fallback handler

            Args:
                handler_to_del: reference to handler
        """
        for priority, handler in cls.fallback_handlers.items():
            if handler == handler_to_del:
                del cls.fallback_handlers[priority]
                return
        LOG.warning('Could not remove fallback!')

    def remove_instance_handlers(self):
        """
            Remove all fallback handlers registered by the fallback skill.
        """
        while len(self.instance_fallback_handlers):
            handler = self.instance_fallback_handlers.pop()
            self.remove_fallback(handler)

    def shutdown(self):
        """
            Remove all registered handlers and perform skill shutdown.
        """
        self.remove_instance_handlers()
        super(FallbackSkill, self).shutdown()
