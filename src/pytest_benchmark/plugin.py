from __future__ import division
from __future__ import print_function

import argparse
import gc
import json
import math
import platform
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime
from distutils.version import StrictVersion

import py
import pytest

from . import __version__
from .compat import XRANGE
from .stats import Stats
from .timers import compute_timer_precision
from .timers import default_timer
from .utils import NameWrapper
from .utils import SecondsDecimal
from .utils import cached_property
from .utils import clonefunc
from .utils import first_or_false
from .utils import format_dict
from .utils import get_commit_id
from .utils import get_commit_info
from .utils import get_current_time
from .utils import load_timer
from .utils import parse_compare_fail
from .utils import parse_rounds
from .utils import parse_save
from .utils import parse_seconds
from .utils import parse_sort
from .utils import parse_timer
from .utils import time_format
from .utils import time_unit

NUMBER_FMT = "{0:,.4f}" if sys.version_info[:2] > (2, 6) else "{0:.4f}"
ALIGNED_NUMBER_FMT = "{0:>{1},.4f}{2:>{3}}" if sys.version_info[:2] > (2, 6) else "{0:>{1}.4f}{2:>{3}}"
HISTOGRAM_CURRENT = "now"


class PerformanceRegression(Exception):
    pass


class MissingBenchmarkData(Exception):
    pass


def pytest_addoption(parser):
    group = parser.getgroup("benchmark")
    group.addoption(
        "--benchmark-min-time",
        metavar="SECONDS", type=parse_seconds, default="0.000025",
        help="Minimum time per round in seconds. Default: %(default)r"
    )
    group.addoption(
        "--benchmark-max-time",
        metavar="SECONDS", type=parse_seconds, default="1.0",
        help="Maximum time to spend in a benchmark in seconds. Default: %(default)r"
    )
    group.addoption(
        "--benchmark-min-rounds",
        metavar="NUM", type=parse_rounds, default=5,
        help="Minimum rounds, even if total time would exceed `--max-time`. Default: %(default)r"
    )
    group.addoption(
        "--benchmark-sort",
        metavar="COL", type=parse_sort, default="min",
        help="Column to sort on. Can be one of: 'min', 'max', 'mean' or 'stddev'. Default: %(default)r"
    )
    group.addoption(
        "--benchmark-group-by",
        metavar="LABEL", default="group",
        help="How to group tests. Can be one of: 'group', 'name', 'fullname', 'func', 'fullfunc' or 'param'."
             " Default: %(default)r"
    )
    group.addoption(
        "--benchmark-timer",
        metavar="FUNC", type=parse_timer, default=str(NameWrapper(default_timer)),
        help="Timer to use when measuring time. Default: %(default)r"
    )
    group.addoption(
        "--benchmark-warmup",
        action="store_true", default=False,
        help="Activates warmup. Will run the test function up to number of times in the calibration phase. "
             "See `--benchmark-warmup-iterations`. Note: Even the warmup phase obeys --benchmark-max-time."
    )
    group.addoption(
        "--benchmark-warmup-iterations",
        metavar="NUM", type=int, default=100000,
        help="Max number of iterations to run in the warmup phase. Default: %(default)r"
    )
    group.addoption(
        "--benchmark-verbose",
        action="store_true", default=False,
        help="Dump diagnostic and progress information."
    )
    group.addoption(
        "--benchmark-disable-gc",
        action="store_true", default=False,
        help="Disable GC during benchmarks."
    )
    group.addoption(
        "--benchmark-skip",
        action="store_true", default=False,
        help="Skip running any benchmarks."
    )
    group.addoption(
        "--benchmark-only",
        action="store_true", default=False,
        help="Only run benchmarks."
    )
    commit_id = get_commit_id()
    group.addoption(
        "--benchmark-save",
        action='append', metavar="NAME", nargs="?", default=[], const=commit_id, type=parse_save,
        help="Save the current run into 'STORAGE-PATH/counter-NAME.json'. Default: %r" % commit_id
    )
    group.addoption(
        "--benchmark-autosave",
        action="store_true",
        help="Autosave the current run into 'STORAGE-PATH/counter-commit_id.json",
    )
    group.addoption(
        "--benchmark-save-data",
        action="store_true",
        help="Use this to make --benchmark-save and --benchmark-autosave include all the timing data,"
             " not just the stats.",
    )
    group.addoption(
        "--benchmark-compare",
        metavar="NUM", nargs="?", default=[], const=True,
        help="Compare the current run against run NUM or the latest saved run if unspecified."
    )
    group.addoption(
        "--benchmark-compare-fail",
        metavar="EXPR", nargs="+", type=parse_compare_fail,
        help="Fail test if performance regresses according to given EXPR"
             " (eg: min:5%% or mean:0.001 for number of seconds). Can be used multiple times."
    )
    group.addoption(
        "--benchmark-storage",
        metavar="STORAGE-PATH", default="./.benchmarks/%s-%s-%s-%s" % (
            platform.system(),
            platform.python_implementation(),
            ".".join(platform.python_version_tuple()[:2]),
            platform.architecture()[0]
        ),
        help="Specify a different path to store the runs (when --benchmark-save or --benchmark-autosave are used). "
             "Default: %(default)r",
    )
    prefix = "benchmark_%s" % get_current_time()
    group.addoption(
        "--benchmark-histogram",
        action='append', metavar="FILENAME-PREFIX", nargs="?", default=[], const=prefix,
        help="Plot graphs of min/max/avg/stddev over time in FILENAME-PREFIX-test_name.svg. Default: %r" % prefix
    )
    group.addoption(
        "--benchmark-json",
        metavar="PATH", type=argparse.FileType('wb'),
        help="Dump a JSON report into PATH. "
             "Note that this will include the complete data (all the timings, not just the stats)."
    )


