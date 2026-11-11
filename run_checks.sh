#!/bin/sh
set -eu
python3 -m compileall -q energyflow synthetic_exchange
python3 -m unittest discover -s tests -v
python3 -m energyflow demo
python3 -m energyflow harvest-drill
python3 -m synthetic_exchange.smoke
