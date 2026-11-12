from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from hashlib import blake2s
from urllib.parse import urlsplit

import numpy as np

import database

WIDTH = 64
HISTORY = 48
SIGNAL = {"open": 0.9, "save": 2.6, "unsave": -2.6, "outbound_click": 1.4}
model = None


def item_vector(kind, topic, link, location):
    try:
        organizer = urlsplit(link or "").hostname
    except ValueError:
        organizer = None
    tags = [("type", kind, 1.4), ("topic", topic, 1.0), ("organizer", organizer, 0.35)]
    if location and "онлайн" in location.casefold():
        tags.append(("format", "online", 0.35))
    vector = np.zeros(WIDTH, dtype=np.float32)
    for field, value, weight in tags:
        if value:
            key = f"{field}:{value.casefold()}".encode()
            column = int.from_bytes(blake2s(key, digest_size=4).digest(), "big") % WIDTH
            vector[column] += weight
    return vector / max(1, np.linalg.norm(vector))


def reaction(kind, dwell):
    if kind == "view_end":
        if dwell is None:
            return 0
        return -0.35 if dwell < 1000 else min(0.55, dwell / 12000)
    return SIGNAL.get(kind, 0)


def user_vector(history, session):
    result = np.zeros((2, WIDTH), dtype=np.float32)
    for index, (vector, weight, session_id) in enumerate(reversed(history)):
        result[0] += weight * vector * 0.96 ** index
        if index < 12 and session_id == session:
            result[1] += weight * vector * 0.89 ** index
    return (np.sign(result) * np.log1p(abs(result))).ravel()


def baseline(profile, items):
    return items @ (profile[:WIDTH] + 0.75 * profile[WIDTH:])


def rank(events, user_key, session):
    history = []
    if user_key:
        rows = database._fetch("""
            SELECT a.kind, a.dwell_ms, a.session_id, e.event_type, e.topic, e.link, e.location
            FROM miniapp_actions a JOIN events e ON e.id = a.event_id
            WHERE a.user_key = ? AND a.surface = 'discover' AND a.kind != 'impression'
            ORDER BY a.received_at DESC, a.occurred_at DESC, a.action_id DESC LIMIT 48
        """, (user_key,))
        history = [(item_vector(kind, topic, link, location), reaction(action, dwell), sid)
                   for action, dwell, sid, kind, topic, link, location in reversed(rows)]
    profile = user_vector(history, session)
    current_model = model
    policy = "discovery_v2"
    scored = events
    if np.any(profile) and events:
        vectors = np.stack([item_vector(e["type"], e["topic"], e["link"], e["location"]) for e in events])
        if current_model is not None:
            scores = current_model.score(profile[None, :], vectors)
            policy = "two_tower_v1"
        else:
            scores = baseline(profile, vectors)
            policy = "behavior_v1"
        scored = [events[i] for i in np.argsort(-scores, kind="stable")]

    # Mix formats among the first cards so a new source cannot monopolize the feed.
    if policy == "discovery_v2":
        groups = {}
        for event in scored[:60]:
            groups.setdefault(event["type"], deque()).append(event)
        diverse = []
        while groups:
            for key in list(groups):
                diverse.append(groups[key].popleft())
                if not groups[key]:
                    del groups[key]
        scored = diverse + scored[60:]
    elif len(scored) >= 10:
        # Give a few near-term cards outside the top scores a chance to be seen.
        for index, event in zip((4, 9, 14), events[:12:4]):
            current_index = scored.index(event)
            if current_index > index:
                scored.insert(index, scored.pop(current_index))
    return scored, policy


class Tower:
    def __init__(self, inputs, rng):
        self.weights = [
            (rng.normal(0, (2 / inputs) ** 0.5, (inputs, 64))).astype(np.float32),
            np.zeros(64, dtype=np.float32),
            (rng.normal(0, (2 / 64) ** 0.5, (64, 32))).astype(np.float32),
            np.zeros(32, dtype=np.float32),
        ]

    def forward(self, inputs):
        w1, b1, w2, b2 = self.weights
        hidden = np.maximum(0, inputs @ w1 + b1)
        output = hidden @ w2 + b2
        norm = np.maximum(1e-6, np.linalg.norm(output, axis=1, keepdims=True))
        return output / norm, (inputs, hidden, norm)

    def gradients(self, cache, output, gradient):
        inputs, hidden, norm = cache
        output_gradient = (gradient - output * (output * gradient).sum(axis=1, keepdims=True)) / norm
        hidden_gradient = (output_gradient @ self.weights[2].T) * (hidden > 0)
        return [inputs.T @ hidden_gradient, hidden_gradient.sum(axis=0),
                hidden.T @ output_gradient, output_gradient.sum(axis=0)]


