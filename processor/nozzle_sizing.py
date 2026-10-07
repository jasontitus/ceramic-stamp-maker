"""Find the smallest stamp body at which one nozzle keeps the artwork's detail.

"Keeps detail" uses the nozzle comparison's own metric: lost and extra pattern
area, each at or below a tolerance percentage of the intended pattern. Every
candidate size is judged by ``print_preview.nozzle_detail``, the same code the
comparison cards use, so a recommended size reproduces the numbers shown there.
Stroke preservation is deliberately excluded: the answer is the size at which
the artwork itself survives, without selective widening.
"""

import contextlib
import math

from print_preview import NOZZLES_MM, nozzle_detail
from stamp_geometry import resolve_dimensions


MIN_BODY_MM = 12
MAX_BODY_MM = 200
TOLERANCES = (0.5, 1, 2, 5)
_GRID_POINTS = 9
# Candidates are whole tenths of a millimeter, steps / 10: exactly the value a
# size input holds once the UI writes it back. Any other float can rasterize a
# canvas one pixel different from the comparison and change its verdict.
_STEPS_PER_MM = 10


def _mm(steps):
    return steps / _STEPS_PER_MM


def _ladder(start, stop):
    """Up to _GRID_POINTS whole steps, geometrically spaced from start to stop."""
    if start == stop:
        return [start]
    points = [round(start * (stop / start) ** (i / (_GRID_POINTS - 1)))
              for i in range(_GRID_POINTS)]
    points[0], points[-1] = start, stop
    return list(dict.fromkeys(points))


def _bisect(evaluate, failing, passing):
    """Narrow a failing-below-passing bracket to 1% (at least one step) of the pass."""
    while passing - failing > max(1, passing // 100):
        middle = (passing + failing) // 2
        if evaluate(middle)["passes"]:
            passing = middle
        else:
            failing = middle
    return passing


def size_for_nozzle(svg_content, settings, mode, nozzle_mm, tolerance_percent,
                    lock=contextlib.nullcontext(), checkpoint=lambda: None):
    """Return the current size's detail and the smallest passing body size.

    ``settings`` holds the resolver arguments (``width_mm``, ``height_mm``,
    ``total_height_mm``, ``body_shape``, ``thicken``, ``fit``). Rectangular
    bodies scale uniformly, keeping their proportions; auto and round bodies
    vary the width or diameter. Candidates outside the 12–200 mm limits, or
    that the resolver rejects (auto height or fill overflow above 200 mm), are
    never proposed.

    When the current size fails, only sizes strictly larger than it are
    searched, so ``minimum`` always grows the stamp; if none of the sizes tried
    passes, ``achievable`` is false and ``largest`` reports the largest valid
    size. When the current size passes, ``minimum`` is the smallest size in the
    passing range just below it, and is informational only.

    Loss is not strictly monotonic in size: tiny bodies can close every gap and
    so lose little ink, and hairlines can enter the pattern only at large
    sizes. The search therefore walks a geometric ladder away from the current
    size, stops at the first change of verdict, and bisects that bracket. The
    returned size always passed an actual evaluation.

    ``lock`` is held around each full-resolution evaluation, not the whole
    search, so a server can bound memory without starving other previews.
    ``checkpoint`` runs before each evaluation and may raise to stop the search.
    """
    if isinstance(nozzle_mm, bool) or nozzle_mm not in NOZZLES_MM:
        raise ValueError("nozzle_mm must be one of " + ", ".join(f"{n:g}" for n in NOZZLES_MM))
    if isinstance(tolerance_percent, bool) or tolerance_percent not in TOLERANCES:
        raise ValueError("tolerance_percent must be one of " + ", ".join(f"{t:g}" for t in TOLERANCES))

    current = resolve_dimensions(svg_content, **settings)
    shape = current["body_shape"]
    aspect = current["height_mm"] / current["width_mm"] if shape == "rectangular" else None
    min_steps, max_steps = MIN_BODY_MM * _STEPS_PER_MM, MAX_BODY_MM * _STEPS_PER_MM

    def body(steps):
        height = None
        if aspect is not None:
            height = _mm(min(max_steps, max(min_steps, math.ceil(steps * aspect - 1e-9))))
        return resolve_dimensions(svg_content, **{**settings, "width_mm": _mm(steps), "height_mm": height})

    def judge(dimensions):
        checkpoint()
        with lock:
            lost, gained = nozzle_detail(svg_content, dimensions, mode, nozzle_mm)
        return {"dimensions": dimensions, "lost_percent": lost, "gained_percent": gained,
                "passes": lost <= tolerance_percent and gained <= tolerance_percent}

    judged = {}

    def evaluate(steps):
        if steps not in judged:
            judged[steps] = judge(body(steps))
        return judged[steps]

    def valid(steps):
        try:
            body(steps)
        except ValueError:
            return False
        return True

    low = min_steps if aspect is None else max(min_steps, math.ceil(min_steps / aspect - 1e-9))
    high = max_steps if aspect is None else min(max_steps, math.floor(max_steps / aspect + 1e-9))
    if not valid(high):
        # Sizes only become invalid above a threshold (auto height or fill
        # overflow past 200 mm), so bisect for the largest valid step.
        if not valid(low):
            raise ValueError("No body size between 12 and 200 mm fits this artwork, shape and fit")
        good, bad = low, high
        while bad - good > 1:
            middle = (good + bad) // 2
            if valid(middle):
                good = middle
            else:
                bad = middle
        high = good

    result = {
        "nozzle_mm": nozzle_mm,
        "line_width_mm": round(nozzle_mm * 1.05, 2),
        "tolerance_percent": tolerance_percent,
        "mode": mode,
        "current": judge(current),
        "achievable": True,
    }
    width = current["width_mm"]
    below = math.floor(width * _STEPS_PER_MM + 1e-9)
    if valid(below) and body(below) == current:
        judged[below] = result["current"]

    if result["current"]["passes"]:
        # Walk down from the current size; the first failure bounds the range.
        passing = failing = None
        for steps in _ladder(max(low, min(high, below)), low):
            if not evaluate(steps)["passes"]:
                failing = steps
                break
            passing = steps
        if passing is None:
            minimum = result["current"]
        elif failing is None:
            minimum = evaluate(passing)
        else:
            minimum = evaluate(_bisect(evaluate, failing, passing))
        result["minimum"] = minimum
        return result

    # Grow only: every candidate is strictly larger than the failing current size.
    first = max(low, below + 1)
    if first > high:
        result["achievable"] = False
        result["largest"] = result["current"]
        return result
    failing = None
    for steps in _ladder(first, high):
        if evaluate(steps)["passes"]:
            passing = steps
            break
        failing = steps
    else:
        result["achievable"] = False
        result["largest"] = evaluate(high)
        return result
    if failing is not None:
        passing = _bisect(evaluate, failing, passing)
    result["minimum"] = evaluate(passing)
    return result
