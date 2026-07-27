"""Executable conformance checks for Labwire protocol servers.

Point the runner at ANY server, ours or an independent implementation, and
get a pass/fail report with spec section references (SPEC 15):

Example:
    >>> # from labwire.conformance import RunOptions, run_suite
    >>> # report = await run_suite("ws://127.0.0.1:9500", RunOptions())
    >>> # print(report.render()); print(report.verdict())
"""

from labwire.conformance._checks import CHECKS, Check, CheckContext, RunOptions
from labwire.conformance._report import LEVELS, CheckOutcome, Report, Status
from labwire.conformance.runner import run_suite

__all__ = [
    "CHECKS",
    "LEVELS",
    "Check",
    "CheckContext",
    "CheckOutcome",
    "Report",
    "RunOptions",
    "Status",
    "run_suite",
]
