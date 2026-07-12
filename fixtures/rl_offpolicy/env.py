"""A tiny but real off-policy RL setup, no ML deps.

- ChainWalk: an N-state chain MDP. Start at 0, goal at N-1. Actions 0=left, 1=right.
  Reward +1 at the goal (terminal), -0.01 per step. Optimal policy: always go right.
- ReplayBuffer: stores transitions; the learner samples minibatches from it. This is
  what makes the loop *off-policy* with experience replay (DQN-style, tabular).
- QLearner: tabular Q-learning (an off-policy TD-control method). Behavior policy is
  epsilon-greedy; the target it learns toward is greedy.

Deterministic given a seed, and the metrics genuinely move (return goes up, TD loss
goes down), so the SDK annotation has meaningful data to track.
"""

from __future__ import annotations

import random


class ChainWalk:
    def __init__(self, n: int = 8, max_steps: int = 50):
        self.n = n
        self.max_steps = max_steps
        self.s = 0
        self.t = 0

    def reset(self) -> int:
        self.s = 0
        self.t = 0
        return self.s

    def step(self, a: int) -> tuple[int, float, bool]:
        self.t += 1
        self.s = max(0, self.s - 1) if a == 0 else min(self.n - 1, self.s + 1)
        done = self.s == self.n - 1 or self.t >= self.max_steps
        reward = 1.0 if self.s == self.n - 1 else -0.01
        return self.s, reward, done


class ReplayBuffer:
    def __init__(self, capacity: int = 5000):
        self.capacity = capacity
        self.data: list[tuple] = []

    def add(self, transition: tuple) -> None:
        self.data.append(transition)
        if len(self.data) > self.capacity:
            self.data.pop(0)

    def sample(self, k: int, rng: random.Random) -> list[tuple]:
        return rng.sample(self.data, min(k, len(self.data)))

    def __len__(self) -> int:
        return len(self.data)


class QLearner:
    """Tabular Q-learning. off-policy: it learns the greedy target while behaving
    epsilon-greedily and replaying old transitions from a buffer."""

    def __init__(self, n_states: int, n_actions: int = 2, lr: float = 0.5, gamma: float = 0.98, seed: int = 0):
        self.q = [[0.0] * n_actions for _ in range(n_states)]
        self.lr = lr
        self.gamma = gamma
        self.n_actions = n_actions
        self.rng = random.Random(seed)

    def act(self, s: int, epsilon: float) -> int:
        if self.rng.random() < epsilon:
            return self.rng.randrange(self.n_actions)
        row = self.q[s]
        return max(range(self.n_actions), key=lambda a: row[a])

    def update_batch(self, batch: list[tuple]) -> tuple[float, float]:
        """One TD update per transition. Returns (mean |TD error|, mean state value)."""
        total_td = 0.0
        for (s, a, r, s2, done) in batch:
            target = r + (0.0 if done else self.gamma * max(self.q[s2]))
            td = target - self.q[s][a]
            self.q[s][a] += self.lr * td
            total_td += abs(td)
        mean_td = total_td / max(1, len(batch))
        mean_q = sum(max(row) for row in self.q) / len(self.q)
        return mean_td, mean_q
