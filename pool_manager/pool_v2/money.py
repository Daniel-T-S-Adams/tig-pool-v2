"""Exact token-unit and collateral arithmetic; never use binary floats."""

from decimal import Decimal, InvalidOperation
from fractions import Fraction


TOKEN_DECIMALS = 18
TIG = 10 ** TOKEN_DECIMALS


class FundsError(ValueError):
    pass


class Conflict(FundsError):
    pass


class InsufficientFunds(FundsError):
    pass


def units(value, *, positive=False):
    if type(value) is not int or value < int(positive):
        raise FundsError("amount must be an integer number of token units")
    return value


def multiplier(value):
    if not isinstance(value, (str, Decimal)):
        raise FundsError("multiplier must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise FundsError("invalid multiplier") from exc
    if not result.is_finite() or not 0 <= result <= 1:
        raise FundsError("multiplier must be between 0 and 1")
    # Bound input size without rounding a valid supplied value.
    if len(result.as_tuple().digits) > 78 or result.as_tuple().exponent < -78:
        raise FundsError("multiplier supports at most 78 decimal places/digits")
    return result


def collateral(track_bundle_counts, value, decimals=TOKEN_DECIMALS):
    counts = list(track_bundle_counts)
    if not counts or any(type(count) is not int or count <= 0 for count in counts):
        raise FundsError("each proposed track requires a positive bundle count")
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise FundsError("invalid token decimals")
    base = 10 * 10 ** decimals * max(counts)
    exact = base * Fraction(multiplier(value))
    return base, -(-exact.numerator // exact.denominator)


def allocate_rewards(pot, credits):
    """5% fee, then largest remainders; stable member-ID order resolves ties."""
    units(pot)
    if any(not isinstance(key, str) or not key for key in credits):
        raise FundsError("member IDs must be nonempty strings")
    if any(type(value) not in (int, Fraction) or value < 0 for value in credits.values()):
        raise FundsError("credit must be an exact nonnegative fraction")
    total = sum(credits.values(), Fraction())
    if not total:
        return pot, {key: 0 for key in credits}
    fee = pot * 5 // 100
    exact = {key: (pot - fee) * Fraction(value) / total for key, value in credits.items()}
    result = {key: value.numerator // value.denominator for key, value in exact.items()}
    remaining = pot - fee - sum(result.values())
    order = sorted(exact, key=lambda key: (-(exact[key] - result[key]), key))
    for key in order[:remaining]:
        result[key] += 1
    return fee, result
