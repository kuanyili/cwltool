"""
Confirm that we can subclass and/or serializae certain classes used by 3rd parties.

Especially if those classes are (or become) compiled with mypyc.
"""

import pickle

import pytest
from ruamel.yaml.comments import CommentedMap
from schema_salad.avro import schema

from cwltool.builder import Builder
from cwltool.command_line_tool import CommandLineTool, ExpressionTool
from cwltool.context import LoadingContext, RuntimeContext
from cwltool.procgenerator import ProcessGenerator
from cwltool.stdfsaccess import StdFsAccess
from cwltool.update import INTERNAL_VERSION
from cwltool.workflow import Workflow

from .test_anon_types import snippet
from .util import get_data


@pytest.mark.parametrize("snippet", snippet)
def test_subclass_CLT(snippet: CommentedMap) -> None:
    """We can subclass CommandLineTool."""

    class TestCLT(CommandLineTool):
        test = True

    a = TestCLT(snippet, LoadingContext())
    assert a.test is True


@pytest.mark.parametrize("snippet", snippet)
def test_subclass_exprtool(snippet: CommentedMap) -> None:
    """We can subclass ExpressionTool."""

    class TestExprTool(ExpressionTool):
        test = False

    a = TestExprTool(snippet, LoadingContext())
    assert a.test is False


@pytest.mark.parametrize("snippet", snippet)
def test_pickle_unpickle_workflow(snippet: CommentedMap) -> None:
    """We can pickle & unpickle a Workflow."""

    a = Workflow(snippet, LoadingContext())
    stream = pickle.dumps(a)
    assert stream
    assert pickle.loads(stream)


def test_pickle_processgenerator() -> None:
    """We can pickle ProcessGenerator."""

    pytoolgen = get_data("tests/wf/generator/pytoolgen.cwl")
    a = ProcessGenerator(
        CommentedMap(
            {
                "cwlVersion": "v1.0",
                "id": f"file://{pytoolgen}",
                "$namespaces": {"cwltool": "http://commonwl.org/cwltool#"},
                "class": "cwltool:ProcessGenerator",
                "inputs": {},
                "outputs": {},
                "run": {
                    "id": f"file://{pytoolgen}#f9f617af-3088-4ade-bbf3-acd1d6f51355",
                    "cwlVersion": "v1.0",
                    "class": "CommandLineTool",
                    "inputs": {},
                    "outputs": {},
                    "baseCommand": "echo",
                },
            },
        ),
        LoadingContext(),
    )

    assert pickle.dumps(a)


def test_serialize_builder() -> None:
    """We can pickle Builder."""
    runtime_context = RuntimeContext()
    builder = Builder(
        {},
        [],
        [],
        {},
        schema.Names(),
        [],
        [],
        {},
        None,
        None,
        StdFsAccess,
        StdFsAccess(""),
        None,
        0.1,
        False,
        False,
        False,
        "no_listing",
        runtime_context.get_outdir(),
        runtime_context.get_tmpdir(),
        runtime_context.get_stagedir(),
        INTERNAL_VERSION,
        "docker",
    )
    assert pickle.dumps(builder)


def test_pickle_unpickle_runtime_context() -> None:
    """We can pickle & unpickle RuntimeContext"""

    runtime_context = RuntimeContext()
    stream = pickle.dumps(runtime_context)
    assert stream
    assert pickle.loads(stream)