def pytest_addhooks(pluginmanager):
    from . import hookspec

    method = getattr(pluginmanager, "add_hookspecs", None)
    if method is None:
        method = pluginmanager.addhooks
    method(hookspec)


class BenchmarkStats(object):
    def __init__(self, fixture, iterations, options):
        self.name = fixture.name
        self.fullname = fixture.fullname
        self.group = fixture.group
        self.param = fixture.param

        self.iterations = iterations
        self.stats = Stats()
        self.options = options

    def __getitem__(self, key):
        try:
            return getattr(self.stats, key)
        except AttributeError:
            return getattr(self, key)

    def json(self, include_data=True):
        out = dict(
            (field, getattr(self.stats, field))
            for field in self.stats.fields
        )
        if include_data:
            out['data'] = self.stats.data
        return out

    def update(self, duration):
        self.stats.update(duration / self.iterations)


class BenchmarkFixture(object):
    _precisions = {}

    @classmethod
    def _get_precision(cls, timer):
        if timer in cls._precisions:
            return cls._precisions[timer]
        else:
            return cls._precisions.setdefault(timer, compute_timer_precision(timer))

    def __init__(self, node, disable_gc, timer, min_rounds, min_time, max_time, warmup, warmup_iterations,
                 add_stats, logger, group=None):
        self.name = node.name
        self.fullname = node._nodeid
        self.param = node.callspec.id if hasattr(node, 'callspec') else None
        self.group = group

        self._disable_gc = disable_gc
        self._timer = timer.target
        self._min_rounds = min_rounds
        self._max_time = float(max_time)
        self._min_time = float(min_time)
        self._add_stats = add_stats
        self._warmup = warmup and warmup_iterations
        self._logger = logger
        self._cleanup_callbacks = []

    def __call__(self, function_to_benchmark, *args, **kwargs):
        if platform.python_implementation() == "PyPy":
            # This was discussed with Antonio Cuni.
            # Cloning the function should help if you run the same
            # function with different arguments over multiple tests.
            function_to_benchmark = clonefunc(function_to_benchmark)

        def runner(loops_range, timer=self._timer):
            gcenabled = gc.isenabled()
            if self._disable_gc:
                gc.disable()
            tracer = sys.gettrace()
            sys.settrace(None)
            start = timer()
            for _ in loops_range:
                function_to_benchmark(*args, **kwargs)
            end = timer()
            sys.settrace(tracer)
            if gcenabled:
                gc.enable()
            return end - start

        duration, iterations, loops_range = self._calibrate_timer(runner)

        # Choose how many time we must repeat the test
        rounds = int(math.ceil(self._max_time / duration))
        rounds = max(rounds, self._min_rounds)

        stats = BenchmarkStats(self, iterations=iterations, options={
            "disable_gc": self._disable_gc,
            "timer": self._timer,
            "min_rounds": self._min_rounds,
            "max_time": self._max_time,
            "min_time": self._min_time,
            "warmup": self._warmup,
        })
        self._add_stats(stats)

        self._logger.debug("  Running %s rounds x %s iterations ..." % (rounds, iterations), yellow=True, bold=True)
        run_start = time.time()
        if self._warmup:
            warmup_rounds = min(rounds, max(1, int(self._warmup / iterations)))
            self._logger.debug("  Warmup %s rounds x %s iterations ..." % (warmup_rounds, iterations))
            for _ in XRANGE(warmup_rounds):
                runner(loops_range)
        for _ in XRANGE(rounds):
            stats.update(runner(loops_range))
        self._logger.debug("  Ran for %ss." % time_format(time.time() - run_start), yellow=True, bold=True)

        return function_to_benchmark(*args, **kwargs)

    def weave(self, target, **kwargs):
        try:
            import aspectlib
        except ImportError as exc:
            raise ImportError(exc.args, "Please install aspectlib or pytest-benchmark[aspect]")

        def aspect(function):
            def wrapper(*args, **kwargs):
                return self(function, *args, **kwargs)

            return wrapper

        self._cleanup_callbacks.append(aspectlib.weave(target, aspect, **kwargs).rollback)

    patch = weave

    def _cleanup(self):
        while self._cleanup_callbacks:
            callback = self._cleanup_callbacks.pop()
            callback()

    def _calibrate_timer(self, runner):
        timer_precision = self._get_precision(self._timer)
        min_time = max(self._min_time, timer_precision * 100)
        min_time_estimate = min_time / 10
        self._logger.debug("")
        self._logger.debug("  Timer precision: %ss" % time_format(timer_precision), yellow=True, bold=True)
        self._logger.debug("  Calibrating to target round %ss; will estimate when reaching %ss." % (
            time_format(min_time), time_format(min_time_estimate)), yellow=True, bold=True)

        loops = 1
        while True:
            loops_range = XRANGE(loops)
            duration = runner(loops_range)
            if self._warmup:
                warmup_start = time.time()
                warmup_iterations = 0
                warmup_rounds = 0
                while time.time() - warmup_start < self._max_time and warmup_iterations < self._warmup:
                    duration = min(duration, runner(loops_range))
                    warmup_rounds += 1
                    warmup_iterations += loops
                self._logger.debug("    Warmup: %ss (%s x %s iterations)." % (
                    time_format(time.time() - warmup_start),
                    warmup_rounds, loops
                ))

            self._logger.debug("    Measured %s iterations: %ss." % (loops, time_format(duration)), yellow=True)
            if duration / min_time >= 0.75:
                break

            if duration >= min_time_estimate:
                # coarse estimation of the number of loops
                loops = int(round(min_time * loops / duration) + 1)
                self._logger.debug("    Estimating %s iterations." % loops, green=True)
                if loops == 1:
                    # If we got a single loop then bail early - nothing to calibrate if the the
                    # test function is 100 times slower than the timer resolution.
                    loops_range = XRANGE(loops)
                    break
            else:
                loops *= 10
        return duration, loops, loops_range


