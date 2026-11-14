"""In-process contextual bandit backend (LinUCB, disjoint arms). Research / shadow only.

Interface: BanditBackend.recommend(context, available_actions) / update(decision_id, reward).
Deterministic: no randomness, ties go to the baseline action then alphabetical order. State lives in memory (lost on restart;
rebuild with fit() from the Shadow Store, see replay.py). Stateless wrt Telegram / DB / production."""
import math
from collections import OrderedDict

import numpy as np

BANDIT_POLICY_VERSION = "linucb-1"
MAX_PENDING = 10_000


class BanditBackend:
    policy_version = "abstract"

    def recommend(self, context, available_actions, decision_id=None) -> dict:
        raise NotImplementedError

    def update(self, decision_id, reward) -> bool:
        raise NotImplementedError


class LinUCBBackend(BanditBackend):
    policy_version = BANDIT_POLICY_VERSION

    def __init__(self, actions, dim: int, alpha: float = 1.0, baseline: str | None = None, min_updates: int = 30,
                 reward_range: tuple = (0.0, 1.0)):
        self.actions = tuple(sorted(set(actions)))
        self.reward_range = (float(reward_range[0]), float(reward_range[1]))
        self.dim, self.alpha, self.baseline, self.min_updates = int(dim), float(alpha), baseline, int(min_updates)
        self._A = {a: np.eye(self.dim) for a in self.actions}
        self._b = {a: np.zeros(self.dim) for a in self.actions}
        self.n = {a: 0 for a in self.actions}
        self._pending: "OrderedDict[str, tuple]" = OrderedDict()

    def _x(self, context):
        try:
            x = np.asarray(context, dtype=float)
        except (TypeError, ValueError):
            return None
        return x if x.shape == (self.dim,) and np.all(np.isfinite(x)) else None

    def _score(self, a, x) -> float:
        A = self._A[a]
        theta = np.linalg.solve(A, self._b[a])
        return float(theta @ x + self.alpha * math.sqrt(max(0.0, float(x @ np.linalg.solve(A, x)))))

    def recommend(self, context, available_actions, decision_id=None) -> dict:
        base = {"policy_version": self.policy_version, "action": None, "propensity": None, "scores": {},
                "cold_start": True}
        x = self._x(context)
        if x is None:
            return {**base, "status": "malformed_context"}
        avail = sorted({a for a in (available_actions or ()) if isinstance(a, str) and a in self._A})
        if not avail:
            return {**base, "status": "no_available_actions"}
        scores = {a: round(self._score(a, x), 6) for a in avail}
        best = max(scores.values())
        tied = [a for a in avail if scores[a] == best]
        action = self.baseline if self.baseline in tied else tied[0]
        if decision_id is not None:
            self._pending[decision_id] = (x, action)
            while len(self._pending) > MAX_PENDING:
                self._pending.popitem(last=False)
        return {**base, "status": "ok", "action": action, "propensity": 1.0, "scores": scores,
                "cold_start": min(self.n[a] for a in avail) < self.min_updates}

    def _learn(self, x, action, reward) -> None:
        self._A[action] += np.outer(x, x)
        self._b[action] += reward * x
        self.n[action] += 1

    def update(self, decision_id, reward) -> bool:
        """Learn from the reward of a decision this backend recommended for. False (and no change) when the reward is
        missing / outside reward_range / not finite, or the decision is unknown. A reward is never invented."""
        if isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward) \
           or not self.reward_range[0] <= reward <= self.reward_range[1]:
            return False
        rec = self._pending.pop(decision_id, None)
        if rec is None:
            return False
        self._learn(rec[0], rec[1], float(reward))
        return True

    def fit(self, samples) -> int:
        """Offline: samples of (context, action, reward); invalid samples are skipped. Returns the number learned."""
        k = 0
        for ctx, action, reward in samples:
            x = self._x(ctx)
            if x is None or action not in self._A or isinstance(reward, bool) or not isinstance(reward, (int, float)) \
               or not math.isfinite(reward) or not self.reward_range[0] <= reward <= self.reward_range[1]:
                continue
            self._learn(x, action, float(reward))
            k += 1
        return k
