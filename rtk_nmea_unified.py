#!/usr/bin/env python3
"""QLM29H entry point; receiver-specific setup and Ntrip live in shared runtime."""
from gnss_telemetry.app import run
if __name__=='__main__':raise SystemExit(run('RTK'))
