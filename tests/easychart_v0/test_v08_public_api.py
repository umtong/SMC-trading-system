from __future__ import annotations

import ictbt.easychart_v0 as easychart


def test_v08_research_api_is_exported() -> None:
    required = (
        "V08Policy",
        "V08IntradayPolicy",
        "GrowthGate",
        "PortfolioContext",
        "run_global_portfolio",
        "sample_trials",
    )

    assert all(hasattr(easychart, name) for name in required)
