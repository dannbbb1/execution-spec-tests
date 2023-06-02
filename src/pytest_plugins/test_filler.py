"""
Top-level pytest configuration file providing:
- Command-line options,
- Test-fixtures that can be used by all test cases,
and that modifies pytest hooks in order to fill test specs for all tests and
writes the generated fixtures to file.
"""
import json
import os
import re
import subprocess
from typing import Any, Dict, List, Tuple, Type

import pytest

from ethereum_test_tools import (
    BaseTest,
    BlockchainTest,
    BlockchainTestFiller,
    Fixture,
    JSONEncoder,
    StateTest,
    StateTestFiller,
    fill_test,
)
from evm_block_builder import EvmBlockBuilder
from evm_transition_tool import EvmTransitionTool


def pytest_addoption(parser):
    """
    Adds command-line options to pytest.
    """
    evm_group = parser.getgroup(
        "evm", "Arguments defining evm executable behavior"
    )
    evm_group.addoption(
        "--evm-bin",
        action="store",
        dest="evm_bin",
        default=None,
        help="Path to evm executable that provides `t8n` and `b11r` ",
    )
    evm_group.addoption(
        "--traces",
        action="store_true",
        dest="evm_collect_traces",
        default=None,
        help="Collect traces of the execution information from the "
        + "transition tool",
    )

    test_group = parser.getgroup(
        "fillers", "Arguments defining filler location and output"
    )
    test_group.addoption(
        "--filler-path",
        action="store",
        dest="filler_path",
        default="./fillers/",
        help="Path to filler directives",
    )
    test_group.addoption(
        "--output",
        action="store",
        dest="output",
        default="./fixtures/",
        help="Directory to store the generated test fixtures. Can be deleted.",
    )


