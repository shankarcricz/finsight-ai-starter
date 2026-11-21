"""Per-user rate limits for endpoints that cost money (OpenAI, embeddings) or hit third-party APIs.

In-memory sliding window: fine for one web process. With several workers/instances each keeps its own
counts, so move this to Redis (or the database) before scaling out.
"""
import threading
import time
from collections import defaultdict, deque
from fastapi import Depends, HTTPException
from app.auth import current_user

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_lock = threading.Lock()


def check(bucket: str, user_id: str, limit: int, window_s: int) -> None:
    now = time.monotonic()
    with _lock:
        q = _hits[(bucket, user_id)]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            retry = int(window_s - (now - q[0])) + 1
            raise HTTPException(429, f'Too many requests. Try again in {max(retry // 60, 1)} min.',
                                headers={'Retry-After': str(retry)})
        q.append(now)


def reset() -> None:
    with _lock:
        _hits.clear()


def limited(bucket: str, limit: int, window_s: int):
    """Dependency returning the user id after counting this request against the user's budget."""
    def dep(user_id: str = Depends(current_user)) -> str:
        check(bucket, user_id, limit, window_s)
        return user_id
    return Depends(dep)


# Budgets per user. Generous for a person, tight enough to cap a runaway script.
CHAT = limited('chat', 40, 3600)
UPLOAD = limited('upload', 60, 3600)
QUOTE = limited('quote', 120, 3600)
FEEDBACK = limited('feedback', 120, 3600)
