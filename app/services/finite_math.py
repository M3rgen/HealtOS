"""Finite read-model arithmetic; retry overflowing intermediates without rounding bounds."""

from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction
from math import isfinite
from sys import float_info

from app.parsers.common import number


def finite_number(value) -> float | None:
    parsed = number(value)
    if parsed is None or not parsed.is_finite():
        return None
    result = float(parsed)
    return result if isfinite(result) and (result != 0 or parsed == 0) else None


def finite_calculation(value: float, exact: Callable[[], Fraction]) -> float | None:
    """Keep ordinary float results; only retry nonfinite or possibly underflowed results.

    Fractions represent the finite input floats exactly. The retry covers the whole
    expression, so intermediate overflow does not hide a representable final result.
    Only the final conversion can raise OverflowError; no reader exception is swallowed.
    """
    if isfinite(value) and value != 0:
        return value
    precise = exact()
    try:
        result = float(precise)
    except OverflowError:
        return None
    return result if isfinite(result) and (result != 0 or precise == 0) else None


def finite_scale(value: float | Decimal, multiplier: float, divisor: float = 1) -> float | None:
    """Keep source decimals until the exact retry, including the unit conversion.

    A source value may exceed float's range while its scaled result still fits.
    Ordinary finite float arithmetic keeps the existing rounding behavior.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value == 0 or multiplier == 0:
            return 0.0 / divisor
        # A finite float shifts the decimal exponent by less than this range.
        # Beyond three ranges, even two scaling operands cannot rescue the result.
        # Reject it before Fraction materializes an enormous power of ten.
        if abs(value.adjusted()) > 3 * (float_info.max_10_exp - float_info.min_10_exp):
            return None
    return finite_calculation(
        float(value) * multiplier / divisor,
        lambda: Fraction(value) * Fraction(multiplier) / Fraction(divisor),
    )


def finite_difference(left: float, right: float) -> float | None:
    return finite_calculation(left - right, lambda: Fraction(left) - Fraction(right))


def finite_sum(values: list[float]) -> float | None:
    return finite_calculation(sum(values), lambda: sum(map(Fraction, values), Fraction()))


def finite_scaled_sum(terms: list[tuple[float, float, float]]) -> float | None:
    """Sum proportions, retrying the entire expression before discarding any term.

    Keep the original operands: even an unrepresentable individual share can belong
    to a representable total. Never restart a total after an overflowing contribution.
    """
    return finite_calculation(
        sum(value * multiplier / divisor for value, multiplier, divisor in terms),
        lambda: sum(
            (
                Fraction(value) * Fraction(multiplier) / Fraction(divisor)
                for value, multiplier, divisor in terms
            ),
            Fraction(),
        ),
    )


def finite_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return finite_calculation(
        sum(values) / len(values),
        lambda: sum(map(Fraction, values), Fraction()) / len(values),
    )