class Logger(object):
    def __init__(self, verbose, capman):
        if capman:
            capman.suspendcapture(in_=True)
        self.verbose = verbose
        self.term = py.io.TerminalWriter(file=sys.stderr)
        if capman:
            capman.resumecapture()
        self.capman = capman

    def warn(self, text):
        if self.capman:
            self.capman.suspendcapture(in_=True)
        self.term.sep("-", red=True, bold=True)
        self.term.write(" WARNING: ", red=True, bold=True)
        self.term.line(text, red=True)
        self.term.sep("-", red=True, bold=True)
        if self.capman:
            self.capman.resumecapture()

    def error(self, text):
        if self.capman:
            self.capman.suspendcapture(in_=True)
        self.term.sep("-", red=True, bold=True)
        self.term.line(text, red=True, bold=True)
        self.term.sep("-", red=True, bold=True)
        if self.capman:
            self.capman.resumecapture()

    def info(self, text, **kwargs):
        if self.capman:
            self.capman.suspendcapture(in_=True)
        if not kwargs or kwargs == {'bold': True}:
            kwargs['purple'] = True
        self.term.line(text, **kwargs)
        if self.capman:
            self.capman.resumecapture()

    def debug(self, text, **kwargs):
        if self.verbose:
            self.info(text, **kwargs)


