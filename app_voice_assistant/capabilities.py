"""Shared base action contract; domain-specific validation stays in contracts.py."""
import json
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name('capabilities.json').read_text())
ACTIONS = CATALOG['actions']
TARGET_PERMISSIONS = CATALOG['target_permissions']
ARGUMENT_SCHEMAS = CATALOG.get('argument_schemas', {})
GUIDES = CATALOG.get('guides', {})
WORKFLOW_ACTIONS = {
    action_id: action for action_id, action in ACTIONS.items()
    if action.get('workflow_steps')
}


def validate_catalog() -> None:
    if int(CATALOG.get('version', 0) or 0) < 4:
        raise RuntimeError('app_voice_catalog_v4_required')
    if not isinstance(ACTIONS, dict) or not isinstance(GUIDES, dict):
        raise RuntimeError('app_voice_catalog_shape_invalid')
    if not isinstance(ARGUMENT_SCHEMAS, dict) or set(ARGUMENT_SCHEMAS) != set(ACTIONS):
        raise RuntimeError('app_voice_argument_schema_coverage_invalid')
    referenced: set[str] = set()
    for guide_id, guide in GUIDES.items():
        if not isinstance(guide_id, str) or not isinstance(guide, dict):
            raise RuntimeError('app_voice_guide_invalid')
        if not str(guide.get('title') or '').strip() or not str(guide.get('summary') or '').strip():
            raise RuntimeError(f'app_voice_guide_copy_missing:{guide_id}')
        action_ids = guide.get('action_ids')
        if not isinstance(action_ids, list) or not action_ids:
            raise RuntimeError(f'app_voice_guide_actions_missing:{guide_id}')
        unknown = {str(item) for item in action_ids} - set(ACTIONS)
        if unknown:
            raise RuntimeError(f'app_voice_guide_action_unknown:{guide_id}')
        destination = guide.get('destination')
        if destination is not None and (
            not isinstance(destination, str) or not destination.strip()
        ):
            raise RuntimeError(f'app_voice_guide_destination_invalid:{guide_id}')
        referenced.update(str(item) for item in action_ids)
    executable = set(ACTIONS) - {'assistant.reply'}
    if not executable <= referenced:
        raise RuntimeError('app_voice_guide_coverage_incomplete')
    for action_id, action in ACTIONS.items():
        if not isinstance(action, dict):
            raise RuntimeError(f'app_voice_action_invalid:{action_id}')
        if not str(action.get('title') or '').strip():
            raise RuntimeError(f'app_voice_action_title_missing:{action_id}')
        if not str(action.get('domain') or '').strip():
            raise RuntimeError(f'app_voice_action_domain_missing:{action_id}')
        if not isinstance(action.get('aliases'), list):
            raise RuntimeError(f'app_voice_action_aliases_invalid:{action_id}')
        if action.get('risk') not in {'read', 'write', 'control'}:
            raise RuntimeError(f'app_voice_action_risk_invalid:{action_id}')
        if action.get('execution_kind') not in {
            'inline_server', 'inline_device',
            'background_server', 'background_device',
        }:
            raise RuntimeError(f'app_voice_action_execution_invalid:{action_id}')
        if not isinstance(action.get('cancellable'), bool):
            raise RuntimeError(f'app_voice_action_cancellable_invalid:{action_id}')
        if not isinstance(action.get('discovery_permissions', []), list):
            raise RuntimeError(f'app_voice_action_discovery_permissions_invalid:{action_id}')
        if not isinstance(action.get('parameters'), list) or not isinstance(action.get('target_kinds'), list):
            raise RuntimeError(f'app_voice_action_contract_invalid:{action_id}')
        workflow_steps = action.get('workflow_steps')
        if workflow_steps is not None:
            if not isinstance(workflow_steps, list) or not 2 <= len(workflow_steps) <= 8:
                raise RuntimeError(f'app_voice_workflow_steps_invalid:{action_id}')
            step_keys: set[str] = set()
            for step in workflow_steps:
                if not isinstance(step, dict):
                    raise RuntimeError(f'app_voice_workflow_step_invalid:{action_id}')
                step_key = str(step.get('key') or '')
                nested_action = str(step.get('action_id') or '')
                if not step_key or step_key in step_keys:
                    raise RuntimeError(f'app_voice_workflow_step_key_invalid:{action_id}')
                if nested_action not in ACTIONS or nested_action == action_id:
                    raise RuntimeError(f'app_voice_workflow_action_invalid:{action_id}')
                step_keys.add(step_key)
            for step in workflow_steps:
                if set(str(item) for item in step.get('depends_on') or []) - step_keys:
                    raise RuntimeError(f'app_voice_workflow_dependency_invalid:{action_id}')
                argument_map = step.get('argument_map', {})
                if not isinstance(argument_map, dict):
                    raise RuntimeError(f'app_voice_workflow_argument_map_invalid:{action_id}')
                if any(
                    str(source).split('.', 1)[0] not in action['parameters']
                    for source in argument_map.values()
                ):
                    raise RuntimeError(f'app_voice_workflow_argument_unknown:{action_id}')
        routine_templates = action.get('routine_templates')
        if routine_templates is not None:
            if action_id != 'routine.save' or not isinstance(routine_templates, dict):
                raise RuntimeError(f'app_voice_routine_templates_invalid:{action_id}')
            schema_templates = set(
                ARGUMENT_SCHEMAS[action_id]['properties']['template_id'].get('enum') or []
            )
            if set(routine_templates) != schema_templates or any(
                not isinstance(value, dict)
                or not str(value.get('title') or '').strip()
                or not isinstance(value.get('required_arguments'), list)
                for value in routine_templates.values()
            ):
                raise RuntimeError(f'app_voice_routine_templates_invalid:{action_id}')
        schema = ARGUMENT_SCHEMAS[action_id]
        if (
            not isinstance(schema, dict)
            or schema.get('type') != 'object'
            or schema.get('additionalProperties') is not False
            or not isinstance(schema.get('properties'), dict)
            or set(schema['properties']) != set(action['parameters'])
        ):
            raise RuntimeError(f'app_voice_argument_schema_invalid:{action_id}')


validate_catalog()


def available_actions(requested, permissions, *, targets=False):
    def allowed(name):
        action = ACTIONS[name]
        required = action.get('target_permissions') if targets else None
        required = required if required is not None else [action['permission']]
        return all(permission is None or permissions.get(permission) is True for permission in required)
    return [name for name in requested if isinstance(name, str) and name in ACTIONS and allowed(name)][:40]
