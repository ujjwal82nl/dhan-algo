# strategies/__init__.py
#
# Makes the strategies/ folder a Python package.
#
# All other files in the project (options_bot.py, csv_tracker.py, etc.)
# import from the TOP-LEVEL strategies.py, not from this package.
# This __init__.py is here so Python recognises the folder as a package,
# enabling strategy files inside it to do:
#
#   from strategies import OptionLeg, Trade   ← top-level module
#   from strategy_base import BaseStrategy    ← sibling inside this package
#
# No imports are needed here — each strategy file manages its own imports.
