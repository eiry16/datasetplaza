"""源方探测的令牌桶：既限长期速率，也限短时突发，并在被掐断后退避。

为什么不是 ``time.sleep(1.2)``：固定间隔只管得住「平均每 1.2 秒一次」，
管不住「连打 5 次再停 6 秒」这种突发。国家统计局本轮实测是**连续约 100 次
枚举后整站按 IP 转 302 循环**，而且掐断后连 search 都跟着 302 —— 触发的是
「一段时间里的请求总量」，不是「相邻两次的间隔」。令牌桶直接对总量建模：

* ``rate``      每秒补充的令牌，长期速率上限；
* ``capacity``  桶容量，也就是允许的短时突发次数；
* ``backoff``   连续失败后指数退避，避免在被限流的 IP 上继续撞墙；
* ``max_fails`` 连续失败到这个数就置 ``exhausted``，让调用方**停手并留断点**，
  而不是硬重试到源方恢复。

刻意不做的事：不自动重试请求、不吞异常、不猜源方限流规则。桶只回答
「现在能不能发、要等多久、是不是该停」，请求成败由调用方告诉它。
这样接 Eurostat / OECD / IMF 时可以原样复用，不必把 NBS 的 302 逻辑抄三遍。

``clock`` / ``sleeper`` 可注入，单测不需要真的睡。
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    """线程安全的令牌桶 + 连续失败退避。"""

    def __init__(
        self,
        rate: float = 0.8,
        capacity: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        max_fails: int = 5,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate 必须为正")
        if capacity < 1:
            raise ValueError("capacity 至少为 1")
        self.rate = float(rate)
        self.capacity = int(capacity)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.max_fails = int(max_fails)
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._lock = threading.Lock()
        self._tokens = float(self.capacity)
        self._last = self._clock()
        self._fails = 0
        self._exhausted = False
        self.waited = 0.0        # 累计等待秒数，便于观测
        self.requests = 0        # 成功 acquire 的次数

    @classmethod
    def from_sleep(cls, sleep: float, **kw) -> "TokenBucket":
        """把旧的 ``--sleep 1.2`` 参数换算成速率，保持命令行兼容。"""
        if sleep <= 0:
            raise ValueError("sleep 必须为正")
        return cls(rate=1.0 / sleep, **kw)

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.capacity,
                           self._tokens + elapsed * self.rate)
        self._last = now

    def acquire(self, tokens: float = 1.0) -> float:
        """阻塞到拿到 ``tokens`` 个令牌，返回实际等待的秒数。

        连续失败期间会额外叠加指数退避。被置为 ``exhausted`` 后直接抛，
        调用方应该停手留断点，而不是继续请求。
        """
        if self._exhausted:
            raise RuntimeError("连续失败已达上限，停止探测（留断点，稍后重跑）")
        with self._lock:
            total = 0.0
            while True:
                self._refill(self._clock())
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    break
                need = (tokens - self._tokens) / self.rate
                self._sleep(need)
                total += need
                # 用注入的 clock 推进：真环境下 sleep 会让时钟自己走，
                # 测试里 clock 由 sleeper 同步推进，这里统一刷新 _last。
                now = self._clock()
                self._refill(now)
            if self._fails:
                extra = min(self.backoff_base * (2 ** (self._fails - 1)),
                            self.backoff_cap)
                self._sleep(extra)
                total += extra
            self.waited += total
            self.requests += 1
            return total

    def note_failure(self) -> None:
        """报告一次失败；连续到达 ``max_fails`` 就置 ``exhausted``。"""
        with self._lock:
            self._fails += 1
            if self._fails >= self.max_fails:
                self._exhausted = True

    def note_success(self) -> None:
        """一次成功即清掉连续失败计数与退避。"""
        with self._lock:
            self._fails = 0

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    @property
    def consecutive_failures(self) -> int:
        return self._fails

    def state(self) -> dict:
        """可观测状态，写进采集日志/报告，便于事后核对限流是否生效。"""
        with self._lock:
            self._refill(self._clock())
            return {"rate": self.rate, "capacity": self.capacity,
                    "tokens": round(self._tokens, 3),
                    "consecutive_failures": self._fails,
                    "exhausted": self._exhausted,
                    "requests": self.requests,
                    "waited_seconds": round(self.waited, 3)}
