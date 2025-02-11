import logging
from gladier.exc import FlowModifierException
from gladier.utils.name_generation import get_funcx_flow_state_name

log = logging.getLogger(__name__)
funcx_modifiers = {'endpoint', 'payload', 'tasks'}
state_modifiers = {'InputPath', 'ResultPath', 'WaitTime'}


class FlowModifiers:
    supported_modifiers = state_modifiers.union(funcx_modifiers)
    funcx_modifiers = funcx_modifiers
    state_modifiers = state_modifiers

    def __init__(self, tools, modifiers, cls=None):
        self.cls = cls
        self.tools = tools
        self.functions = [func for tool in tools for func in tool.funcx_functions]
        self.function_names = [f.__name__ for f in self.functions]
        self.state_names = [get_funcx_flow_state_name(f) for f in self.functions]
        self.modifiers = modifiers
        self.check_modifiers()

    def get_function(self, function_identifier):
        if function_identifier in self.function_names:
            return self.functions[self.function_names.index(function_identifier)]
        if function_identifier in self.functions:
            return function_identifier

    def get_flow_state_name(self, function_identifier):
        func = self.get_function(function_identifier)
        return get_funcx_flow_state_name(func)

    def get_state_result_path(self, state_name):
        return f'$.{state_name}.details.results'

    def check_modifiers(self):
        log.debug(f'Checking modifiers: {self.modifiers}')
        if not isinstance(self.modifiers, dict):
            raise FlowModifierException(f'{self.cls}: Flow Modifiers must be a dict')

        # Check if modifiers were set correctly
        for name, mods in self.modifiers.items():
            if not self.get_function(name):
                raise FlowModifierException(f'Class {self.cls} included modifier which does not '
                                            f'exist: {name}. Allowed modifiers include '
                                            f'{", ".join(self.function_names)}')

            for mod_name, mod_value in mods.items():
                if mod_name not in self.supported_modifiers:
                    raise FlowModifierException(f'Class {self.cls}: Unsupported modifier '
                                                f'"{mod_name}". The only supported modifiers are: '
                                                f'{self.supported_modifiers}')

    def apply_modifiers(self, flow):
        for name, mods in self.modifiers.items():
            state_name = self.get_flow_state_name(name)
            flow['States'][state_name] = self.apply_modifier(flow['States'][state_name], mods)
        return flow

    def apply_modifier(self, flow_state, state_modifiers):

        for modifier_name, value in state_modifiers.items():
            log.debug(f'Applying modifier "{modifier_name}", value "{value}"')
            # If this is for a funcx task
            if modifier_name in self.funcx_modifiers:
                if modifier_name == 'tasks':
                    flow_state['Parameters'] = self.generic_set_modifier(
                        flow_state['Parameters'], 'tasks', value)
                else:
                    flow_state['Parameters']['tasks'] = [
                        self.generic_set_modifier(fx_task, modifier_name, value)
                        for fx_task in flow_state['Parameters']['tasks']
                    ]
            elif modifier_name in self.state_modifiers:
                self.generic_set_modifier(flow_state, modifier_name, value)
                if modifier_name == 'InputPath' and 'Parameters' in flow_state:
                    flow_state.pop('Parameters')
                    flow_state['InputPath'] = flow_state.pop('InputPath.$')
        return flow_state

    def generic_set_modifier(self, item, mod_name, mod_value):
        if not isinstance(mod_value, str):
            if mod_value in self.functions:
                sn = get_funcx_flow_state_name(mod_value)
                mod_value = self.get_state_result_path(sn)
        elif isinstance(mod_value, str) and not mod_value.startswith('$.'):
            if mod_value in self.function_names:
                sn = self.state_names[self.function_names.index(mod_value)]
                mod_value = self.get_state_result_path(sn)
            elif mod_value in self.state_names:
                mod_value = self.get_state_result_path(mod_value)
            else:
                mod_value = f'$.input.{mod_value}'

        # Remove duplicate keys
        for duplicate_mod_key in (mod_name, f'{mod_name}.$'):
            if duplicate_mod_key in item.keys():
                item.pop(duplicate_mod_key)

        if isinstance(mod_value, str) and mod_value.startswith('$.'):
            mod_name = f'{mod_name}.$'
        item[mod_name] = mod_value
        log.debug(f'Set modifier {mod_name} to {mod_value}')
        return item
