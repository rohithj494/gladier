import os
import logging
import hashlib
import re
import json
from collections.abc import Iterable

import fair_research_login
import globus_sdk
import globus_automate_client
from funcx import FuncXClient
from funcx.serialize import FuncXSerializer

import gladier
import gladier.config
import gladier.utils.dynamic_imports
import gladier.utils.automate
import gladier.utils.name_generation
import gladier.utils.config_migrations
import gladier.exc
import gladier.version
log = logging.getLogger(__name__)

# The funcx scope is a class variable after 0.0.5. After we upgrade this can be
# removed
funcx_scope = getattr(FuncXClient, 'FUNCX_SCOPE',
                      'https://auth.globus.org/scopes/'
                      'facd7ccc-c5f4-42aa-916b-a0e270e2c2a9/all')

search_scope = 'urn:globus:auth:scope:search.api.globus.org:all'

GLADIER_SCOPES = [
    # FuncX requires search, auth, and its own funcx scope
    search_scope,
    'openid',
    funcx_scope,

    # Automate scopes
    *globus_automate_client.flows_client.ALL_FLOW_SCOPES,

    # Flow Scope is MISSING. FLow scopes correspond to the scope generated by automate when a
    # flow is deployed. That can only be determined by an instantiated Gladier Client. One
    # can retrieve the full list of scopes, including the flow scope from a previously deployed
    # flow with the following:
    # mc = MyGladierClient()
    # gladier_scopes_plus_flow_scope = mc.scopes
]