class BenchmarkSession(object):
    compare_by_fullname = None
    compare_by_name = None

    def __init__(self, config):
        self.verbose = config.getoption("benchmark_verbose")
        self.logger = Logger(
            self.verbose,
            config.pluginmanager.getplugin("capturemanager")
        )
        self.config = config
        self.options = dict(
            min_time=SecondsDecimal(config.getoption("benchmark_min_time")),
            min_rounds=config.getoption("benchmark_min_rounds"),
            max_time=SecondsDecimal(config.getoption("benchmark_max_time")),
            timer=load_timer(config.getoption("benchmark_timer")),
            disable_gc=config.getoption("benchmark_disable_gc"),
            warmup=config.getoption("benchmark_warmup"),
            warmup_iterations=config.getoption("benchmark_warmup_iterations"),
        )
        self.skip = config.getoption("benchmark_skip")
        if config.getoption("dist", "no") != "no" and not self.skip:
            self.logger.warn(
                "Benchmarks are automatically skipped because xdist plugin is active."
                "Benchmarks cannot be performed reliably in a parallelized environment.",
            )
            self.skip = True
        if hasattr(config, "slaveinput"):
            self.skip = True
        self.only = config.getoption("benchmark_only")
        self.sort = config.getoption("benchmark_sort")
        if self.skip and self.only:
            raise pytest.UsageError("Can't have both --benchmark-only and --benchmark-skip options.")
        self.benchmarks = []
        self.group_by = config.getoption("benchmark_group_by")
        self.save = first_or_false(config.getoption("benchmark_save"))
        self.autosave = config.getoption("benchmark_autosave")
        self.save_data = config.getoption("benchmark_save_data")
        self.json = config.getoption("benchmark_json")

        self.compare = config.getoption("benchmark_compare")
        self.compare_fail = config.getoption("benchmark_compare_fail")
        self.performance_regressions = []
        if self.compare_fail and not self.compare_file:
            raise pytest.UsageError("--benchmark-compare-fail requires valid --benchmark-compare.")

        self.storage = py.path.local(config.getoption("benchmark_storage"))
        self.storage.ensure(dir=1)

        self.histogram = first_or_false(config.getoption("benchmark_histogram"))

    @cached_property
    def compare_file(self):
        if self.compare:
            files = self.storage.listdir("[0-9][0-9][0-9][0-9]_*.json", sort=True)
            if not files:
                self.logger.warn("No benchmark files in %r. Expected files matching [0-9][0-9][0-9][0-9]_*.json" % self.storage)

            if self.compare is True:
                files.sort()
                return files[-1]
            else:
                files = [f for f in files if str(f.basename).startswith(self.compare)]
                if len(files) == 1:
                    return files[0]

                if not files:
                    self.logger.warn("No benchmark files matched %r" % self.compare)
                elif len(files) > 1:
                    self.logger.warn("Too many benchmark files matched %r: %s" % (self.compare, files))

    @property
    def next_num(self):
        files = self.storage.listdir("[0-9][0-9][0-9][0-9]_*.json")
        files.sort(reverse=True)
        if not files:
            return "0001"
        for f in files:
            try:
                return "%04i" % (int(str(f.basename).split('_')[0]) + 1)
            except ValueError:
                raise
        return "0001"

    def handle_saving(self):
        if self.json:
            output_json = self.config.hook.pytest_benchmark_generate_json(
                config=self.config,
                benchmarks=self.benchmarks,
                include_data=True
            )
            self.config.hook.pytest_benchmark_update_json(
                config=self.config,
                benchmarks=self.benchmarks,
                output_json=output_json
            )
            with self.json as fh:
                fh.write(json.dumps(output_json, ensure_ascii=True, indent=4).encode())
            self.logger.info("Wrote benchmark data in %s" % self.json, purple=True)

        if self.save or self.autosave:
            output_json = self.config.hook.pytest_benchmark_generate_json(
                config=self.config,
                benchmarks=self.benchmarks,
                include_data=self.save_data
            )
            self.config.hook.pytest_benchmark_update_json(
                config=self.config,
                benchmarks=self.benchmarks,
                output_json=output_json
            )
            output_file = None
            if self.save:
                output_file = self.storage.join("%s_%s.json" % (self.next_num, self.save))
                assert not output_file.exists()
            elif self.autosave:
                output_file = self.storage.join("%s_%s.json" % (self.next_num, get_commit_id()))
                assert not output_file.exists()

            if output_file:
                with output_file.open('wb') as fh:
                    fh.write(json.dumps(output_json, ensure_ascii=True, indent=4).encode())
                self.logger.info("Saved benchmark data in %s" % output_file)

    def handle_loading(self):
        if self.compare_file:
            with self.compare_file.open('rU') as fh:
                try:
                    compared_benchmark = json.load(fh)
                except Exception as exc:
                    self.logger.warn("Failed to load %s: %s" % (self.compare_file, exc))
                    return

            if 'version' in compared_benchmark:
                if StrictVersion(compared_benchmark['version']) > StrictVersion(__version__):
                    self.logger.warn(
                        "Benchmark data from %s was saved with a newer version (%s) than the current version (%s)." % (
                            self.compare_file,
                            compared_benchmark['version'],
                            __version__,
                        )
                    )
            machine_info = self.config.hook.pytest_benchmark_generate_machine_info(config=self.config)
            self.config.hook.pytest_benchmark_update_machine_info(config=self.config, machine_info=machine_info)
            self.config.hook.pytest_benchmark_compare_machine_info(config=self.config, benchmarksession=self,
                                                                   machine_info=machine_info,
                                                                   compared_benchmark=compared_benchmark)
            # self.compare_by_name = dict((bench['name'], bench) for bench in compared_benchmark['benchmarks'])
            self.compare_by_fullname = dict((bench['fullname'], bench) for bench in compared_benchmark['benchmarks'])

            self.logger.info("Comparing against benchmark %s:" % self.compare_file.basename, bold=True)
            self.logger.info("| commit info: %s" % format_dict(compared_benchmark['commit_info']))
            self.logger.info("| saved at: %s" % compared_benchmark['datetime'])
            self.logger.info("| saved using pytest-benchmark %s:" % compared_benchmark['version'])

    def display(self, tr):
        if not self.benchmarks:
            return

        tr.ensure_newline()
        self.handle_saving()
        self.handle_loading()
        self.display_results_table(tr)
        self.check_regressions()
        self.handle_histogram()

    def check_regressions(self):
        if self.performance_regressions:
            self.logger.error("Performance has regressed:\n%s" % "\n".join(
                "\t%s - %s" % line for line in self.performance_regressions
            ))
            raise PerformanceRegression("Performance has regressed.")

    def handle_histogram(self):
        if self.histogram:
            from .histogram import make_plot

            history = {}
            for bench_file in self.storage.listdir("[0-9][0-9][0-9][0-9]_*.json"):
                with bench_file.open('rU') as fh:
                    fullname = bench_file.purebasename
                    if '_' in fullname:
                        id_, name = fullname.split('_', 1)
                    else:
                        id_, name = fullname, ''
                    data = history[id_] = json.load(fh)
                    data['name'] = name
                    # data['by_name'] = dict((bench['name'], bench) for bench in data['benchmarks'])
                    data['by_fullname'] = dict((bench['fullname'], bench) for bench in data['benchmarks'])

            for bench in self.benchmarks:
                name = bench.fullname
                for c in "\/:*?<>|":
                    name = name.replace(c, '_').replace('__', '_')
                output_file = py.path.local("%s-%s.svg" % (self.histogram, name))

                table = list(self.generate_histogram_table(bench, history, sorted(history)))

                plot = make_plot(
                    bench_name=bench.fullname,
                    table=table,
                    compare=self.compare_file,
                    annotations=dict((k, v['name']) for k, v in history.items()),
                    sort=self.sort,
                    current=HISTOGRAM_CURRENT,
                )
                plot.render_to_file(str(output_file))
                self.logger.info("Generated histogram %s" % output_file, bold=True)

    @staticmethod
    def generate_histogram_table(current, history, sequence):
        for name in sequence:
            trial = history[name]
            for bench in trial['benchmarks']:
                if bench['fullname'] == current.fullname:
                    found = True
                # elif bench['name'] == current.name:
                #     found = True
                else:
                    found = False

                if found:
                    yield '%s' % name, trial['datetime'], bench['stats']
                    break

        yield HISTOGRAM_CURRENT, "", current.json()

    def display_results_table(self, tr):
        timer = self.options.get('timer')
        for group, benchmarks in self.config.hook.pytest_benchmark_group_stats(
                config=self.config,
                benchmarks=self.benchmarks,
                group_by=self.group_by
        ):
            worst = {}
            best = {}
            if len(benchmarks) > 1:
                for prop in "min", "max", "mean", "stddev":
                    worst[prop] = max(bench[prop] for bench in benchmarks)
                    best[prop] = min(bench[prop] for bench in benchmarks)
            for prop in "outliers", "rounds", "iterations":
                worst[prop] = max(benchmark[prop] for benchmark in benchmarks)

            unit, adjustment = time_unit(best.get(self.sort, benchmarks[0][self.sort]))
            labels = {
                "name": "Name (time in %ss)" % unit,
                "min": "Min",
                "max": "Max",
                "mean": "Mean",
                "stddev": "StdDev",
                "rounds": "Rounds",
                "iterations": "Iterations",
                "iqr": "IQR",
                "median": "Median",
                "outliers": "Outliers(*)",
            }
            widths = {
                "name": 3 + max(len(labels["name"]), max(len(benchmark.name) for benchmark in benchmarks)),
                "rounds": 2 + max(len(labels["rounds"]), len(str(worst["rounds"]))),
                "iterations": 2 + max(len(labels["iterations"]), len(str(worst["iterations"]))),
                "outliers": 2 + max(len(labels["outliers"]), len(str(worst["outliers"]))),
            }
            for prop in "min", "max", "mean", "stddev", "median", "iqr":
                widths[prop] = 2 + max(len(labels[prop]), max(
                    len(NUMBER_FMT.format(bench[prop] * adjustment))
                    for bench in benchmarks
                ))

            rpadding = 8 if self.compare_file else 0
            labels_line = labels["name"].ljust(widths["name"]) + "".join(
                labels[prop].rjust(widths[prop]) + (
                    " " * rpadding
                    if prop not in ["outliers", "rounds", "iterations"]
                    else ""
                )
                for prop in ("min", "max", "mean", "stddev", "median", "iqr", "outliers", "rounds", "iterations")
            )
            tr.write_line(
                (" benchmark%(name)s: %(count)s tests, min %(min_rounds)s rounds (of min %(min_time)s),"
                 " %(max_time)s max time, timer: %(timer)s " % dict(
                     self.options,
                     count=len(benchmarks),
                     name="" if group is None else " %r" % group,
                     timer=timer
                 )).center(len(labels_line), '-'),
                yellow=True,
            )
            tr.write_line(labels_line)
            tr.write_line("-" * len(labels_line), yellow=True)

            for bench in benchmarks:
                tr.write(bench.name.ljust(widths["name"]))
                for prop in "min", "max", "mean", "stddev", "median", "iqr":
                    tr.write(
                        ALIGNED_NUMBER_FMT.format(bench[prop] * adjustment, widths[prop], "", rpadding),
                        green=bench[prop] == best.get(prop),
                        red=bench[prop] == worst.get(prop),
                        bold=True,
                    )
                for prop in "outliers", "rounds", "iterations":
                    tr.write("{0:>{1}}".format(bench[prop], widths[prop]))
                tr.write("\n")
                if self.compare_file:
                    if bench.fullname in self.compare_by_fullname:
                        self.display_compare_row(tr, widths, adjustment, bench,
                                                 self.compare_by_fullname[bench.fullname])
                    # elif bench.name in self.compare_by_name:
                    #     self.display_compare_row(tr, widths, adjustment, bench, self.compare_by_name[bench.name])

            tr.write_line("-" * len(labels_line), yellow=True)
            tr.write_line("(*) Outliers: 1 Standard Deviation from Mean; "
                          "1.5 IQR (InterQuartile Range) from 1st Quartile and 3rd Quartile.", bold=True, black=True)
            tr.write_line("")

    def display_compare_row(self, tr, widths, adjustment, bench, compare_to):
        stats = compare_to['stats']

        if self.compare_fail:
            for check in self.compare_fail:
                fail = check.fails(bench, stats)
                if fail:
                    self.performance_regressions.append((bench.fullname, fail))

        tr.write("".ljust(widths["name"]))
        for prop in "min", "max", "mean", "stddev", "median", "iqr":
            new = bench[prop]
            old = stats[prop]
            val = new - old
            fmt = NUMBER_FMT.format(abs(val * adjustment))
            if val > 0:
                tr.write(
                    "{0:>{1}} {2:<7}".format("+" + fmt, widths[prop],
                                             "(%i%%)" % abs(new / old * 100 - 100) if old else 'inf'),
                    red=True
                )
            elif val < 0:
                tr.write(
                    "{0:>{1}} {2:<7}".format("-" + fmt, widths[prop],
                                             "(%i%%)" % abs(new / old * 100 - 100) if old else 'inf'),
                    green=True
                )
            else:
                tr.write("{0:>{1}}        ".format("NC", widths[prop]), bold=True, black=True)

        for prop in "outliers", "rounds", "iterations":
            tr.write("{0:>{1}}".format(stats[prop], widths[prop]))
        tr.write("\n")


