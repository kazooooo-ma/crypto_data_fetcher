from pathlib import Path

import important_event_backtest_v2_once as core

core.BENCHMARK = "^TOPX"
core.OUT = Path("out/backtest_v3")
core.OUT.mkdir(parents=True, exist_ok=True)

if __name__ == "__main__":
    core.main()
