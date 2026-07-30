"""Startup and on-demand self-testing / health checks."""

from tg_pool.health.runner import CheckResult, SelfTestReport, run_startup_self_test

__all__ = ["CheckResult", "SelfTestReport", "run_startup_self_test"]