class TwoTower:
    def __init__(self, rng):
        self.user = Tower(2 * WIDTH, rng)
        self.event = Tower(WIDTH, rng)

    def score(self, profiles, items):
        users, _ = self.user.forward(profiles)
        cards, _ = self.event.forward(items)
        return (users * cards).sum(axis=1) * 4

    def fit(self, profiles, items, labels, rng):
        weights = self.user.weights + self.event.weights
        first = [np.zeros_like(w) for w in weights]
        second = [np.zeros_like(w) for w in weights]
        step = 0
        for _ in range(12):
            order = rng.permutation(len(labels))
            for start in range(0, len(labels), 128):
                ids = order[start:start + 128]
                users, uc = self.user.forward(profiles[ids])
                cards, ec = self.event.forward(items[ids])
                logits = (users * cards).sum(axis=1) * 4
                error = (1 / (1 + np.exp(-logits)) - labels[ids])[:, None] * (4 / len(ids))
                gradients = self.user.gradients(uc, users, cards * error)
                gradients += self.event.gradients(ec, cards, users * error)
                step += 1
                for i, (weight, gradient) in enumerate(zip(weights, gradients)):
                    first[i] = 0.9 * first[i] + 0.1 * gradient
                    second[i] = 0.999 * second[i] + 0.001 * gradient ** 2
                    weight -= 0.001 * (first[i] / (1 - 0.9 ** step)) / (
                        np.sqrt(second[i] / (1 - 0.999 ** step)) + 1e-8)


def average_precision(labels, scores):
    order = np.argsort(-scores, kind="stable")
    ordered = labels[order]
    thresholds = np.r_[np.flatnonzero(np.diff(scores[order])), len(labels) - 1]
    tp = np.cumsum(ordered)[thresholds]
    return float(np.sum((tp / (thresholds + 1)) * np.diff(tp, prepend=0)) / tp[-1])


def train_from_logs():
    global model
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=45)).isoformat()
    latest = (now - timedelta(minutes=30)).isoformat()
    rows = database._fetch("""
        SELECT a.user_key, a.session_id, a.impression_id, a.kind, a.dwell_ms, a.received_at,
               e.event_type, e.topic, e.link, e.location
        FROM miniapp_actions a JOIN events e ON e.id = a.event_id
        WHERE a.surface = 'discover' AND a.received_at BETWEEN ? AND ?
        ORDER BY a.received_at DESC, a.occurred_at DESC,
                 CASE WHEN a.kind = 'impression' THEN 0 ELSE 1 END DESC, a.action_id DESC
        LIMIT 24000
    """, (start, latest))
    saved = {imp: datetime.fromisoformat(timestamp)
             for _, _, imp, action, _, timestamp, *_ in rows if action == "save"}
    if len(saved) < 20:
        return "ожидаем сохранения", len(saved)
    history = defaultdict(lambda: deque(maxlen=HISTORY))
    samples = []
    for user, session, imp, action, dwell, timestamp, kind, topic, link, location in reversed(rows):
        item = item_vector(kind, topic, link, location)
        if action == "impression":
            profile = user_vector(history[user], session)
            if np.any(profile):
                shown = datetime.fromisoformat(timestamp)
                was_saved = imp in saved and shown <= saved[imp] <= shown + timedelta(minutes=30)
                samples.append((profile, item, was_saved, timestamp, user))
        else:
            history[user].append((item, reaction(action, dwell), session))
    if len(samples) < 200 or len({row[4] for row in samples}) < 10:
        return "ожидаем историю показов", len(samples)

    split = samples[int(len(samples) * 0.8)][3]
    # Keep a gap so late saves on training impressions cannot leak across the split.
    cutoff = (datetime.fromisoformat(split) - timedelta(minutes=30)).isoformat()
    train = [row for row in samples if row[3] < cutoff]
    valid = [row for row in samples if row[3] >= split]
    if (len(train) < 100 or sum(row[2] for row in train) < 10
            or sum(row[2] for row in valid) < 5 or all(row[2] for row in valid)):
        return "ожидаем временную валидацию", len(valid)

    def arrays(data):
        profiles, items, labels, *_ = zip(*data)
        return np.stack(profiles), np.stack(items), np.asarray(labels, dtype=np.float32)

    rng = np.random.default_rng(42)
    candidate = TwoTower(rng)
    candidate.fit(*arrays(train), rng)
    profiles, items, labels = arrays(valid)
    scores = candidate.score(profiles, items)
    basic = (items * (profiles[:, :WIDTH] + 0.75 * profiles[:, WIDTH:])).sum(axis=1)
    ap = average_precision(labels, scores)
    baseline_ap = average_precision(labels, basic)
    if ap > baseline_ap:
        model = candidate
    return "AP Two-Tower / baseline", round(ap, 4), round(baseline_ap, 4)
