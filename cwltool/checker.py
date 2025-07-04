"""Static checking of CWL workflow connectivity."""

from collections.abc import Iterator, MutableMapping, MutableSequence, Sized
from typing import Any, Literal, NamedTuple, Optional, Union, cast

from schema_salad.exceptions import ValidationException
from schema_salad.sourceline import SourceLine, bullets, strip_dup_lineno
from schema_salad.utils import json_dumps

from .errors import WorkflowException
from .loghandler import _logger
from .process import shortname
from .utils import CWLObjectType, CWLOutputType, SinkType, aslist


def _get_type(tp: Any) -> Any:
    if isinstance(tp, MutableMapping):
        if tp.get("type") not in ("array", "record", "enum"):
            return tp["type"]
    return tp


def check_types(
    srctype: SinkType,
    sinktype: SinkType,
    linkMerge: Optional[str],
    valueFrom: Optional[str],
) -> Union[Literal["pass"], Literal["warning"], Literal["exception"]]:
    """
    Check if the source and sink types are correct.

    :raises WorkflowException: If there is an unrecognized linkMerge type
    """
    if valueFrom is not None:
        return "pass"
    if linkMerge is None:
        if can_assign_src_to_sink(srctype, sinktype, strict=True):
            return "pass"
        if can_assign_src_to_sink(srctype, sinktype, strict=False):
            return "warning"
        return "exception"
    if linkMerge == "merge_nested":
        return check_types(
            {"items": _get_type(srctype), "type": "array"},
            _get_type(sinktype),
            None,
            None,
        )
    if linkMerge == "merge_flattened":
        return check_types(merge_flatten_type(_get_type(srctype)), _get_type(sinktype), None, None)
    raise WorkflowException(f"Unrecognized linkMerge enum {linkMerge!r}")


def merge_flatten_type(src: SinkType) -> CWLOutputType:
    """Return the merge flattened type of the source type."""
    if isinstance(src, MutableSequence):
        return [merge_flatten_type(t) for t in src]
    if isinstance(src, MutableMapping) and src.get("type") == "array":
        return src
    return {"items": src, "type": "array"}


def can_assign_src_to_sink(src: SinkType, sink: Optional[SinkType], strict: bool = False) -> bool:
    """
    Check for identical type specifications, ignoring extra keys like inputBinding.

    In non-strict comparison, at least one source type must match one sink type,
    except for 'null'.
    In strict comparison, all source types must match at least one sink type.

    :param src: admissible source types
    :param sink: admissible sink types
    """
    if src == "Any" or sink == "Any":
        return True
    if isinstance(src, MutableMapping) and isinstance(sink, MutableMapping):
        if sink.get("not_connected") and strict:
            return False
        if src["type"] == "array" and sink["type"] == "array":
            return can_assign_src_to_sink(
                cast(MutableSequence[CWLOutputType], src["items"]),
                cast(MutableSequence[CWLOutputType], sink["items"]),
                strict,
            )
        if src["type"] == "record" and sink["type"] == "record":
            return _compare_records(src, sink, strict)
        if src["type"] == "File" and sink["type"] == "File":
            for sinksf in cast(list[CWLObjectType], sink.get("secondaryFiles", [])):
                if not [
                    1
                    for srcsf in cast(list[CWLObjectType], src.get("secondaryFiles", []))
                    if sinksf == srcsf
                ]:
                    if strict:
                        return False
            return True
        return can_assign_src_to_sink(src["type"], sink["type"], strict)
    if isinstance(src, MutableSequence):
        if strict:
            for this_src in src:
                if not can_assign_src_to_sink(this_src, sink):
                    return False
            return True
        for this_src in src:
            if this_src != "null" and can_assign_src_to_sink(this_src, sink):
                return True
        return False
    if isinstance(sink, MutableSequence):
        for this_sink in sink:
            if can_assign_src_to_sink(src, this_sink):
                return True
        return False
    return bool(src == sink)


