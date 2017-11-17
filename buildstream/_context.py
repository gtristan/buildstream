#!/usr/bin/env python3
#
#  Copyright (C) 2016 Codethink Limited
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Tristan Van Berkom <tristan.vanberkom@codethink.co.uk>

import os
import hashlib
import pickle
from collections import deque, Mapping
from . import _site
from . import _yaml
from . import utils
from ._exceptions import LoadError, LoadErrorReason
from ._profile import Topics, profile_start, profile_end


# Context()
#
# The Context object holds all of the user preferences
# and context for a given invocation of BuildStream.
#
# This is a collection of data from configuration files and command
# line arguments and consists of information such as where to store
# logs and artifacts, where to perform builds and cache downloaded sources,
# verbosity levels and basically anything pertaining to the context
# in which BuildStream was invoked.
#
class Context():

    def __init__(self, cli_options):

        # Filename indicating which configuration file was used, or None for the defaults
        self.config_origin = None

        # Whether elements must be rebuilt when their dependencies have changed
        self.strict_build_plan = None

        # The directory where various sources are stored
        self.sourcedir = None

        # The directory where build sandboxes will be created
        self.builddir = None

        # The local binary artifact cache directory
        self.artifactdir = None

        # The URL from which to download prebuilt artifacts
        self.artifact_pull = None

        # The URL to upload built artifacts to
        self.artifact_push = None

        # The port number for pushing artifacts over ssh
        self.artifact_push_port = 22

        # The directory to store build logs
        self.logdir = None

        # The abbreviated cache key length to display in the UI
        self.log_key_length = 0

        # Whether debug mode is enabled
        self.log_debug = False

        # Whether verbose mode is enabled
        self.log_verbose = False

        # Maximum number of lines to print from build logs
        self.log_error_lines = 0

        # Maximum number of lines to print in the master log for a detailed message
        self.log_message_lines = 0

        # Format string for printing the pipeline at startup time
        self.log_element_format = None

        # Maximum number of fetch or refresh tasks
        self.sched_fetchers = 4

        # Maximum number of build tasks
        self.sched_builders = 4

        # Maximum number of push tasks
        self.sched_pushers = 4

        # Maximum number of retries for network tasks
        self.sched_network_retries = 2

        # What to do when a build fails in non interactive mode
        self.sched_error_action = 'continue'

        # Make sure the XDG vars are set in the environment before loading anything
        self._init_xdg()

        # Private variables
        self._cache_key = None
        self._message_handler = None
        self._message_depth = deque()
        self._platform = None
        self._project_overrides = {}
        self._cli_options = cli_options

    # load()
    #
    # Loads the configuration files
    #
    # Args:
    #    config (filename): The user specified configuration file, if any
    #
    # Raises:
    #   LoadError
    #
    # This will first load the BuildStream default configuration and then
    # override that configuration with the configuration file indicated
    # by *config*, if any was specified.
    #
    def load(self, config=None):
        profile_start(Topics.LOAD_CONTEXT, 'load')

        # If a specific config file is not specified, default to trying
        # a $XDG_CONFIG_HOME/buildstream.conf file
        #
        if not config:
            default_config = os.path.join(os.environ['XDG_CONFIG_HOME'],
                                          'buildstream.conf')
            if os.path.exists(default_config):
                config = default_config

        # Load default config
        #
        defaults = _yaml.load(_site.default_user_config)

        if config:
            self.config_origin = os.path.abspath(config)
            user_config = _yaml.load(config)
            _yaml.composite(defaults, user_config)

        _yaml.node_validate(defaults, [
            'sourcedir', 'builddir', 'artifactdir', 'logdir',
            'scheduler', 'artifacts', 'logging', 'projects',
        ])

        for dir in ['sourcedir', 'builddir', 'artifactdir', 'logdir']:
            # Allow the ~ tilde expansion and any environment variables in
            # path specification in the config files.
            #
            path = _yaml.node_get(defaults, str, dir)
            path = os.path.expanduser(path)
            path = os.path.expandvars(path)
            setattr(self, dir, path)

        # Load artifact share configuration
        artifacts = _yaml.node_get(defaults, Mapping, 'artifacts')
        _yaml.node_validate(artifacts, ['pull-url', 'push-url', 'push-port'])
        self.artifact_pull = _yaml.node_get(artifacts, str, 'pull-url', default_value='') or None
        self.artifact_push = _yaml.node_get(artifacts, str, 'push-url', default_value='') or None
        self.artifact_push_port = _yaml.node_get(artifacts, int, 'push-port', default_value=22)

        # Load logging config
        logging = _yaml.node_get(defaults, Mapping, 'logging')
        _yaml.node_validate(logging, [
            'key-length', 'verbose',
            'error-lines', 'message-lines',
            'debug', 'element-format'
        ])
        self.log_key_length = _yaml.node_get(logging, int, 'key-length')
        self.log_debug = _yaml.node_get(logging, bool, 'debug')
        self.log_verbose = _yaml.node_get(logging, bool, 'verbose')
        self.log_error_lines = _yaml.node_get(logging, int, 'error-lines')
        self.log_message_lines = _yaml.node_get(logging, int, 'message-lines')
        self.log_element_format = _yaml.node_get(logging, str, 'element-format')

        # Load scheduler config
        scheduler = _yaml.node_get(defaults, Mapping, 'scheduler')
        _yaml.node_validate(scheduler, [
            'on-error', 'fetchers', 'builders',
            'pushers', 'network-retries'
        ])
        self.sched_error_action = _yaml.node_get(scheduler, str, 'on-error')
        self.sched_fetchers = _yaml.node_get(scheduler, int, 'fetchers')
        self.sched_builders = _yaml.node_get(scheduler, int, 'builders')
        self.sched_pushers = _yaml.node_get(scheduler, int, 'pushers')
        self.sched_network_retries = _yaml.node_get(scheduler, int, 'network-retries')

        # Load per-projects overrides
        self._project_overrides = _yaml.node_get(defaults, Mapping, 'projects', default_value={})

        # Shallow validation of overrides, parts of buildstream which rely
        # on the overrides are expected to validate elsewhere.
        for project_name, overrides in _yaml.node_items(self._project_overrides):
            _yaml.node_validate(overrides, ['artifacts', 'options', 'strict'])

        profile_end(Topics.LOAD_CONTEXT, 'load')

        valid_actions = ['continue', 'quit']
        if self.sched_error_action not in valid_actions:
            provenance = _yaml.node_get_provenance(scheduler, 'on-error')
            raise LoadError(LoadErrorReason.INVALID_DATA,
                            "{}: on-error should be one of: {}".format(
                                provenance, ", ".join(valid_actions)))

    # _get_overrides():
    #
    # Fetch the override dictionary for the active project. This returns
    # a node loaded from YAML and as such, values loaded from the returned
    # node should be loaded using the _yaml.node_get() family of functions.
    #
    # Args:
    #    project_name (str): The project name
    #
    # Returns:
    #    (Mapping): The overrides dictionary for the specified project
    #
    def _get_overrides(self, project_name):
        return _yaml.node_get(self._project_overrides, Mapping, project_name, default_value={})

    # _get_strict():
    #
    # Fetch whether we are strict or not
    #
    # Args:
    #    project_name (str): The project name
    #
    # Returns:
    #    (bool): Whether or not to use strict build plan
    #
    def _get_strict(self, project_name):

        # If it was set by the CLI, it overrides any config
        if self.strict_build_plan is not None:
            return self.strict_build_plan

        overrides = self._get_overrides(project_name)
        return _yaml.node_get(overrides, bool, 'strict', default_value=True)

    # _get_cache_key():
    #
    # Returns the cache key, calculating it if necessary
    #
    # Returns:
    #    (str): A hex digest cache key for the Context
    #
    def _get_cache_key(self):
        if self._cache_key is None:

            # Anything that alters the build goes into the unique key
            self._cache_key = utils._generate_key({})

        return self._cache_key

    # _set_message_handler()
    #
    # Sets the handler for any status messages propagated through
    # the context.
    #
    # The message handler should have the same signature as
    # the _message() method
    def _set_message_handler(self, handler):
        self._message_handler = handler

    # _push_message_depth() / _pop_message_depth()
    #
    # For status messages, send the depth of timed
    # activities inside a given task through the message
    #
    def _push_message_depth(self, silent_nested):
        self._message_depth.appendleft(silent_nested)

    def _pop_message_depth(self):
        assert(self._message_depth)
        self._message_depth.popleft()

    def _silent_messages(self):
        for silent in self._message_depth:
            if silent:
                return True
        return False

    # _message():
    #
    # Proxies a message back to the caller, this is the central
    # point through which all messages pass.
    #
    # Args:
    #    message: A Message object
    #
    def _message(self, message):

        # Tag message only once
        if message.depth is None:
            message.depth = len(list(self._message_depth))

        # Send it off to the log handler (can be the frontend,
        # or it can be the child task which will log and propagate
        # to the frontend)
        assert(self._message_handler)

        self._message_handler(message, context=self)
        return

    # Force the resolved XDG variables into the environment,
    # this is so that they can be used directly to specify
    # preferred locations of things from user configuration
    # files.
    def _init_xdg(self):
        if not os.environ.get('XDG_CACHE_HOME'):
            os.environ['XDG_CACHE_HOME'] = os.path.expanduser('~/.cache')
        if not os.environ.get('XDG_CONFIG_HOME'):
            os.environ['XDG_CONFIG_HOME'] = os.path.expanduser('~/.config')
        if not os.environ.get('XDG_DATA_HOME'):
            os.environ['XDG_DATA_HOME'] = os.path.expanduser('~/.local/share')