def pytest_benchmark_compare_machine_info(config, benchmarksession, machine_info, compared_benchmark):
    if compared_benchmark['machine_info'] != machine_info:
        benchmarksession.logger.warn(
            "Benchmark machine_info is different. Current: %s VS saved: %s." % (
                format_dict(machine_info),
                format_dict(compared_benchmark['machine_info']),
            )
        )


def pytest_runtest_call(item, __multicall__):
    bs = item.config._benchmarksession

    fixure = hasattr(item, "funcargs") and item.funcargs.get("benchmark")
    if isinstance(fixure, BenchmarkFixture):
        if bs.skip:
            pytest.skip("Skipping benchmark (--benchmark-skip active).")
        else:
            __multicall__.execute()
    else:
        if bs.only:
            pytest.skip("Skipping non-benchmark (--benchmark-only active).")
        else:
            __multicall__.execute()


def pytest_benchmark_group_stats(config, benchmarks, group_by):
    groups = defaultdict(list)
    for bench in benchmarks:
        if group_by == 'group':
            groups[bench.group].append(bench)
        elif group_by == 'name':
            groups[bench.name].append(bench)
        elif group_by == 'func':
            groups[bench.name.split('[')[0]].append(bench)
        elif group_by == 'fullfunc':
            groups[bench.fullname.split('[')[0]].append(bench)
        elif group_by == 'fullname':
            groups[bench.fullname].append(bench)
        elif group_by == 'param':
            groups[bench.param].append(bench)
        else:
            raise NotImplementedError("Unsupported grouping %r." % group_by)
    return sorted(groups.items(), key=lambda pair: pair[0] or "")