class GladierBaseClient(object):
    """The Gladier Client ties together commonly used funcx functions
    and basic flows with auto-registration tools to make complex tasks
    easy to automate.

    Default options are intended for CLI usage and maximum user convenience.

    :param authorizers: Provide live globus_sdk authorizers with a dict keyed by
                        scope.
    :type globus_sdk.AccessTokenAuthorizer: A globus authorizer
    :param auto_login: Automatically trigger login() calls when needed. Should not be used
                       with authorizers.
    :param auto_registration: Automatically register functions or flows if they are not
                              previously registered or obsolete.
    :raises gladier.exc.AuthException: if authorizers given are insufficient

    """
    secret_config_filename = os.path.expanduser("~/.gladier-secrets.cfg")
    config_filename = 'gladier.cfg'
    app_name = 'gladier_client'
    client_id = 'e6c75d97-532a-4c88-b031-8584a319fa3e'
    globus_group = None
    subscription_id = None

    def __init__(self, authorizers=None, auto_login=True, auto_registration=True):
        self.__flows_client = None
        self.__tools = None
        self.public_config = self._load_public_config()
        self.private_config = self._load_private_config()
        self.authorizers = authorizers or dict()
        self.auto_login = auto_login
        self.auto_registration = auto_registration

        private_cfg = self.get_cfg(private=True)
        private_cfg = gladier.utils.config_migrations.migrate_gladier(private_cfg)
        private_cfg.save()

        if os.path.exists(self.config_filename):
            pub_cfg = self.get_cfg(private=False)
            pub_cfg = gladier.utils.config_migrations.migrate_gladier(pub_cfg)
            pub_cfg.save()

        if self.authorizers and self.auto_login:
            log.warning('Authorizers provided when "auto_login=True", you probably want to set '
                        'auto_login=False if you are providing your own authorizers...')
        if self.authorizers and self.missing_authorizers:
            raise gladier.exc.AuthException(f'Missing Authorizers: {self.missing_authorizers}')
        try:
            if not self.authorizers:
                log.debug('No authorizers provided, loading from disk.')
                self.authorizers = self.get_native_client().get_authorizers_by_scope()
        except fair_research_login.exc.LoadError:
            log.debug('Load from disk failed, login will be required.')
        if self.auto_login and not self.is_logged_in():
            self.login()

    def _load_public_config(self):
        return gladier.config.GladierConfig(self.config_filename, self.section)

    def _load_private_config(self):
        return gladier.config.GladierSecretsConfig(self.secret_config_filename,
                                                   self.section, self.client_id)

    @staticmethod
    def get_gladier_defaults_cls(tool_ref):
        """
        Load a Gladier default class (gladier.GladierBaseTool) by import string. For
        Example: get_gladier_defaults_cls('gladier.tools.hello_world.HelloWorld')

        :param tool_ref: A tool ref can be a dotted import string or an actual GladierBaseTool
                         class.
        :return: gladier.GladierBaseTool
        """
        log.debug(f'Looking for Gladier tool: {tool_ref} ({type(tool_ref)})')
        if isinstance(tool_ref, str):
            default_cls = gladier.utils.dynamic_imports.import_string(tool_ref)
            default_inst = default_cls()
            if issubclass(type(default_inst), gladier.base.GladierBaseTool):
                return default_inst
            raise gladier.exc.ConfigException(f'{default_inst} is not of type '
                                              f'{gladier.base.GladierBaseTool}')
        elif isinstance(tool_ref, gladier.base.GladierBaseTool):
            return tool_ref
        else:
            cls_inst = tool_ref()
            if isinstance(cls_inst, gladier.base.GladierBaseTool):
                return cls_inst
            raise gladier.exc.ConfigException(
                f'"{tool_ref}" must be a {gladier.base.GladierBaseTool} or a dotted import '
                'string ')

    @property
    def version(self):
        return gladier.version.__version__

    @property
    def section(self):
        """Get the default section name for the config. The section name is derived
        from the name of the user's flow_definition class turned snake case."""
        name = self.__class__.__name__
        # https://stackoverflow.com/questions/1175208/elegant-python-function-to-convert-camelcase-to-snake-case
        snake_name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        snake_name = re.sub('([a-z0-9])([A-Z])', r'\1_\2', snake_name).lower()
        return snake_name

    @property
    def tools(self):
        """
        Load the current list of tools configured on this class

        :return: a list of subclassed instances of gladier.GladierBaseTool
        """
        if getattr(self, '__tools', None):
            return self.__tools

        if not getattr(self, 'gladier_tools', None) or not isinstance(self.gladier_tools, Iterable):
            raise gladier.exc.ConfigException(
                '"gladier_tools" must be a defined list of Gladier Tools. '
                'Ex: ["gladier.tools.hello_world.HelloWorld"]')
        self.__tools = [self.get_gladier_defaults_cls(gt) for gt in self.gladier_tools]
        return self.__tools

    def get_cfg(self, private=True):
        cfg = self.private_config if private is True else self.public_config
        section = self.section
        if section not in cfg.sections():
            log.debug(f'Adding new section {section} to {cfg.filename}')
            cfg[section] = {}
        return cfg

    def get_section(self, private=True):
        return self.get_cfg(private=private)[self.section]

    def get_native_client(self):
        """
        fair_research_login.NativeClient is used when ``authorizers`` are not provided to __init__.
        This enables local login to the Globus Automate Client, FuncX, and any other Globus
        Resource Server.

        :return: an instance of fair_research_login.NativeClient
        """
        if getattr(self, 'client_id', None) is None:
            raise gladier.exc.AuthException(
                'Gladier client must be instantiated with a '
                '"client_id" to use "login()!'
            )
        return fair_research_login.NativeClient(client_id=self.client_id,
                                                app_name=self.app_name,
                                                token_storage=self.get_cfg(private=True))

    @property
    def scopes(self):
        """
        The current list of scopes required by this class. This changes if there
        is a flow configured in the local Gladier config file, otherwise it will
        only consist of basic scopes for running the funcx client/flows client/etc

        :return: list of globus scopes required by this client
        """
        # Copy a list of the gladier scopes
        gladier_scopes = GLADIER_SCOPES.copy()
        # Add a flow scope if one exists
        flow_scope = self.get_section(private=True).get('flow_scope')
        if flow_scope:
            gladier_scopes.append(flow_scope)
        return gladier_scopes

    @property
    def missing_authorizers(self):
        """
        :return:  a list of Globus scopes for which there are no authorizers
        """
        return [scope for scope in self.scopes if scope not in self.authorizers.keys()]

    @property
    def flows_client(self):
        """
        :return: an authorized Gloubs Automate Client
        """
        if getattr(self, '__flows_client', None) is not None:
            return self.__flows_client
        automate_authorizer = self.authorizers[
            globus_automate_client.flows_client.MANAGE_FLOWS_SCOPE
        ]
        flow_scope = self.get_section(private=True).get('flow_scope')
        flow_authorizer = self.authorizers.get(flow_scope)

        def get_flow_authorizer(*args, **kwargs):
            return flow_authorizer

        self.__flows_client = globus_automate_client.FlowsClient.new_client(
            self.client_id, get_flow_authorizer, automate_authorizer,
        )
        return self.__flows_client

    @property
    def funcx_client(self):
        """
        :return: an authorized funcx client
        """
        if getattr(self, '__funcx_client', None) is not None:
            return self.__funcx_client
        self.__funcx_client = FuncXClient(
            fx_authorizer=self.authorizers[funcx_scope],
            search_authorizer=self.authorizers[search_scope],
            openid_authorizer=self.authorizers['openid'],
        )
        return self.__funcx_client

    def login(self, **login_kwargs):
        """Login to the Gladier client. This will ensure the user has the correct
        tokens configured but it DOES NOT guarantee they are in the correct group to
        run a flow. Can be run both locally and on a server.
        See help(fair_research_login.NativeClient.login) for a full list of kwargs.
        """
        nc = self.get_native_client()
        if self.is_logged_in() and login_kwargs.get('force') is not True:
            log.debug('Already logged in, skipping login.')
            return
        log.info('Initiating Native App Login...')
        log.debug(f'Requesting Scopes: {self.scopes}')
        login_kwargs['requested_scopes'] = login_kwargs.get('requested_scopes', self.scopes)
        login_kwargs['refresh_tokens'] = login_kwargs.get('refresh_tokens', True)
        nc.login(**login_kwargs)
        self.authorizers = nc.get_authorizers_by_scope()

    def logout(self):
        """Log out and revoke this client's tokens. This object will no longer
        be usable until a new login is called.
        """
        if not self.client_id:
            raise gladier.exc.AuthException('Gladier client must be instantiated with a '
                                            '"client_id" to use "login()!')
        log.info(f'Revoking the following scopes: {self.scopes}')
        self.get_native_client().logout()
        # Clear authorizers cache
        self.authorizers = dict()

    def is_logged_in(self):
        """
        Check if the client is logged in.

        :return: True, if there are no self.missing_authorizers. False otherwise.
        """
        return not bool(self.missing_authorizers)

    def get_flow_definition(self):
        """
        Get the flow definition attached to this class. If the flow definition is an import string,
        it will automatically load the import string and return the full flow.

        :return: A dict of the Automate Flow definition
        """
        if not getattr(self, 'flow_definition', None):
            raise gladier.exc.ConfigException(f'"flow_definition" was not set on '
                                              f'{self.__class__.__name__}')

        if isinstance(self.flow_definition, dict):
            return self.flow_definition
        elif isinstance(self.flow_definition, str):
            return self.get_gladier_defaults_cls(self.flow_definition).flow_definition
        raise gladier.exc.ConfigException('"flow_definition" must be a dict or an import string '
                                          'to a sub-class of type '
                                          '"gladier.GladierBaseTool"')



    def get_flow_checksum(self):
        """
        Get the SHA256 checksum of the current flow definition.

        :return: sha256 hex string of flow definition
        """
        return hashlib.sha256(json.dumps(self.get_flow_definition()).encode()).hexdigest()

    @staticmethod
    def get_globus_urn(uuid, id_type='group'):
        """Convenience method for appending the correct Globus URN prefix on a uuid."""
        URN_PREFIXES = {
            'group': 'urn:globus:groups:id:',
            'identity': 'urn:globus:auth:identity:'
        }
        if id_type not in URN_PREFIXES:
            raise gladier.exc.DevelopmentException('"id_type" must be one of '
                                                   f'{URN_PREFIXES.keys()}. Got: {id_type}')
        return f'{URN_PREFIXES[id_type]}{uuid}'

    def get_flow_permission(self, permission_type, identities=None):
        """
        This function is a generic shim that should work for most Gladier clients that
        want basic permissions that will work with a single Globus Group. This method can be
        overridden to change any of the automate defaults:

        permission_type for deploying flows:
            'visible_to', 'runnable_by', 'administered_by',

        permission_type for running flows:
            'manage_by', 'monitor_by'

        By default, always returns either None for using automate defaults, or setting every
        permission_type above to use the set client `globus_group`.
        """
        if identities is None and self.globus_group:
            identities = [self.get_globus_urn(self.globus_group)]
        permission_types = {
            'visible_to', 'runnable_by', 'administered_by', 'manage_by', 'monitor_by'
        }
        if permission_type not in permission_types:
            raise gladier.exc.DevelopmentException(f'permission_type must be one of '
                                                   f'{permission_types}')
        return identities

    @staticmethod
    def get_funcx_function_name(funcx_function):
        """
        Generate a function name given a funcx function. These function namse are used to refer
        to funcx functions within the config. There is no guarantee of uniqueness for function
        names.

        :return: human readable string identifier for a function (intended for a gladier.cfg file)
        """
        return f'{funcx_function.__name__}_funcx_id'

    @staticmethod
    def get_funcx_function_checksum(funcx_function):
        """
        Get the SHA256 checksum of a funcx function
        :return: sha256 hex string of a given funcx function
        """
        fxs = FuncXSerializer()
        serialized_func = fxs.serialize(funcx_function).encode()
        return hashlib.sha256(serialized_func).hexdigest()

    def get_funcx_function_ids(self):
        """Get all funcx function ids for this run, registering them if there are no ids
        stored in the local Gladier config file OR the stored function id checksums do
        not match the actual functions provided on each of the Gladier tools. If register
        is False, no changes to the config will be made and exceptions will be raised instead.

        :raises: gladier.exc.RegistrationException
        :raises: gladier.exc.FunctionObsolete
        :returns: a dict of function ids where keys are names and values are funcX function ids.
        """
        funcx_ids = dict()
        for tool in self.tools:
            log.debug(f'Checking functions for {tool}')
            funcx_funcs = getattr(tool, 'funcx_functions', [])
            if not funcx_funcs:
                log.warning(f'Tool {tool} did not define any funcX functions!')
            if not funcx_funcs and not isinstance(funcx_funcs, Iterable):
                raise gladier.exc.DeveloperException(
                    f'Attribute "funcx_functions" on {tool} needs to be an iterable! Found '
                    f'{type(funcx_funcs)}')

            cfgs = self.get_section(private=True)

            for func in funcx_funcs:
                fid_name = gladier.utils.name_generation.get_funcx_function_name(func)
                checksum = self.get_funcx_function_checksum(func)
                checksum_name = gladier.utils.name_generation.get_funcx_function_checksum_name(func)
                try:
                    if not cfgs.get(fid_name):
                        raise gladier.exc.RegistrationException(
                            f'Tool {tool} missing funcx registration for {fid_name}')
                    if not cfgs.get(checksum_name):
                        raise gladier.exc.RegistrationException(
                            f'Tool {tool} with function {fid_name} '
                            f'has a function id but no checksum!')
                    if not cfgs[checksum_name] == checksum:
                        raise gladier.exc.FunctionObsolete(
                            f'Tool {tool} with function {fid_name} '
                            f'has changed and needs to be re-registered.')
                    funcx_ids[fid_name] = cfgs[fid_name]
                except (gladier.exc.RegistrationException, gladier.exc.FunctionObsolete):
                    if self.auto_registration is True:
                        log.info(f'Registering function {fid_name}')
                        self.register_funcx_function(func)
                        funcx_ids[fid_name] = cfgs[fid_name]
                    else:
                        raise
        return funcx_ids

    def register_funcx_function(self, function):
        """Register the functions with funcx. Ids are saved in the local gladier.cfg"""

        fxid_name = gladier.utils.name_generation.get_funcx_function_name(function)
        fxck_name = gladier.utils.name_generation.get_funcx_function_checksum_name(function)
        cfg = self.get_cfg(private=True)
        cfg[self.section][fxid_name] = self.funcx_client.register_function(function,
                                                                           function.__doc__)
        cfg[self.section][fxck_name] = self.get_funcx_function_checksum(function)
        cfg.save()

    def get_flow_id(self):
        """Get the current flow id for the current Gladier flow definiton.
        If self.auto_register is True, it will automatically (re)register a flow if it
        has changed on disk, otherwise raising exceptions.

        :raises: gladier.exc.FlowObsolete
        :raises: gladier.exc.NoFlowRegistered
        """
        cfg_sec = self.get_section(private=True)
        flow_id, flow_scope = cfg_sec.get('flow_id'), cfg_sec.get('flow_scope')
        if not flow_id or not flow_scope:
            if self.auto_registration is False:
                raise gladier.exc.NoFlowRegistered(
                    f'No flow registered for {self.config_filename} under section {self.section}')
            flow_id = self.register_flow()
        elif cfg_sec.get('flow_checksum') != self.get_flow_checksum():
            if self.auto_registration is False:
                raise gladier.exc.FlowObsolete(
                    f'"flow_definition" on {self} has changed and needs to be re-registered.')
            self.register_flow()
        return cfg_sec['flow_id']

    def register_flow(self):
        """
        Register a flow with Globus Automate. If a flow has already been registered with automate,
        the flow will attempt to update the flow instead. If not, it will deploy a new flow.

        :raises: Automate exceptions on flow deployment.
        :return: an automate flow UUID
        """
        cfg = self.get_cfg()

        flow_id = cfg[self.section].get('flow_id')
        flow_definition = self.get_flow_definition()
        flow_permissions = {
            p_type: self.get_flow_permission(p_type)
            for p_type in ['runnable_by', 'visible_to', 'administered_by']
            if self.get_flow_permission(p_type)
        }
        log.debug(f'Flow permissions set to: {flow_permissions or "Flows defaults"}')
        flow_kwargs = flow_permissions
        # Input schema will be (probably is now) a required field. Returning {} is a temporary
        # fix to avoid an automate error, until we can properly generate an input schema.
        flow_kwargs['input_schema'] = {}
        if self.subscription_id:
            flow_kwargs['subscription_id'] = self.subscription_id
        if flow_id:
            try:
                log.info(f'Flow checksum failed, updating flow {flow_id}...')
                self.flows_client.update_flow(flow_id, flow_definition, **flow_kwargs)
                cfg[self.section]['flow_checksum'] = self.get_flow_checksum()
                cfg.save()
            except globus_sdk.exc.GlobusAPIError as gapie:
                if gapie.code == 'Not Found':
                    flow_id = None
                else:
                    raise
        if flow_id is None:
            log.info('No flow detected, deploying new flow...')
            title = f'{self.__class__.__name__} Flow'

            flow = self.flows_client.deploy_flow(flow_definition, title=title, **flow_kwargs).data
            cfg[self.section]['flow_id'] = flow['id']
            cfg[self.section]['flow_scope'] = flow['globus_auth_scope']
            cfg[self.section]['flow_checksum'] = self.get_flow_checksum()
            cfg.save()
            flow_id = cfg[self.section]['flow_id']

        return flow_id

    def get_input(self):
        """
        Get funcx function ids, funcx endpoints, and each tool's default input. Default
        input may not be enough to run the flow. For example if a tool does processing on a
        local filesystem, the file will always need to be provided by the user when calling
        run_flow().

        Defaults rely on GladierBaseTool.flow_input defined separately for each tool.

        :return: input for a flow wrapped in an 'input' dict. For example:
                 {'input': {'foo': 'bar'}}
        """
        flow_input = self.get_funcx_function_ids()
        for tool in self.tools:
            # conflicts = set(flow_input.keys()).intersection(set(tool.flow_input))
            # if conflicts:
            #     for prev_tools in tools:
            #         for r in prev_tools.flow_input:
            #             if set(flow_input.keys()).intersection(set(tool.flow_input)):
            #                 raise gladier.exc.ConfigException(
            #                   f'Conflict: Tools {tool} and {prev_tool} 'both define {r}')
            flow_input.update(tool.flow_input)
            # Iterate over both private and public input variables, and include any relevant ones
            # Note: Precedence starts and ends with: Public --> Private --> Default on Tool
            t_input, t_required = set(tool.flow_input), set(getattr(tool, 'required_input', []))
            input_keys = t_input.union(t_required)
            log.debug(f'{tool}: Looking for overrides for the following input keys: {input_keys}')
            for cfg in (self.get_cfg(private=True), self.get_cfg(private=False)):
                override_values = {k: cfg[self.section][k] for k in input_keys
                                   if cfg[self.section].get(k)}
                if override_values:
                    log.info(f'Updates from {cfg.filename}: {list(override_values.keys())}')
                    flow_input.update(override_values)
        return {'input': flow_input}

    def check_input(self, tool, flow_input):
        """
        Do basic checking on included input against requirements set by a tool. Raises an
        exception if the check does not 'pass'

        :param tool: The gladier.GladierBaseTool tool set in self.tools
        :param flow_input: Flow input intended to be passed to run_flow()
        :raises: gladier.exc.ConfigException
        """
        for req_input in tool.required_input:
            if req_input not in flow_input['input']:
                raise gladier.exc.ConfigException(
                    f'{tool} requires flow input value: "{req_input}"')

    def run_flow(self, flow_input=None, use_defaults=True, **flow_kwargs):
        """
        Start a Globus Automate flow. Flows and Functions must be registered prior or
        self.auto_registration must be True.

        If auto-registering a flow and self.auto_login is True, this may result in two logins.
        The first is for authorizing basic tooling, and the second is to autorize the newly
        registered automate flow.

        :param flow_input: A dict of input to be passed to the automate flow. self.check_input()
                           is called on each tool to ensure basic needs are met for each.
                           Input MUST be wrapped inside an 'input' dict,
                           for example {'input': {'foo': 'bar'}}.

        :param use_defaults: Use the result of self.get_input() to populate base input for the
                             flow. All conflicting input provided by flow_input overrides
                             values set in use_defaults.
        :param **flow_kwargs: Set several keyed arguments that include the label to be used in the automate app. 
                             If no label is passed the standard automate label is used. 
        :raise: gladier.exc.ConfigException by self.check_input()
        :raises: gladier.exc.FlowObsolete
        :raises: gladier.exc.NoFlowRegistered
        :raises: gladier.exc.RegistrationException
        :raises: gladier.exc.FunctionObsolete
        :raises: gladier.exc.AuthException
        :raises: Any globus_sdk.exc.BaseException
        """
        combine_flow_input = self.get_input() if use_defaults else dict()
        if flow_input is not None:
            if not flow_input.get('input') or len(flow_input.keys()) != 1:
                raise gladier.exc.ConfigException(
                    f'Malformed input to flow, all input must be nested under "input", got '
                    f'{flow_input.keys()}')
            combine_flow_input['input'].update(flow_input['input'])
        for tool in self.tools:
            self.check_input(tool, combine_flow_input)
        if not self.is_logged_in():
            raise gladier.exc.AuthException(f'Not Logged in, missing scopes '
                                            f'{self.missing_authorizers}')
        # When registering a flow for the first time, a special flow scope needs to be authorized
        # before the flow can begin. On first time runs, this requires an additional login.
        flow_id = self.get_flow_id()
        if not self.is_logged_in():
            log.info(f'Missing authorizers: {self.missing_authorizers}, need additional login '
                     f'to run flow.')
            if self.auto_login is True:
                self.login()
            else:
                raise gladier.exc.AuthException(
                    f'Need {self.missing_authorizers} to run flow!', self.missing_authorizers)

        flow_kwargs.update({
            p_type: self.get_flow_permission(p_type)
            for p_type in ['manage_by', 'monitor_by']
            if self.get_flow_permission(p_type)
        })
        log.debug(f'Flow run permissions set to: {flow_kwargs or "Flows defaults"}')
        cfg_sec = self.get_section(private=True)

        try:
            flow = self.flows_client.run_flow(flow_id, cfg_sec['flow_scope'],
                                              combine_flow_input, **flow_kwargs).data
        except globus_sdk.exc.GlobusAPIError as gapie:
            log.debug('Encountered error when running flow', exc_info=True)
            automate_error_message = json.loads(gapie.message)
            detail_message = automate_error_message['error']['detail']
            if 'unable to get tokens for scopes' in detail_message:
                if self.auto_login:
                    log.info('Initiating new login for dependent scope change')
                    self.login(requested_scopes=[cfg_sec['flow_scope']], force=True)
                    flow = self.flows_client.run_flow(flow_id, cfg_sec['flow_scope'],
                                                      combine_flow_input, **flow_kwargs).data
                else:
                    raise gladier.exc.AuthException('Scope change for flow, re-auth required',
                                                    missing_scopes=(cfg_sec['flow_scope'],))
            else:
                raise

        log.info(f'Started flow {flow_kwargs.get("label")} flow id "{cfg_sec["flow_id"]}" with action '
                 f'"{flow["action_id"]}"')

        if flow['status'] == 'FAILED':
            raise gladier.exc.ConfigException(f'Flow Failed: {flow["details"]["description"]}')
        return flow

    def get_status(self, action_id):
        """
        Get the current status of the automate flow. Attempts to do additional work on funcx
        functions to deserialize any exception output.

        :param action_id: The globus action UUID used for this flow. The Automate flow id is
                          always the flow_id configured for this tool.
        :raises: Globus Automate exceptions from self.flows_client.flow_action_status
        :returns: a Globus Automate status object (with varying state structures)
        """
        try:
            status = self.flows_client.flow_action_status(
                self.get_flow_id(), self.get_section(private=True)['flow_scope'], action_id
            ).data
        except KeyError:
            raise gladier.exc.ConfigException('No Flow defined, register a flow')

        try:
            return gladier.utils.automate.get_details(status)
        except (KeyError, AttributeError):
            return status

    @staticmethod
    def _default_progress_callback(response):
        if response['status'] == 'ACTIVE':
            print(f'[{response["status"]}]: {response["details"]["description"]}')

    def progress(self, action_id, callback=None):
        """
        Continuously call self.get_status() until the flow completes. Each status response is
        used as a parameter to the provided callback, by default will use the builtin callback
        to print the current state to stdout.

        :param action_id: The action id for a running flow. The flow is automatically pulled
                          based on the current tool's flow_definition.
        :param callback: The function to call with the result from self.get_status. Must take
                         a single parameter: mycallback(self.get_status())
        """
        callback = callback or self._default_progress_callback
        status = self.get_status(action_id)
        while status['status'] not in ['SUCCEEDED', 'FAILED']:
            status = self.get_status(action_id)
            callback(status)

    def get_details(self, action_id, state_name):
        """
        Attempt to extrapolate details from get_status() for a given state_name define in the flow
        definition. Note: This is usually only possible when a flow completes.

        :param action_id: The action_id for this flow. Flow id is automatically determined based
                          on the current tool being run.
        :param state_name: The state in the automate definition to fetch
        :returns: sub-dict of get_status() describing the :state_name:.
        """
        return gladier.utils.automate.get_details(self.get_status(action_id), state_name)
    
    def get_run_url(self, action_id):
        """
        Returns a globus automate webapp link for a given run on this flow. 
        :param action_id: The action_id for this flow. Flow id is automatically determined based
                          on the current tool being run.
        :returns: Globus webapp url of a particular run of this flow.
        """
        return 'https://app.globus.org/flows/%s/runs/%s' % (self.flow_id,action_id)