def _compare_records(src: CWLObjectType, sink: CWLObjectType, strict: bool = False) -> bool:
    """
    Compare two records, ensuring they have compatible fields.

    This handles normalizing record names, which will be relative to workflow
    step, so that they can be compared.

    :return: True if the records have compatible fields, False otherwise.
    """

    def _rec_fields(rec: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        out = {}
        for field in rec["fields"]:
            name = shortname(field["name"])
            out[name] = field["type"]
        return out

    srcfields = _rec_fields(src)
    sinkfields = _rec_fields(sink)
    for key in sinkfields.keys():
        if (
            not can_assign_src_to_sink(
                srcfields.get(key, "null"), sinkfields.get(key, "null"), strict
            )
            and sinkfields.get(key) is not None
        ):
            _logger.info(
                "Record comparison failure for %s and %s\n"
                "Did not match fields for %s: %s and %s",
                src["name"],
                sink["name"],
                key,
                srcfields.get(key),
                sinkfields.get(key),
            )
            return False
    return True


def missing_subset(fullset: list[Any], subset: list[Any]) -> list[Any]:
    """Calculate the items missing from the fullset given the subset."""
    missing = []
    for i in subset:
        if i not in fullset:
            missing.append(i)
    return missing


def static_checker(
    workflow_inputs: list[CWLObjectType],
    workflow_outputs: MutableSequence[CWLObjectType],
    step_inputs: MutableSequence[CWLObjectType],
    step_outputs: list[CWLObjectType],
    param_to_step: dict[str, CWLObjectType],
) -> None:
    """
    Check if all source and sink types of a workflow are compatible before run time.

    :raises ValidationException: If any incompatibilities are detected.
    """
    # source parameters: workflow_inputs and step_outputs
    # sink parameters: step_inputs and workflow_outputs

    # make a dictionary of source parameters, indexed by the "id" field
    src_dict: dict[str, CWLObjectType] = {}
    for param in workflow_inputs + step_outputs:
        src_dict[cast(str, param["id"])] = param

    step_inputs_val = _check_all_types(src_dict, step_inputs, "source", param_to_step)
    workflow_outputs_val = _check_all_types(
        src_dict, workflow_outputs, "outputSource", param_to_step
    )

    warnings = step_inputs_val["warning"] + workflow_outputs_val["warning"]
    exceptions = step_inputs_val["exception"] + workflow_outputs_val["exception"]

    warning_msgs = []
    exception_msgs = []
    for warning in warnings:
        src = warning.src
        sink = warning.sink
        linkMerge = warning.linkMerge
        sinksf = sorted(
            cast(str, p["pattern"])
            for p in cast(MutableSequence[CWLObjectType], sink.get("secondaryFiles", []))
            if p.get("required", True)
        )
        srcsf = sorted(
            cast(str, p["pattern"])
            for p in cast(MutableSequence[CWLObjectType], src.get("secondaryFiles", []))
        )
        # Every secondaryFile required by the sink, should be declared
        # by the source
        missing = missing_subset(srcsf, sinksf)
        src_name = shortname(cast(str, src["id"]))
        sink_id = cast(str, sink["id"])
        sink_name = shortname(sink_id)
        if missing:
            msg1 = "Parameter '{}' requires secondaryFiles {} but".format(
                sink_name,
                missing,
            )
            msg3 = SourceLine(src, "id").makeError(
                "source '%s' does not provide those secondaryFiles." % (src_name)
            )
            msg4 = SourceLine(src.get("_tool_entry", src), "secondaryFiles").makeError(
                "To resolve, add missing secondaryFiles patterns to definition of '%s' or"
                % (src_name)
            )
            msg5 = SourceLine(sink.get("_tool_entry", sink), "secondaryFiles").makeError(
                "mark missing secondaryFiles in definition of '%s' as optional." % (sink_name)
            )
            msg = SourceLine(sink).makeError(
                "{}\n{}".format(msg1, bullets([msg3, msg4, msg5], "  "))
            )
        elif sink.get("not_connected"):
            if not sink.get("used_by_step"):
                msg = SourceLine(sink, "type").makeError(
                    "'%s' is not an input parameter of %s, expected %s"
                    % (
                        sink_name,
                        param_to_step[sink_id]["run"],
                        ", ".join(
                            shortname(cast(str, s["id"]))
                            for s in cast(
                                list[dict[str, Union[str, bool]]],
                                param_to_step[sink_id]["inputs"],
                            )
                            if not s.get("not_connected")
                        ),
                    )
                )
            else:
                msg = ""
        else:
            msg = (
                SourceLine(src, "type").makeError(
                    "Source '%s' of type %s may be incompatible"
                    % (src_name, json_dumps(src["type"]))
                )
                + "\n"
                + SourceLine(sink, "type").makeError(
                    "  with sink '{}' of type {}".format(sink_name, json_dumps(sink["type"]))
                )
            )
            if linkMerge is not None:
                msg += "\n" + SourceLine(sink).makeError(
                    "  source has linkMerge method %s" % linkMerge
                )

        if warning.message is not None:
            msg += "\n" + SourceLine(sink).makeError("  " + warning.message)

        if msg:
            warning_msgs.append(msg)

    for exception in exceptions:
        src = exception.src
        sink = exception.sink
        linkMerge = exception.linkMerge
        extra_message = exception.message
        msg = (
            SourceLine(src, "type").makeError(
                "Source '%s' of type %s is incompatible"
                % (shortname(cast(str, src["id"])), json_dumps(src["type"]))
            )
            + "\n"
            + SourceLine(sink, "type").makeError(
                "  with sink '{}' of type {}".format(
                    shortname(cast(str, sink["id"])), json_dumps(sink["type"])
                )
            )
        )
        if extra_message is not None:
            msg += "\n" + SourceLine(sink).makeError("  " + extra_message)

        if linkMerge is not None:
            msg += "\n" + SourceLine(sink).makeError("  source has linkMerge method %s" % linkMerge)
        exception_msgs.append(msg)

    for sink in step_inputs:
        sink_type = cast(Union[str, list[str], list[CWLObjectType], CWLObjectType], sink["type"])
        if (
            "null" != sink_type
            and "null" not in sink_type
            and "source" not in sink
            and "default" not in sink
            and "valueFrom" not in sink
        ):
            msg = SourceLine(sink).makeError(
                "Required parameter '%s' does not have source, default, or valueFrom expression"
                % shortname(cast(str, sink["id"]))
            )
            exception_msgs.append(msg)

    all_warning_msg = strip_dup_lineno("\n".join(warning_msgs))
    all_exception_msg = strip_dup_lineno("\n" + "\n".join(exception_msgs))

    if all_warning_msg:
        _logger.warning("Workflow checker warning:\n%s", all_warning_msg)
    if exceptions:
        raise ValidationException(all_exception_msg)


class _SrcSink(NamedTuple):
    """An error or warning message about a connection between two points of the workflow graph."""

    src: CWLObjectType
    sink: CWLObjectType
    linkMerge: Optional[str]
    message: Optional[str]


def _check_all_types(
    src_dict: dict[str, CWLObjectType],
    sinks: MutableSequence[CWLObjectType],
    sourceField: Union[Literal["source"], Literal["outputSource"]],
    param_to_step: dict[str, CWLObjectType],
) -> dict[str, list[_SrcSink]]:
    """
    Given a list of sinks, check if their types match with the types of their sources.

    :raises WorkflowException: if there is an unrecognized linkMerge value
                               (from :py:func:`check_types`)
    :raises ValidationException: if a sourceField is missing
    """
    validation: dict[str, list[_SrcSink]] = {"warning": [], "exception": []}
    for sink in sinks:
        if sourceField in sink:
            valueFrom = cast(Optional[str], sink.get("valueFrom"))
            pickValue = cast(Optional[str], sink.get("pickValue"))

            extra_message = None
            if pickValue is not None:
                extra_message = "pickValue is: %s" % pickValue

            if isinstance(sink[sourceField], MutableSequence):
                linkMerge: Optional[str] = cast(
                    Optional[str],
                    sink.get(
                        "linkMerge",
                        ("merge_nested" if len(cast(Sized, sink[sourceField])) > 1 else None),
                    ),
                )

                if pickValue in ["first_non_null", "the_only_non_null"]:
                    linkMerge = None

                srcs_of_sink: list[CWLObjectType] = []
                for parm_id in cast(MutableSequence[str], sink[sourceField]):
                    srcs_of_sink += [src_dict[parm_id]]
                    if is_conditional_step(param_to_step, parm_id) and pickValue is None:
                        validation["warning"].append(
                            _SrcSink(
                                src_dict[parm_id],
                                sink,
                                linkMerge,
                                message="Source is from conditional step, but pickValue is not used",
                            )
                        )
                    if is_all_output_method_loop_step(param_to_step, parm_id):
                        src_dict[parm_id]["type"] = {
                            "type": "array",
                            "items": src_dict[parm_id]["type"],
                        }
            else:
                parm_id = cast(str, sink[sourceField])
                if parm_id not in src_dict:
                    raise SourceLine(sink, sourceField, ValidationException).makeError(
                        f"{sourceField} not found: {parm_id}"
                    )

                srcs_of_sink = [src_dict[parm_id]]
                linkMerge = None

                if pickValue is not None:
                    validation["warning"].append(
                        _SrcSink(
                            src_dict[parm_id],
                            sink,
                            linkMerge,
                            message="pickValue is used but only a single input source is declared",
                        )
                    )

                if is_conditional_step(param_to_step, parm_id):
                    src_typ = aslist(srcs_of_sink[0]["type"])
                    snk_typ = sink["type"]

                    if "null" not in src_typ:
                        src_typ = ["null"] + cast(list[Any], src_typ)

                    if "null" not in cast(
                        Union[list[str], CWLObjectType], snk_typ
                    ):  # Given our type names this works even if not a list
                        validation["warning"].append(
                            _SrcSink(
                                src_dict[parm_id],
                                sink,
                                linkMerge,
                                message="Source is from conditional step and may produce `null`",
                            )
                        )

                    srcs_of_sink[0]["type"] = src_typ

                if is_all_output_method_loop_step(param_to_step, parm_id):
                    src_dict[parm_id]["type"] = {
                        "type": "array",
                        "items": src_dict[parm_id]["type"],
                    }

            for src in srcs_of_sink:
                check_result = check_types(src, sink, linkMerge, valueFrom)
                if check_result == "warning":
                    validation["warning"].append(
                        _SrcSink(src, sink, linkMerge, message=extra_message)
                    )
                elif check_result == "exception":
                    validation["exception"].append(
                        _SrcSink(src, sink, linkMerge, message=extra_message)
                    )

    return validation


def circular_dependency_checker(step_inputs: list[CWLObjectType]) -> None:
    """
    Check if a workflow has circular dependency.

    :raises ValidationException: If a circular dependency is detected.
    """
    adjacency = get_dependency_tree(step_inputs)
    vertices = adjacency.keys()
    processed: list[str] = []
    cycles: list[list[str]] = []
    for vertex in vertices:
        if vertex not in processed:
            traversal_path = [vertex]
            processDFS(adjacency, traversal_path, processed, cycles)
    if cycles:
        exception_msg = "The following steps have circular dependency:\n"
        cyclestrs = [str(cycle) for cycle in cycles]
        exception_msg += "\n".join(cyclestrs)
        raise ValidationException(exception_msg)


def get_dependency_tree(step_inputs: list[CWLObjectType]) -> dict[str, list[str]]:
    """Get the dependency tree in the form of adjacency list."""
    adjacency = {}  # adjacency list of the dependency tree
    for step_input in step_inputs:
        if "source" in step_input:
            if isinstance(step_input["source"], list):
                vertices_in = [get_step_id(cast(str, src)) for src in step_input["source"]]
            else:
                vertices_in = [get_step_id(cast(str, step_input["source"]))]
            vertex_out = get_step_id(cast(str, step_input["id"]))
            for vertex_in in vertices_in:
                if vertex_in not in adjacency:
                    adjacency[vertex_in] = [vertex_out]
                elif vertex_out not in adjacency[vertex_in]:
                    adjacency[vertex_in].append(vertex_out)
            if vertex_out not in adjacency:
                adjacency[vertex_out] = []
    return adjacency


def processDFS(
    adjacency: dict[str, list[str]],
    traversal_path: list[str],
    processed: list[str],
    cycles: list[list[str]],
) -> None:
    """Perform depth first search."""
    tip = traversal_path[-1]
    for vertex in adjacency[tip]:
        if vertex in traversal_path:
            i = traversal_path.index(vertex)
            cycles.append(traversal_path[i:])
        elif vertex not in processed:
            traversal_path.append(vertex)
            processDFS(adjacency, traversal_path, processed, cycles)
    processed.append(tip)
    traversal_path.pop()


def get_step_id(field_id: str) -> str:
    """Extract step id from either input or output fields."""
    if "/" in field_id.split("#")[1]:
        step_id = "/".join(field_id.split("/")[:-1])
    else:
        step_id = field_id.split("#")[0]
    return step_id


def is_conditional_step(param_to_step: dict[str, CWLObjectType], parm_id: str) -> bool:
    """Return True if the step given by the parm_id is a conditional step."""
    if (source_step := param_to_step.get(parm_id)) is not None:
        if source_step.get("when") is not None:
            return True
    return False


def is_all_output_method_loop_step(param_to_step: dict[str, CWLObjectType], parm_id: str) -> bool:
    """Check if a step contains a `loop` directive with `all_iterations` outputMethod."""
    source_step: Optional[MutableMapping[str, Any]] = param_to_step.get(parm_id)
    if source_step is not None:
        if (
            source_step.get("loop") is not None
            and source_step.get("outputMethod") == "all_iterations"
        ):
            return True
    return False


def loop_checker(steps: Iterator[MutableMapping[str, Any]]) -> None:
    """
    Check `loop` compatibility with other directives.

    :raises ValidationException: If there is an incompatible combination between `loop` and `scatter`.
    """
    exceptions = []
    for step in steps:
        if "loop" in step:
            if "when" not in step:
                exceptions.append(
                    SourceLine(step, "id").makeError(
                        "The `when` clause is mandatory when the `loop` directive is defined."
                    )
                )
            if "scatter" in step:
                exceptions.append(
                    SourceLine(step, "id").makeError(
                        "The `loop` clause is not compatible with the `scatter` directive."
                    )
                )
    if exceptions:
        raise ValidationException("\n".join(exceptions))
