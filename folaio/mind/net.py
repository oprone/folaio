"""FolaioNet: Folaio's own neural network, written from scratch with NumPy.

One hidden layer (like Cony AI), trained on the user's own PDFs:
input = a sentence's words, output = which section of the library it came from.
While learning that, the hidden layer learns which words and ideas belong
together; Folaio uses it as its "meaning" for search and quizzes.
"""
from __future__ import annotations

import time

import numpy as np

from .text import DIM, Sparse


class FolaioNet:
    def __init__(self, n_classes: int, hidden: int = 64, seed: int = 42):
        rng = np.random.default_rng(seed)
        self.W1 = rng.normal(0, 0.1, (DIM, hidden)).astype(np.float32)
        self.b1 = np.zeros(hidden, dtype=np.float32)
        self.W2 = rng.normal(0, np.sqrt(2 / hidden), (hidden, n_classes)).astype(np.float32)
        self.b2 = np.zeros(n_classes, dtype=np.float32)

    # ---------- using the network ----------
    def hidden(self, X: Sparse) -> np.ndarray:
        return np.maximum(X.dot(self.W1) + self.b1, 0)

    def proba(self, X: Sparse) -> np.ndarray:
        return _softmax(self.hidden(X) @ self.W2 + self.b2)

    def embed(self, X: Sparse) -> np.ndarray:
        """The hidden layer as a meaning-fingerprint, length 1."""
        h = self.hidden(X)
        return h / np.maximum(np.linalg.norm(h, axis=1, keepdims=True), 1e-9)

    # ---------- learning ----------
    def fit(self, X: Sparse, y: np.ndarray, epochs: int = 20, lr: float = 0.01,
            batch: int = 64, time_budget: float = 120.0, progress=None) -> None:
        """Mini-batch backpropagation with the Adam optimizer.

        Only the rows of W1 for words actually in a batch are touched, so a step
        costs the same whether the library has 1 book or 100.
        """
        n = X.n_rows
        rng = np.random.default_rng(0)
        self._adam = {k: (np.zeros_like(v), np.zeros_like(v)) for k, v in self.state().items()}
        self._t = 0
        start = time.time()
        for epoch in range(epochs):
            order = rng.permutation(n)
            for s in range(0, n, batch):
                rows = order[s:s + batch]
                self._step(X.take(rows), y[rows], lr)
            if progress:
                progress(epoch + 1, epochs)
            if time.time() - start > time_budget:
                break   # good enough: never keep a slow PC busy for too long
        del self._adam   # free the optimizer's memory once learning is done

    def _step(self, xb: Sparse, yb: np.ndarray, lr: float) -> None:
        m = xb.n_rows
        # forward
        z1 = xb.dot(self.W1) + self.b1
        h = np.maximum(z1, 0)
        p = _softmax(h @ self.W2 + self.b2)
        # backward (softmax + cross-entropy)
        p[np.arange(m), yb] -= 1
        d2 = p / m
        dh = (d2 @ self.W2.T) * (z1 > 0)
        # gradient of W1 only for the words present in this batch
        rows, inv = np.unique(xb.indices, return_inverse=True)
        g1 = np.zeros((len(rows), self.W1.shape[1]), dtype=np.float32)
        np.add.at(g1, inv, xb.data[:, None] * dh[xb.row_ids()])
        self._t += 1
        self._adam_update("W2", self.W2, h.T @ d2, lr)
        self._adam_update("b2", self.b2, d2.sum(0), lr)
        self._adam_update("b1", self.b1, dh.sum(0), lr)
        self._adam_update("W1", self.W1, g1, lr, rows)

    def _adam_update(self, name, param, grad, lr, rows=None, b1=0.9, b2=0.999, eps=1e-8):
        mom, vel = self._adam[name]
        if rows is not None:   # "lazy" Adam: update only the touched rows
            mom[rows] = b1 * mom[rows] + (1 - b1) * grad
            vel[rows] = b2 * vel[rows] + (1 - b2) * grad ** 2
            mh, vh = mom[rows], vel[rows]
        else:
            mom[:] = b1 * mom + (1 - b1) * grad
            vel[:] = b2 * vel + (1 - b2) * grad ** 2
            mh, vh = mom, vel
        step = lr * (mh / (1 - b1 ** self._t)) / (np.sqrt(vh / (1 - b2 ** self._t)) + eps)
        if rows is not None:
            param[rows] -= step
        else:
            param -= step

    # ---------- saving ----------
    def state(self) -> dict:
        return {"W1": self.W1, "b1": self.b1, "W2": self.W2, "b2": self.b2}

    @classmethod
    def from_state(cls, s) -> "FolaioNet":
        net = cls.__new__(cls)
        net.W1, net.b1, net.W2, net.b2 = (np.asarray(s[k]) for k in ("W1", "b1", "W2", "b2"))
        return net


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)
