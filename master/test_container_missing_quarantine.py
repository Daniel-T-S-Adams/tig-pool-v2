"""Container-not-found is a boot race, not a dead box."""

from master.infra_errors import is_container_missing_error, is_infrastructure_error


SAT_MSG = (
    "Challenge container satisfiability not found. "
    "Did you start it with 'docker-compose up satisfiability'?"
)
ENERGY_MSG = (
    "Challenge container energy_arbitrage not found. "
    "Did you start it with 'docker-compose up energy_arbitrage'?"
)


def test_container_missing_is_not_quarantine():
    assert is_container_missing_error(SAT_MSG) is True
    assert is_container_missing_error(ENERGY_MSG) is True
    assert is_infrastructure_error(SAT_MSG) is False
    assert is_infrastructure_error(ENERGY_MSG) is False


def test_real_infra_still_quarantines():
    assert is_infrastructure_error("cannot open shared object file: libfoo.so") is True
    assert is_infrastructure_error("Permission denied") is True
    assert is_container_missing_error("cannot open shared object file") is False
