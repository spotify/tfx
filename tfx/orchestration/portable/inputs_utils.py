# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Portable library for input artifacts resolution."""
from typing import Dict, Mapping, Sequence, Union

from tfx import types
from tfx.dsl.compiler import placeholder_utils
from tfx.orchestration import metadata
from tfx.orchestration.portable import data_types
from tfx.orchestration.portable.input_resolution import channel_resolver
from tfx.orchestration.portable.input_resolution import exceptions
from tfx.orchestration.portable.input_resolution import resolver_config_resolver
from tfx.proto.orchestration import pipeline_pb2
from tfx.utils import typing_utils


def _resolve_channels_dict(
    metadata_handler: metadata.Metadata,
    node_inputs: pipeline_pb2.NodeInputs) -> typing_utils.ArtifactMultiMap:
  """Resolves initial input dict from input channel definition."""
  result = {}
  for key, input_spec in node_inputs.inputs.items():
    if input_spec.channels:
      result[key] = channel_resolver.resolve_union_channels(
          metadata_handler.store, input_spec.channels)
  return result


def _is_sufficient(artifact_multimap: Mapping[str, Sequence[types.Artifact]],
                   node_inputs: pipeline_pb2.NodeInputs) -> bool:
  """Checks given artifact multimap has enough artifacts per channel."""
  return all(
      len(artifacts) >= node_inputs.inputs[key].min_count
      for key, artifacts in artifact_multimap.items()
      if key in node_inputs.inputs)


class Trigger(tuple, Sequence[typing_utils.ArtifactMultiMap]):
  """Input resolution result of list of dict."""

  def __new__(cls, resolved_inputs: Sequence[typing_utils.ArtifactMultiMap]):
    assert resolved_inputs, 'resolved inputs should be non-empty.'
    return super().__new__(cls, resolved_inputs)


class Skip(tuple, Sequence[typing_utils.ArtifactMultiMap]):
  """Input resolution result of empty list."""

  def __new__(cls):
    return super().__new__(cls)


def resolve_input_artifacts(
    *,
    pipeline_node: pipeline_pb2.PipelineNode,
    metadata_handler: metadata.Metadata,
) -> Union[Trigger, Skip]:
  """Resolve input artifacts according to a pipeline node IR definition.

  Input artifacts are resolved in the following steps:

  1. An initial input dict (Mapping[str, Sequence[Artifact]]) is fetched from
     the input channel definitions (in NodeInputs.inputs.channels).
  2. Optionally input resolution logic is performed if specified (in
     NodeInputs.resolver_config).
  3. Filters input map with enough number of artifacts as specified in
     NodeInputs.inputs.min_count.

  Args:
    pipeline_node: Current PipelineNode on which input resolution is running.
    metadata_handler: MetadataHandler instance for MLMD access.

  Raises:
    InputResolutionError: If input resolution went wrong.

  Returns:
    Trigger: a non-empty list of input dicts. All resolved input dicts should be
        executed.
    Skip: an empty list. Should effectively skip the current component
        execution.
  """
  node_inputs = pipeline_node.inputs
  initial_dict = _resolve_channels_dict(metadata_handler, node_inputs)
  try:
    resolved = resolver_config_resolver.resolve(
        metadata_handler.store,
        initial_dict,
        node_inputs.resolver_config)
  except exceptions.SkipSignal:
    return Skip()
  except exceptions.InputResolutionError:
    raise
  except Exception as e:
    raise exceptions.InputResolutionError(
        f'Error occurred during input resolution: {str(e)}.') from e

  if typing_utils.is_artifact_multimap(resolved):
    resolved = [resolved]
  if not typing_utils.is_list_of_artifact_multimap(resolved):
    raise exceptions.FailedPreconditionError(
        'Invalid input resolution result; expected Sequence[ArtifactMultiMap] '
        f'type but got {resolved}.')
  resolved = [d for d in resolved if _is_sufficient(d, node_inputs)]
  if not resolved:
    raise exceptions.FailedPreconditionError('No valid inputs.')
  return Trigger(resolved)


def resolve_parameters(
    node_parameters: pipeline_pb2.NodeParameters) -> Dict[str, types.Property]:
  """Resolves parameters given parameter spec.

  Args:
    node_parameters: The spec to get parameters.

  Returns:
    A Dict of parameters.

  Raises:
    RuntimeError: When there is at least one parameter still in runtime
      parameter form.
  """
  result = {}
  for key, value in node_parameters.parameters.items():
    if not value.HasField('field_value'):
      raise RuntimeError('Parameter value not ready for %s' % key)
    result[key] = getattr(value.field_value,
                          value.field_value.WhichOneof('value'))

  return result


def resolve_parameters_with_schema(
    node_parameters: pipeline_pb2.NodeParameters
) -> Dict[str, pipeline_pb2.Value]:
  """Resolves parameter schemas given parameter spec.

  Args:
    node_parameters: The spec to get parameters.

  Returns:
    A Dict of parameters with schema.

  Raises:
    RuntimeError: When there is no field_value available.
  """
  result = {}
  for key, value in node_parameters.parameters.items():
    if value.HasField('placeholder'):
      continue
    if not value.HasField('field_value'):
      raise RuntimeError('Parameter value not ready for %s' % key)
    result[key] = value

  return result


def resolve_dynamic_parameters(
    node_parameters: pipeline_pb2.NodeParameters,
    input_artifacts: typing_utils.ArtifactMultiMap
) -> Dict[str, types.ExecPropertyTypes]:
  """Resolves dynamic execution properties given the input artifacts.

  Args:
    node_parameters: The spec to get parameters.
    input_artifacts: The input dict.

  Returns:
    A Dict of resolved dynamic parameters.

  Raises:
    RuntimeError: When cannot find input for dynamic exec prop.
  """
  result = {}
  converted_input_artifacts = {}
  for key, value in input_artifacts.items():
    converted_input_artifacts[key] = list(value)
  for key, value in node_parameters.parameters.items():
    if value.HasField('placeholder'):
      execution_info = data_types.ExecutionInfo(
          input_dict=converted_input_artifacts,
          output_dict={},
          exec_properties={})
      context = placeholder_utils.ResolutionContext(
          exec_info=execution_info)
      try:
        resolved_val = placeholder_utils.resolve_placeholder_expression(
            value.placeholder, context)
        if resolved_val is None:
          raise exceptions.InputResolutionError(
              f'Cannot find input for dynamic exec prop: {key}')
        result[key] = resolved_val
      except Exception as e:
        raise exceptions.InputResolutionError(
            f'Failed to resolve dynamic exec properties: {key}'
        ) from e

  return result
