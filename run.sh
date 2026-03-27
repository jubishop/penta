#!/usr/bin/env bash
# Convenience launcher — always uses the Python that has penta installed.
# Usage: ./run.sh [directory]
exec python3 -m penta "$@"