@pytest.hookimpl(trylast=True)
def pytest_report_header(config, start_path):
    """Add lines to pytest's console output header"""
    t8n = EvmTransitionTool(
        binary=config.getoption("evm_bin"),
        trace=config.getoption("evm_collect_traces"),
    )
    result = subprocess.run(
        ["solc", "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    solc_output = result.stdout.decode().split("\n")

    version_pattern = r"0\.\d+\.\d+\+\S+"
    solc_version_string = None

    for line in solc_output:
        match = re.search(version_pattern, line)
        if match:
            solc_version_string = match.group(0)
            break
    return [f"{t8n.version()}, solc version {solc_version_string}"]


@pytest.fixture(autouse=True, scope="session")
def evm_bin(request):
    """
    Returns the configured evm tool binary path.
    """
    return request.config.getoption("evm_bin")


@pytest.fixture(autouse=True, scope="session")
def t8n(request, evm_bin):
    """
    Returns the configured transition tool.
    """
    t8n = EvmTransitionTool(
        binary=evm_bin,
        trace=request.config.getoption("evm_collect_traces"),
    )
    return t8n


@pytest.fixture(autouse=True, scope="session")
def b11r(request, evm_bin):
    """
    Returns the configured block builder tool.
    """
    b11r = EvmBlockBuilder(binary=evm_bin)
    return b11r


class FixtureCollector:
    """
    Collects all fixtures generated by the test cases.
    """

    all_fixtures: Dict[str, List[Tuple[str, Any]]]
    output_dir: str

    def __init__(self, output_dir: str) -> None:
        self.all_fixtures = {}
        self.output_dir = output_dir

    def add_fixture(self, item, fixture: Fixture) -> None:
        """
        Adds a fixture to the list of fixtures of a given test case.
        """

        def get_module_dir(item) -> str:
            """
            Returns the directory of the test case module.
            """
            dirname = os.path.dirname(item.path)
            basename, _ = os.path.splitext(item.path)
            module_path_no_ext = os.path.join(dirname, basename)
            module_dir = os.path.relpath(
                module_path_no_ext,
                item.funcargs["filler_path"],
            )
            return module_dir

        module_dir = get_module_dir(item) + "/" + item.originalname
        if module_dir not in self.all_fixtures:
            self.all_fixtures[module_dir] = []
        m = re.match(r".*?\[(.*)\]", item.name)
        if not m:
            raise Exception("Could not parse test name: " + item.name)
        name = m.group(1)
        if fixture.name:
            name += "-" + fixture.name
        jsonFixture = json.loads(json.dumps(fixture, cls=JSONEncoder))
        self.all_fixtures[module_dir].append((name, jsonFixture))

    def dump_fixtures(self) -> None:
        """
        Dumps all collected fixtures to their respective files.
        """
        for module_file, fixtures in self.all_fixtures.items():
            output_json = {}
            for index, name_fixture in enumerate(fixtures):
                name, fixture = name_fixture
                name = str(index).zfill(3) + "-" + name
                output_json[name] = fixture
            file_path = self.output_dir + os.sep + module_file + ".json"
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w") as f:
                json.dump(output_json, f, indent=4)


@pytest.fixture(scope="module")
def fixture_collector(request):
    """
    Returns the configured fixture collector instance used for all tests
    in one test module.
    """
    fixture_collector = FixtureCollector(
        output_dir=request.config.getoption("output")
    )
    yield fixture_collector
    fixture_collector.dump_fixtures()


@pytest.fixture(autouse=True, scope="session")
def engine():
    """
    Returns the sealEngine used in the generated test fixtures.
    """
    return "NoProof"


@pytest.fixture(autouse=True, scope="session")
def filler_path(request):
    """
    Returns the directory containing the fillers to execute.
    """
    return request.config.getoption("filler_path")


@pytest.fixture(autouse=True)
def eips():
    """
    A fixture specifying that, by default, no EIPs should be activated for
    fillers.

    This fixture (function) may be redefined in test filler modules in order
    to overwrite this default and return a list of integers specifying which
    EIPs should be activated for the fillers in scope.
    """
    return []


SPEC_TYPES: List[Type[BaseTest]] = [StateTest, BlockchainTest]
SPEC_TYPES_PARAMETERS: List[str] = [
    s.pytest_parameter_name() for s in SPEC_TYPES
]


@pytest.fixture(scope="function")
def state_test(
    request, t8n, b11r, fork, engine, reference_spec, eips, fixture_collector
) -> StateTestFiller:
    """
    Fixture used to instantiate an auto-fillable StateTest object from within
    a test function.

    Every test that defines a StateTest filler must explicitly specify this
    fixture in its function arguments and set the StateTestWrapper's spec
    property.

    Implementation detail: It must be scoped on test function level to avoid
    leakage between tests.
    """

    class StateTestWrapper(StateTest):
        def __init__(self, *args, **kwargs):
            super(StateTestWrapper, self).__init__(*args, **kwargs)
            fixture_collector.add_fixture(
                request.node,
                fill_test(
                    t8n,
                    b11r,
                    self,
                    fork,
                    engine,
                    reference_spec,
                    eips=eips,
                ),
            )

    return StateTestWrapper


@pytest.fixture(scope="function")
def blockchain_test(
    request, t8n, b11r, fork, engine, reference_spec, eips, fixture_collector
) -> BlockchainTestFiller:
    """
    Fixture used to define an auto-fillable BlockchainTest analogous to the
    state_test fixture for StateTests.
    See the state_test fixture docstring for details.
    """

    class BlockchainTestWrapper(BlockchainTest):
        def __init__(self, *args, **kwargs):
            super(BlockchainTestWrapper, self).__init__(*args, **kwargs)
            fixture_collector.add_fixture(
                request.node,
                fill_test(
                    t8n,
                    b11r,
                    self,
                    fork,
                    engine,
                    reference_spec,
                    eips=eips,
                ),
            )

    return BlockchainTestWrapper


def pytest_collection_modifyitems(items, config):
    """
    A pytest hook called during collection, after all items have been
    collected.

    Here we dynamically apply "state_test" or "blockchain_test" markers
    to a test if the test function uses the corresponding fixture.
    """
    for item in items:
        if "state_test" in item.fixturenames:
            marker = pytest.mark.state_test()
            item.add_marker(marker)
        elif "blockchain_test" in item.fixturenames:
            marker = pytest.mark.blockchain_test()
            item.add_marker(marker)


def pytest_make_parametrize_id(config, val, argname):
    """
    Pytest hook called when generating test ids. We use this to generate
    more readable test ids for the generated tests.
    """
    return f"{argname}={val}"


def pytest_runtest_call(item):
    """
    Pytest hook called in the context of test execution.
    """

    class InvalidFiller(Exception):
        def __init__(self, message):
            super().__init__(message)

    if (
        "state_test" in item.fixturenames
        and "blockchain_test" in item.fixturenames
    ):
        raise InvalidFiller(
            "A filler should only implement either a state test or "
            "a blockchain test; not both."
        )

    # Check that the test defines either test type as parameter.
    if not any([i for i in item.funcargs if i in SPEC_TYPES_PARAMETERS]):
        pytest.fail(
            "Test must define either one of the following parameters to "
            + "properly generate a test: "
            + ", ".join(SPEC_TYPES_PARAMETERS)
        )
