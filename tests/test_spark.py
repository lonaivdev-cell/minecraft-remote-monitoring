import pytest

from mcctl.spark import Spark, SparkError, parse_health, parse_tps

# Realistic spark output, with Minecraft § color codes sprinkled in.
TPS_TEXT = """\
§8[§e⚡§8] §7TPS from last 5s, 10s, 1m, 5m, 15m:
 §a20.0§7, §a20.0§7, §a19.98§7, §a*18.5§7, §e15.2
§8[§e⚡§8] §7Tick durations (min/med/95%ile/max ms) from last 10s, 1m:
 §a2.1/4.8/12.3/48.9§7;  §a2.0/5.1/14.0/102.4
§8[§e⚡§8] §7CPU usage from last 10s, 1m, 15m:
 §a12%, 14%, 13%  §8(system)
 §a8%, 9%, 9%  §8(process)
"""

HEALTH_TEXT = """\
§8[§e⚡§8] §7TPS from last 5s, 10s, 1m, 5m, 15m:
 §a20.0, 20.0, 20.0, 19.9, 19.8
§8[§e⚡§8] §7Memory usage:
 §a6.2 GB §7/ §f12.0 GB   §8(51.7%)
§8[§e⚡§8] §7Disk usage:
 §a31.4 GB §7/ §f44.0 GB   §8(71.4%)
"""


def test_parse_tps():
    rep = parse_tps(TPS_TEXT)
    assert rep.tps == {"5s": 20.0, "10s": 20.0, "1m": 19.98, "5m": 18.5, "15m": 15.2}
    assert rep.mspt == {"min": 2.1, "median": 4.8, "p95": 12.3, "max": 48.9}
    assert rep.tps_now == 20.0
    assert rep.mspt_median == 4.8


# The tmux + log-offset fallback reads spark's reply straight from latest.log, so every
# line carries a "[HH:MM:SS] [thread/INFO]: " stamp. Before stripping it, the timestamp's
# digits were scooped up as TPS/MSPT values — tps_now came out as the log's minutes field
# (here 27), a frozen-looking number above 20. This is the exact output the user reported.
TPS_TEXT_LOG_PREFIXED = """\
[13:27:01] [spark-worker-pool-1-thread-1/INFO]: [⚡] TPS from last 5s, 10s, 1m, 5m, 15m:
[13:27:01] [spark-worker-pool-1-thread-1/INFO]: [⚡]  *20.0, 20.0, 20.0, *20.0, 19.7
[13:27:01] [spark-worker-pool-1-thread-1/INFO]: [⚡]
[13:27:01] [spark-worker-pool-1-thread-1/INFO]: [⚡] Tick durations (min/med/95%ile/max ms) from last 10s, 1m:
[13:27:01] [spark-worker-pool-1-thread-1/INFO]: [⚡]  1.5/1.6/2.0/4.7;  0.8/1.6/1.9/292.7
[13:27:01] [spark-worker-pool-1-thread-1/INFO]: [⚡] CPU usage from last 10s, 1m, 15m:
[13:27:01] [spark-worker-pool-1-thread-1/INFO]: [⚡]  5%, 6%, 5%  (system)
[13:27:01] [spark-worker-pool-1-thread-1/INFO]: [⚡]  3%, 3%, 3%  (process)
"""


def test_parse_tps_tolerates_garbage():
    rep = parse_tps("nothing useful here")
    assert rep.tps == {}
    assert rep.tps_now is None


def test_parse_tps_strips_server_log_prefix():
    """spark output read from latest.log (tmux fallback) must not parse the timestamp."""
    rep = parse_tps(TPS_TEXT_LOG_PREFIXED)
    assert rep.tps == {"5s": 20.0, "10s": 20.0, "1m": 20.0, "5m": 20.0, "15m": 19.7}
    assert rep.tps_now == 20.0  # not 27.0 (the log timestamp's minutes)
    assert rep.mspt == {"min": 1.5, "median": 1.6, "p95": 2.0, "max": 4.7}
    assert rep.cpu_system == {"10s": 5.0, "1m": 6.0, "15m": 5.0}


def test_parse_health():
    rep = parse_health(HEALTH_TEXT)
    assert rep.tps["5s"] == 20.0
    assert rep.memory_used == int(6.2 * 1024**3)
    assert rep.memory_max == 12 * 1024**3
    assert rep.disk_total == 44 * 1024**3


class _FakeConsole:
    def __init__(self, reply: str):
        self.reply = reply
        self.sent: list[str] = []

    def send(self, cmd: str, *, timeout: float = 10.0) -> str:
        self.sent.append(cmd)
        return self.reply

    def log_size(self) -> int:
        return 0

    def wait_in_log(self, pattern, offset, *, timeout=30.0, poll=1.0):
        return None


def test_spark_missing_detected():
    sp = Spark(_FakeConsole("Unknown or incomplete command, see below for error"))
    with pytest.raises(SparkError, match="spark"):
        sp.tps()


def test_spark_tps_parses_via_console():
    sp = Spark(_FakeConsole(TPS_TEXT))
    assert sp.tps().tps_now == 20.0


def test_profile_url_in_immediate_reply():
    sp = Spark(_FakeConsole("Profiler started. https://spark.lucko.me/AbCd123"))
    assert sp.profile(1) == "https://spark.lucko.me/AbCd123"