def pytest_terminal_summary(terminalreporter):
    try:
        terminalreporter.config._benchmarksession.display(terminalreporter)
    except PerformanceRegression:
        raise
    except Exception:
        terminalreporter.config._benchmarksession.logger.error("\n%s" % traceback.format_exc())
        raise


def pytest_benchmark_generate_machine_info():
    return {
        "node": platform.node(),
        "processor": platform.processor(),
        "machine": platform.machine(),
        "python_compiler": platform.python_compiler(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "release": platform.release(),
        "system": platform.system()
    }


def pytest_benchmark_generate_commit_info(config):
    return get_commit_info()


def pytest_benchmark_generate_json(config, benchmarks, include_data):
    machine_info = config.hook.pytest_benchmark_generate_machine_info(config=config)
    config.hook.pytest_benchmark_update_machine_info(config=config, machine_info=machine_info)

    commit_info = config.hook.pytest_benchmark_generate_commit_info(config=config)
    config.hook.pytest_benchmark_update_commit_info(config=config, commit_info=commit_info)

    benchmarks_json = []
    output_json = {
        'machine_info': machine_info,
        'commit_info': commit_info,
        'benchmarks': benchmarks_json,
        'datetime': datetime.utcnow().isoformat(),
        'version': __version__,
    }
    for bench in benchmarks:
        benchmarks_json.append({
            'group': bench.group,
            'name': bench.name,
            'fullname': bench.fullname,
            'stats': dict(bench.json(include_data=include_data), iterations=bench.iterations),
            'options': dict(
                (k, v.__name__ if callable(v) else v) for k, v in bench.options.items()
            )
        })
    return output_json


@pytest.fixture(scope="function")
def benchmark(request):
    bs = request.config._benchmarksession

    if bs.skip:
        pytest.skip("Benchmarks are disabled.")
    else:
        node = request.node
        marker = node.get_marker("benchmark")
        options = marker.kwargs if marker else {}
        if 'timer' in options:
            options['timer'] = NameWrapper(options['timer'])
        fixture = BenchmarkFixture(
            node,
            add_stats=bs.benchmarks.append,
            logger=bs.logger,
            **dict(bs.options, **options)
        )
        request.addfinalizer(fixture._cleanup)
        return fixture


@pytest.fixture(scope="function")
def benchmark_weave(benchmark):
    return benchmark.weave


def pytest_runtest_setup(item):
    marker = item.get_marker("benchmark")
    if marker:
        if marker.args:
            raise ValueError("benchmark mark can't have positional arguments.")
        for name in marker.kwargs:
            if name not in (
                    "max_time", "min_rounds", "min_time", "timer", "group", "disable_gc", "warmup",
                    "warmup_iterations"):
                raise ValueError("benchmark mark can't have %r keyword argument." % name)


def pytest_configure(config, __multicall__):
    __multicall__.execute()  # force the other plugins to initialise
    config.addinivalue_line("markers", "benchmark: mark a test with custom benchmark settings.")
    config._benchmarksession = BenchmarkSession(config)
    config.pluginmanager.register(config._benchmarksession, "pytest-benchmark")
