"""Intermediate layer between `Timer` and `valgrind`."""
import collections
import enum
import dataclasses
import itertools as it
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from types import ModuleType
from typing import (
    cast, Any, Callable, DefaultDict, Dict, Generator, List, NamedTuple,
    Optional, Tuple, Union, TYPE_CHECKING)

import torch

# This is to support back-testing. Once that is no longer needed, we should
# just use `torch.jit.ScriptFunction/Module`
from torch._C import ScriptFunction, ScriptModule
from torch.utils.benchmark.utils import common


__all__ = ["FunctionCount", "FunctionCounts", "CallgrindStats", "CopyIfCallgrind"]


if TYPE_CHECKING:
    CompletedProcessType = subprocess.CompletedProcess[str]
else:
    CompletedProcessType = subprocess.CompletedProcess


FunctionCount = NamedTuple("FunctionCount", [("count", int), ("function", str)])


@dataclasses.dataclass(repr=False, eq=False, frozen=True)
class FunctionCounts(object):
    _data: Tuple[FunctionCount, ...]
    inclusive: bool

    # For normal use, torch._tensor_str.PRINT_OPTS.linewidth determines
    # the print settings. This is simply to allow hermetic unit tests.
    _linewidth: Optional[int] = None

    def __iter__(self) -> Generator[FunctionCount, None, None]:
        for i in self._data:
            yield i

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, item: Any) -> "Union[FunctionCount, FunctionCounts]":
        data: Union[FunctionCount, Tuple[FunctionCount, ...]] = self._data[item]
        return (
            FunctionCounts(cast(Tuple[FunctionCount, ...], data), self.inclusive)
            if isinstance(data, tuple) else data
        )

    def __repr__(self) -> str:
        count_len = 0
        for c, _ in self:
            # Account for sign in string length.
            count_len = max(count_len, len(str(c)) + int(c < 0))

        lines = []
        linewidth = self._linewidth or torch._tensor_str.PRINT_OPTS.linewidth
        fn_str_len = max(linewidth - count_len - 4, 40)
        for c, fn in self:
            if len(fn) > fn_str_len:
                left_len = int((fn_str_len - 5) // 2)
                fn = fn[:left_len] + " ... " + fn[-(fn_str_len - left_len - 5):]
            lines.append(f"  {c:>{count_len}}  {fn}")

        if len(lines) > 18:
            lines = lines[:9] + ["...".rjust(count_len + 2)] + lines[-9:]

        if not self.inclusive:
            lines.extend(["", f"Total: {self.sum()}"])

        return "\n".join([super().__repr__()] + lines)

    def __add__(
        self,
        other,  # type: FunctionCounts
    ) -> "FunctionCounts":
        return self._merge(other, lambda c: c)

    def __sub__(
        self,
        other,  # type: FunctionCounts
    ) -> "FunctionCounts":
        return self._merge(other, lambda c: -c)

    def transform(self, map_fn: Callable[[str], str]) -> "FunctionCounts":
        counts: DefaultDict[str, int] = collections.defaultdict(int)
        for c, fn in self._data:
            counts[map_fn(fn)] += c

        return self._from_dict(counts, self.inclusive)

    def filter(self, filter_fn: Callable[[str], bool]) -> "FunctionCounts":
        return FunctionCounts(tuple(i for i in self if filter_fn(i.function)), self.inclusive)

    def sum(self) -> int:
        return sum(c for c, _ in self)

    def denoise(self) -> "FunctionCounts":
        """Remove known noisy instructions.

        Several instructions in the CPython interpreter are rather noisy. These
        instructions involve unicode to dictionary lookups which Python uses to
        map variable names. FunctionCounts is generally a content agnostic
        container, however this is sufficiently important for obtaining
        reliable results to warrant an exception."""
        return self.filter(lambda fn: "dictobject.c:lookdict_unicode" not in fn)

    def _merge(
        self,
        second,   # type: FunctionCounts
        merge_fn: Callable[[int], int]
    ) -> "FunctionCounts":
        assert self.inclusive == second.inclusive, "Cannot merge inclusive and exclusive counts."
        counts: DefaultDict[str, int] = collections.defaultdict(int)
        for c, fn in self:
            counts[fn] += c

        for c, fn in second:
            counts[fn] += merge_fn(c)

        return self._from_dict(counts, self.inclusive)

    @staticmethod
    def _from_dict(counts: Dict[str, int], inclusive: bool) -> "FunctionCounts":
        flat_counts = (FunctionCount(c, fn) for fn, c in counts.items() if c)
        return FunctionCounts(tuple(sorted(flat_counts, reverse=True)), inclusive)


@dataclasses.dataclass(repr=False, eq=False, frozen=True)
class CallgrindStats(object):
    task_spec: common.TaskSpec
    number_per_run: int
    built_with_debug_symbols: bool
    baseline_inclusive_stats: FunctionCounts
    baseline_exclusive_stats: FunctionCounts
    stmt_inclusive_stats: FunctionCounts
    stmt_exclusive_stats: FunctionCounts

    def __repr__(self) -> str:
        newline = "\n"  # `\` cannot appear in fstring code section.
        base_stats = self.baseline_exclusive_stats
        output = f"""
{super().__repr__()}
{self.task_spec.summarize()}
  {'':>25}All{'':>10}Noisy symbols removed
    Instructions: {self.counts(denoise=False):>12}{'':>15}{self.counts(denoise=True):>12}
    Baseline:     {base_stats.sum():>12}{'':>15}{base_stats.denoise().sum():>12}
{self.number_per_run} runs per measurement, {self.task_spec.num_threads} thread{'s' if self.task_spec.num_threads > 1 else ''}
""".strip()
        if not self.built_with_debug_symbols:
            output += textwrap.dedent("""
            Warning: PyTorch was not built with debug symbols.
                     Source information may be limited. Rebuild with
                     REL_WITH_DEB_INFO=1 for more detailed results.""")
        return output

    def stats(self, inclusive: bool = False) -> FunctionCounts:
        """Returns stats as a tuple of (count, function)

        `inclusive` matches the semantics of callgrind. If True, the counts
        include instructions executed by children. `inclusive=True` is useful
        for identifying hot spots in code; `inclusive=False` is useful for
        identifying reducing noise when diffing counts from two different
        runs. (See CallgrindStats.delta(...) for more details)
        """
        if inclusive:
            return self.stmt_inclusive_stats - self.baseline_inclusive_stats
        return self.stmt_exclusive_stats - self.baseline_exclusive_stats

    def counts(self, *, denoise: bool = False) -> int:
        """Returns the total number of instructions executed.

        See `FunctionCounts.denoise()` for an explation of the `denoise` arg.
        """
        stats = self.stmt_exclusive_stats
        return (stats.denoise() if denoise else stats).sum()

    # FIXME: Once 3.7 is the minimum version, type annotate `other` per PEP 563
    def delta(
        self,
        other,  # type: CallgrindStats
        inclusive: bool = False,
        subtract_baselines: bool = True
    ) -> FunctionCounts:
        """Diff two sets of counts.

        One common reason to collect instruction counts is to determine the
        the effect that a particular change will have on the number of instructions
        needed to perform some unit of work. If a change increases that number, the
        next logical question is "why". This generally involves looking at what part
        if the code increased in instruction count. This function automates that
        process so that one can easily diff counts on both an inclusive and
        exclusive basis. The `subtract_baselines` argument allows one to disable
        baseline correction, though in most cases it shouldn't matter as the
        baselines are expected to more or less cancel out.
        """
        if subtract_baselines:
            return self.stats(inclusive=inclusive) - other.stats(inclusive=inclusive)
        elif inclusive:
            return self.stmt_inclusive_stats - other.stmt_inclusive_stats
        return self.stmt_exclusive_stats - other.stmt_exclusive_stats

    def as_standardized(self) -> "CallgrindStats":
        """Strip library names and some prefixes from function strings.

        When comparing two different sets of instruction counts, on stumbling
        block can be path prefixes. Callgrind includes the full filepath
        when reporting a function (as it should). However, this can cause
        issues when diffing profiles. If a key component such as Python
        or PyTorch was built in separate locations in the two profiles, which
        can result in something resembling:
            23234231 /tmp/first_build_dir/thing.c:foo(...)
             9823794 /tmp/first_build_dir/thing.c:bar(...)
              ...
               53453 .../aten/src/Aten/...:function_that_actually_changed(...)
              ...
             -9823794 /tmp/second_build_dir/thing.c:bar(...)
            -23234231 /tmp/second_build_dir/thing.c:foo(...)

        Stripping prefixes can ameliorate this issue by regularizing the
        strings and causing better cancellation of equivilent call sites
        when diffing.
        """
        def strip(stats: FunctionCounts) -> FunctionCounts:
            transforms = (
                # PyTorch may have been built in different locations.
                (r"^.+build/\.\./", "build/../"),
                (r"^.+/" + re.escape("build/aten/"), "build/aten/"),

                # "Python" and "Objects" come from CPython.
                (r"^.+/" + re.escape("Python/"), "Python/"),
                (r"^.+/" + re.escape("Objects/"), "Objects/"),

                # Strip library name. e.g. `libtorch.so`
                (r"\s\[.+\]$", ""),
            )

            for before, after in transforms:
                stats = stats.transform(lambda fn: re.sub(before, after, fn))

            return stats

        return CallgrindStats(
            task_spec=self.task_spec,
            number_per_run=self.number_per_run,
            built_with_debug_symbols=self.built_with_debug_symbols,
            baseline_inclusive_stats=strip(self.baseline_inclusive_stats),
            baseline_exclusive_stats=strip(self.baseline_exclusive_stats),
            stmt_inclusive_stats=strip(self.stmt_inclusive_stats),
            stmt_exclusive_stats=strip(self.stmt_exclusive_stats),
        )


class Serialization(enum.Enum):
    PICKLE = 0
    TORCH = 1
    TORCH_JIT = 2


_GLOBALS_ALLOWED_TYPES: Dict[Serialization, Tuple[Any, ...]] = {
    Serialization.PICKLE: (str, bytes, bool, int, float, complex),
    Serialization.TORCH_JIT: (ScriptFunction, ScriptModule),
    Serialization.TORCH: (torch.nn.Module,),
}


class CopyIfCallgrind:
    """Signal that a global may be replaced with a deserialized copy.

    See `GlobalsBridge` for why this matters.
    """
    def __init__(self, value: Any, *, setup: Optional[str] = None):
        for method, supported_types in _GLOBALS_ALLOWED_TYPES.items():
            if any(isinstance(value, t) for t in supported_types):
                self._value: Any = value
                self._setup: Optional[str] = setup
                self._serialization: Serialization = method
                break
        else:
            supported_str = "\n".join([
                getattr(t, "__name__", repr(t))
                for t in it.chain(_GLOBALS_ALLOWED_TYPES.values())])

            raise ValueError(
                f"Unsupported type: {type(value)}\n"
                f"`collect_callgrind` restricts globals to the following types:\n"
                f"{textwrap.indent(supported_str, '  ')}"
            )

    @property
    def value(self) -> Any:
        return self._value

    @property
    def setup(self) -> Optional[str]:
        return self._setup

    @property
    def serialization(self) -> Serialization:
        return self._serialization

    @staticmethod
    def unwrap_all(globals: Dict[str, Any]) -> Dict[str, Any]:
        return {
            k: (v.value if isinstance(v, CopyIfCallgrind) else v)
            for k, v in globals.items()
        }


class GlobalsBridge:
    """Handle the transfer of (certain) globals when collecting Callgrind statistics.

    Key takeaway: Any globals passed must be wrapped in `CopyIfCallgrind` to
                  work with `Timer.collect_callgrind`.

    Consider the following code snippet:
    ```
        import pickle
        import timeit

        class Counter:
            value = 0

            def __call__(self):
                self.value += 1

        counter = Counter()
        timeit.Timer("counter()", globals={"counter": counter}).timeit(10)
        print(counter.value)  # 10

        timeit.Timer(
            "counter()",
            globals={"counter": pickle.loads(pickle.dumps(counter))}
        ).timeit(20)
        print(counter.value)  # Still 10
    ```

    In the first case, `stmt` is executed using the objects in `globals`;
    however, the addition of serialization and deserialization changes the
    semantics and may meaningfully change behavior.

    This is a practical consideration when collecting Callgrind statistics.
    Unlike `exec` based execution (which `timeit` uses under the hood) which
    can share in-memory data structures with the caller, Callgrind collection
    requires an entirely new process in order to run under Valgrind. This means
    that any data structures used for statement execution will have to be
    serialized and deserialized in the subprocess.

    In order to avoid surprising semantics from (user invisible) process
    boundaries, what can be passed through `globals` is severely restricted
    for `Timer.collect_callgrind`. It is expected that most setup should be
    achievable (albeit perhaps less ergonomically) by passing a `setup`
    string.

    There are, however, exceptions. One such class are TorchScripted functions.
    Because they require a concrete file with source code it is not possible
    to define them using a `setup` string. Another group are torch.nn.Modules,
    whose construction can be complex and prohibitively cumbersome to coerce
    into a `setup` string. Finally, most builtin types are sufficiently well
    behaved and sufficiently common to warrant allowing as well. (e.g.
    `globals={"n": 1}` is very convenient.)

    Fortunately, all have well defined serialization semantics. This class
    is responsible for enabling the Valgrind subprocess to use elements in
    `globals` so long as they are an allowed type.

    Caveats:
        The user is required to acknowledge this serialization by wrapping
        elements in `globals` with `CopyIfCallgrind`.

        While ScriptFunction and ScriptModule are expected to save and load
        quite robustly, it is up to the user to ensure that an nn.Module can
        un-pickle successfully.

        `torch.Tensor` and `np.ndarray` are deliberately excluded. The
        serialization/deserialization process perturbs the representation of a
        tensor in ways that could result in incorrect measurements. For example,
        if a tensor lives in pinned CPU memory, this fact would not be preserved
        by a dump, and that will in turn change the performance of certain CUDA
        operations.
    """

    def __init__(self, globals: Dict[str, Any], data_dir: str) -> None:
        self._globals: Dict[str, CopyIfCallgrind] = {}
        self._data_dir = data_dir
        if not os.path.exists(data_dir):
            os.mkdir(data_dir)

        if globals.get("torch", torch) is not torch:
            raise ValueError("`collect_callgrind` does not support mocking out `torch`.")

        for name, value in globals.items():
            if name in ("torch", "__builtins__"):
                # Torch will be imported by the collection script, and
                # __builtins__ is added by Timer.
                continue

            if not isinstance(value, CopyIfCallgrind):
                raise ValueError(
                    "`collect_callgrind` requires that globals be wrapped in "
                    "`CopyIfCallgrind` so that serialization is explicit."
                )

            self._globals[name] = value

    def construct(self) -> str:
        load_lines = []
        for name, wrapped_value in self._globals.items():
            if wrapped_value.setup is not None:
                load_lines.append(textwrap.dedent(wrapped_value.setup))

            if wrapped_value.serialization == Serialization.PICKLE:
                path = os.path.join(self._data_dir, f"{name}.pkl")
                load_lines.append(
                    f"with open({repr(path)}, 'rb') as f:\n    {name} = pickle.load(f)")
                with open(path, "wb") as f:
                    pickle.dump(wrapped_value.value, f)

            elif wrapped_value.serialization == Serialization.TORCH:
                path = os.path.join(self._data_dir, f"{name}.pt")
                load_lines.append(f"{name} = torch.load({repr(path)})")
                torch.save(wrapped_value.value, path)

            elif wrapped_value.serialization == Serialization.TORCH_JIT:
                path = os.path.join(self._data_dir, f"{name}.pt")
                load_lines.append(f"{name} = torch.jit.load({repr(path)})")
                with open(path, "wb") as f:
                    torch.jit.save(wrapped_value.value, f)

            else:
                raise NotImplementedError(
                    f"Unknown serialization method: {wrapped_value.serialization}")

        return "\n".join(load_lines)


class _ValgrindWrapper(object):
    def __init__(self) -> None:
        self._bindings_module: Optional[ModuleType] = None
        if hasattr(torch._C, "valgrind_supported_platform"):
            self._supported_platform: bool = torch._C.valgrind_supported_platform()

        else:
            print("Callgrind bindings are not present in `torch._C`. JIT-ing bindings.")
            # This import will JIT the Callgrind control bindings, so don't
            # invoke unless we know we'll need it.
            from torch.utils.benchmark.utils.valgrind_wrapper.compat_bindings import bindings
            self._bindings_module = bindings
            self._supported_platform = bindings.valgrind_supported_platform()

        self._commands_available: Dict[str, bool] = {}
        if self._supported_platform:
            # Only bother checking on supported platforms.
            for cmd in ("valgrind", "callgrind_control", "callgrind_annotate"):
                self._commands_available[cmd] = not subprocess.run(
                    ["which", cmd],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).returncode

        self._build_type: Optional[str] = None
        build_search = re.search("BUILD_TYPE=(.+),", torch.__config__.show())
        if build_search is not None:
            self._build_type = build_search.groups()[0].split(",")[0]

        self._baseline_cache: Dict[Tuple[int, int], Tuple[FunctionCounts, FunctionCounts]] = {}

    def _validate(self) -> None:
        if not self._supported_platform:
            raise OSError("Valgrind is not supported on this platform.")

        missing_cmds = [cmd for cmd, available in self._commands_available.items() if not available]
        if missing_cmds:
            raise OSError("Missing: " + ", ".join(missing_cmds))

    def collect_callgrind(
        self,
        task_spec: common.TaskSpec,
        globals: Dict[str, Any],
        number: int,
        collect_baseline: bool
    ) -> CallgrindStats:
        """Collect stats, and attach a reference run which can be used to filter interpreter overhead."""
        self._validate()
        baseline_inclusive_stats = FunctionCounts((), inclusive=True)
        baseline_exclusive_stats = FunctionCounts((), inclusive=False)
        if collect_baseline:
            cache_key = (number, task_spec.num_threads)
            if cache_key not in self._baseline_cache:
                self._baseline_cache[cache_key] = self._invoke(
                    common.TaskSpec(
                        stmt="pass",
                        setup="pass",
                        num_threads=task_spec.num_threads
                    ),
                    globals={},
                    number=number,
                )
            baseline_inclusive_stats, baseline_exclusive_stats = \
                self._baseline_cache[cache_key]

        stmt_inclusive_stats, stmt_exclusive_stats = self._invoke(task_spec, globals, number)
        return CallgrindStats(
            task_spec=task_spec,
            number_per_run=number,
            built_with_debug_symbols=self._build_type == "RelWithDebInfo",
            baseline_inclusive_stats=baseline_inclusive_stats,
            baseline_exclusive_stats=baseline_exclusive_stats,
            stmt_inclusive_stats=stmt_inclusive_stats,
            stmt_exclusive_stats=stmt_exclusive_stats,
        )

    def _invoke(
        self,
        task_spec: common.TaskSpec,
        globals: Dict[str, Any],
        number: int,
    ) -> Tuple[FunctionCounts, FunctionCounts]:
        """Core invocation method for Callgrind collection.

        Valgrind operates by effectively replacing the CPU with an emulated
        version which allows it to instrument any code at the cost of severe
        performance degradation. This has the practical effect that in order
        to collect Callgrind statistics, a new process has to be created
        running under `valgrind`. The steps for this process are:

        1) Create a scratch directory.
        2) Codegen a run script. (_ValgrindWrapper._construct_script)
            Inside the run script:
                * Validate that Python and torch match the parent process
                * Validate that it is indeed running under valgrind
                * Execute `setup` and warm up `stmt`
                * Begin collecting stats
                * Run the `stmt` loop
                * Stop collecting stats
        3) Parse the run results.
        4) Cleanup the scratch directory.
        """
        working_dir = tempfile.mkdtemp()
        data_dir = os.path.join(working_dir, "data")
        script_file = os.path.join(working_dir, "timer_callgrind.py")
        callgrind_out = os.path.join(working_dir, "callgrind.out")
        error_log = os.path.join(working_dir, "error.txt")
        stat_log = os.path.join(working_dir, "callgrind_stat.txt")
        stdout_stderr_log = os.path.join(working_dir, "stdout_stderr.log")

        def run(args: List[str], **kwargs: Any) -> Tuple[CompletedProcessType, str]:
            # https://thraxil.org/users/anders/posts/2008/03/13/Subprocess-Hanging-PIPE-is-your-enemy/
            f_stdout_stderr = open(stdout_stderr_log, "wb")
            try:
                invocation = subprocess.run(
                    args,
                    stdout=f_stdout_stderr,
                    stderr=subprocess.STDOUT,
                    **kwargs,
                )
                with open(stdout_stderr_log, "rt") as f:
                    return invocation, f.read()
            finally:
                f_stdout_stderr.close()

        try:
            if self._bindings_module is not None:
                shutil.copy(
                    self._bindings_module.__file__,
                    os.path.join(working_dir, os.path.split(self._bindings_module.__file__)[1])
                )

            with open(script_file, "wt") as f:
                f.write(self._construct_script(
                    task_spec,
                    globals=GlobalsBridge(globals, data_dir),
                    number=number,
                    error_log=error_log,
                    stat_log=stat_log,
                    bindings=self._bindings_module))

            valgrind_invocation, valgrind_invocation_output = run([
                "valgrind",
                "--tool=callgrind",
                f"--callgrind-out-file={callgrind_out}",
                "--dump-line=yes",
                "--dump-instr=yes",
                "--instr-atstart=yes",
                "--collect-atstart=no",
                "python",
                script_file,
            ])

            if valgrind_invocation.returncode:
                error_report = ""
                if os.path.exists(error_log):
                    with open(error_log, "rt") as f:
                        error_report = f.read()
                if not error_report:
                    error_report = "Unknown error.\n" + valgrind_invocation_output

                raise OSError(f"Failed to collect callgrind profile:\n{error_report}")

            def parse_output(inclusive: bool) -> FunctionCounts:
                annotate_invocation, annotate_invocation_output = run([
                    "callgrind_annotate",
                    f"--inclusive={'yes' if inclusive else 'no'}",
                    callgrind_out
                ], check=True)

                begin_collecting = False
                fn_counts = []
                for l in annotate_invocation_output.splitlines(keepends=False):
                    if not begin_collecting and re.match(r"Ir\s+file:function", l):
                        begin_collecting = True
                        continue

                    count_match = re.match(r"^\s*([0-9,]+)\s+(.+:.+)$", l)
                    if count_match:
                        ir_str, file_function = count_match.groups()
                        ir = int(ir_str.replace(",", ""))
                        fn_counts.append(FunctionCount(ir, file_function))
                        continue

                    if begin_collecting and re.match(r"-+", l):
                        continue

                    begin_collecting = False

                return FunctionCounts(tuple(sorted(fn_counts, reverse=True)), inclusive=inclusive)
            return parse_output(inclusive=True), parse_output(inclusive=False)
        finally:
            shutil.rmtree(working_dir)

    @staticmethod
    def _construct_script(
        task_spec: common.TaskSpec,
        globals: GlobalsBridge,
        number: int,
        error_log: str,
        stat_log: str,
        bindings: Optional[ModuleType],
    ) -> str:
        # The naive template looks something like:
        #   "for _ in range({number}): {stmt}"
        # However a loop in Python is surprisingly expensive, and significantly
        # increases the number of background Python instructions. So instead we
        # partially unroll the loops, with a block size of 100 chosen to keep
        # the instruction overhead from `range` low while also not ballooning
        # the size of the generated file.
        block_size = 100
        loop_count = number // block_size
        remainder = number - block_size * loop_count
        blocked_stmt = ""
        if loop_count:
            unrolled_stmts = textwrap.indent("\n".join([task_spec.stmt] * block_size), " " * 4)
            blocked_stmt += f"for _ in range({loop_count}):\n{unrolled_stmts}\n"
        if remainder:
            blocked_stmt += "\n".join([task_spec.stmt] * remainder)

        return textwrap.dedent(r"""
            import gc
            import os
            import pickle
            import subprocess
            import sys
            import time

            # Mitigate https://github.com/pytorch/pytorch/issues/37377
            # which can sometimes cause the subprocess call to fail.
            import numpy as np

            import torch
            torch.set_num_threads({num_threads})

            {bindings_import}

            PID = os.getpid()

            def log_failure(msg):
                with open({error_log_repr}, "wt") as f:
                    f.write(msg)
                sys.exit(1)

            def check_result(completed_process):
                if completed_process.returncode:
                    log_failure(f"Command failed: {{' '.join(completed_process.args)}}")
                return completed_process

            # =============================================================================
            # == Check that subprocess matches parent =====================================
            # =============================================================================
            if sys.executable != "{parent_interpreter}":
                log_failure(
                    "Interpreter mismatch:\n"
                    f"  {{sys.executable}}\n    vs.\n  {parent_interpreter}"
                )

            if torch.__file__ != "{torch_file}":
                log_failure(
                    "PyTorch does not match expected file:\n"
                    f"  {{torch.__file__}}\n    vs.\n  {torch_file}"
                )

            # =============================================================================
            # == User specified setup =====================================================
            # =============================================================================
            # Load serialized globals
            {load_globals}

            # User setup str
            {setup}

            for _ in range({warmup_number}):
            {indented_stmt}

            # =============================================================================
            # == Callgrind management =====================================================
            # =============================================================================
            with open("{stat_log}", "wb") as stat_file:
                # If many instances of callgrind are running at once, the output of
                # `callgrind_control` may exceed 16kb which would cause `subprocess.PIPE`
                # to deadlock. So instead we use a file.
                callgrind_stat = check_result(subprocess.run(
                    ["callgrind_control", "--stat"],
                    stdout=stat_file,
                    stderr=subprocess.STDOUT,
                ))

            with open("{stat_log}", "rt") as stat_file:
                stat_lines = stat_file.read().splitlines()

            if f"PID {{PID}}: python {{__file__}}" not in stat_lines:
                log_failure("Process does not appear to be running callgrind.")

            gc.collect()
            time.sleep(0.01)

            # =============================================================================
            # == User code block ==========================================================
            # =============================================================================
            callgrind_bindings.valgrind_toggle()
            {blocked_stmt}

            # Sleep is to allow the interpreter to catch up before we stop collecting in
            # order to reduce jitter.
            time.sleep(0.01)
            callgrind_bindings.valgrind_toggle()
        """).strip().format(
            indented_stmt=textwrap.indent(task_spec.stmt, " " * 4),
            blocked_stmt=blocked_stmt,
            number=number,
            load_globals=globals.construct(),
            setup=task_spec.setup,
            warmup_number=min(number, 10),
            num_threads=task_spec.num_threads,
            error_log_repr=repr(error_log),
            stat_log=stat_log,
            parent_interpreter=sys.executable,
            torch_file=torch.__file__,
            bindings_import=(
                "import torch._C as callgrind_bindings" if bindings is None
                else f"import {bindings.__name__} as callgrind_bindings"),
        )


CALLGRIND_SINGLETON: Optional[_ValgrindWrapper] = None
def wrapper_singleton() -> _ValgrindWrapper:
    global CALLGRIND_SINGLETON
    if CALLGRIND_SINGLETON is None:
        CALLGRIND_SINGLETON = _ValgrindWrapper()
    return CALLGRIND_SINGLETON
