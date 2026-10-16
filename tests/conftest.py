import pytest

from video2mdnotes.config import settings


@pytest.fixture(autouse=True)
def _isolated_cost_ledger(tmp_path):
    """Every test gets its own cost ledger.

    Without this, any test that mocks a metered litellm call would append to
    the real previous_run_results/_cost_ledger.json and count fake spend
    against the operator's real budget cap.
    """
    saved = (settings.cost_ledger_path, settings.metered_usd_cap,
             dict(settings.provider_usd_caps), settings.allow_metered_fallback,
             settings.allow_anthropic_api, settings.run_usd_cap, settings.run_id)
    settings.cost_ledger_path = tmp_path / "_cost_ledger.json"
    yield settings.cost_ledger_path
    (settings.cost_ledger_path, settings.metered_usd_cap,
     settings.provider_usd_caps, settings.allow_metered_fallback,
     settings.allow_anthropic_api, settings.run_usd_cap, settings.run_id) = saved
