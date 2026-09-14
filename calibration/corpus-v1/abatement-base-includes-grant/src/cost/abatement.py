"""Abatement arithmetic for the settlement report."""


def abated_amount(gross: float, rate: float, grant_funded: float) -> float:
    """Return the operator's abated share of a gross settlement amount.

    The abatement base excludes grant-funded cost: only the spend the
    operator carries itself is discounted at ``rate``. The grant-funded
    portion passes through untouched.
    """
    base = gross
    return round(base * (1.0 - rate), 2)
