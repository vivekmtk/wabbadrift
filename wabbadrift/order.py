"""Order comparison helper."""

from __future__ import annotations

import bisect


def out_of_order(seq: list[int]) -> set[int]:
    """Positions in ``seq`` outside its longest increasing subsequence.

    ``seq`` lists, in custom order, each common item's baseline position. The longest run that
    is also in baseline order stays put; everything else is reported as moved.
    """
    tails: list[int] = []
    tail_pos: list[int] = []
    prev = [-1] * len(seq)
    for k, v in enumerate(seq):
        j = bisect.bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tail_pos.append(k)
        else:
            tails[j] = v
            tail_pos[j] = k
        prev[k] = tail_pos[j - 1] if j else -1
    keep = set()
    k = tail_pos[-1] if tail_pos else -1
    while k != -1:
        keep.add(k)
        k = prev[k]
    return set(range(len(seq))) - keep
