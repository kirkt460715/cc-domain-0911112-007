#!/bin/sh
set -eu
python3 -m compileall -q energyflow
python3 -m unittest discover -s tests -v
python3 -m energyflow demo
python3 -m synthetic_exchange.smoke
