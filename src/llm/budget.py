"""Atomic, persistent request admission. Unknown charges stay reserved, including after a restart."""
from __future__ import annotations

import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .base import BudgetExceeded

_CHANGED = threading.Condition(threading.RLock())
_ACTIVE = {}  # process-owned reservations by database; persisted foreign pending is never assumed live

class ReservationPressure(BudgetExceeded):
    """The request would fit after genuinely live reservations settle."""


def micro_usd(value: float) -> int:
    if not math.isfinite(value) or value < 0:
        raise ValueError('cost must be finite and nonnegative')
    return math.ceil(value * 1_000_000)


class BudgetLedger:
    def __init__(self, path: Optional[str], limits: Mapping[str, Optional[float]],
                 max_requests: int = 500, deadline_s: float = 1800,
                 initial_costs: Optional[Mapping[str, Sequence[float]]] = None):
        if max_requests < 1 or deadline_s <= 0:
            raise ValueError('request allowance and deadline must be positive')
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path or ':memory:', timeout=15, check_same_thread=False,
                                  isolation_level=None)
        self.lock = threading.RLock()
        self.identity = str(Path(path).resolve()) if path else uuid.uuid4().hex
        self.deadline = time.monotonic() + deadline_s
        self.scopes = list(limits)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS accounts (
                scope TEXT PRIMARY KEY, cap INTEGER, requests INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY, amount INTEGER NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries (
                reservation TEXT NOT NULL, scope TEXT NOT NULL,
                PRIMARY KEY(reservation, scope));
        ''')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for scope, limit in limits.items():
                    cap = micro_usd(limit) if limit is not None else None
                    row = self.db.execute('SELECT cap, requests FROM accounts WHERE scope=?', (scope,)).fetchone()
                    if row is None:
                        self.db.execute('INSERT INTO accounts VALUES (?,?,?)', (scope, cap, max_requests))
                        for cost in (initial_costs or {}).get(scope, []):
                            historical = uuid.uuid4().hex
                            self.db.execute('INSERT INTO reservations VALUES (?,?,?)',
                                            (historical, micro_usd(cost), 'settled'))
                            self.db.execute('INSERT INTO entries VALUES (?,?)', (historical, scope))
                    elif (row[0] is not None and (cap is None or cap > row[0])) or max_requests > row[1]:
                        raise ValueError('cannot silently enlarge a persisted budget/request allowance')
                    else:
                        self.db.execute('UPDATE accounts SET cap=?, requests=? WHERE scope=?',
                                        (cap, max_requests, scope))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded('phase deadline reached; coverage is incomplete')

    def reserve(self, usd: float) -> str:
        while True:
            try:
                return self._reserve_now(usd)
            except ReservationPressure:
                self.check_deadline()
                with _CHANGED:
                    _CHANGED.wait(timeout=min(.1, max(.001,self.deadline-time.monotonic())))

    def _reserve_now(self, usd: float) -> str:
        self.check_deadline()
        amount = micro_usd(usd)
        rid = uuid.uuid4().hex
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for scope in self.scopes:
                    cap, allowance = self.db.execute('SELECT cap, requests FROM accounts WHERE scope=?', (scope,)).fetchone()
                    used, calls = self.db.execute('''SELECT COALESCE(SUM(r.amount),0), COUNT(*)
                        FROM reservations r JOIN entries e ON e.reservation=r.id WHERE e.scope=?''', (scope,)).fetchone()
                    if calls >= allowance:
                        raise BudgetExceeded(f'physical admission allowance exhausted ({scope})')
                    if cap is not None and used + amount > cap:
                        with _CHANGED:
                            live=set(_ACTIVE.get(self.identity,set()))
                        pending=self.db.execute('''SELECT r.id,r.amount FROM reservations r
                            JOIN entries e ON e.reservation=r.id
                            WHERE e.scope=? AND r.state='pending' ''',(scope,)).fetchall()
                        active=sum(n for key,n in pending if key in live)
                        if used-active+amount<=cap and active:
                            raise ReservationPressure('queued behind in-flight settlements')
                        stale=any(key not in live for key,_ in pending)
                        reason='; pending reconciliation needed' if stale else ''
                        raise BudgetExceeded(f'budget cannot fit next reservation ({scope}){reason}; coverage incomplete')
                self.db.execute('INSERT INTO reservations VALUES (?,?,?)', (rid, amount, 'pending'))
                self.db.executemany('INSERT INTO entries VALUES (?,?)', [(rid, s) for s in self.scopes])
                self.db.execute('COMMIT')
                with _CHANGED:
                    _ACTIVE.setdefault(self.identity,set()).add(rid)
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        return rid

    def settle(self, rid: str, actual_usd: Optional[float]):
        """None means unknown billing: retain the entire admitted amount, never release it optimistically."""
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                old = self.db.execute('SELECT amount FROM reservations WHERE id=?', (rid,)).fetchone()
                if old is None:
                    raise KeyError('unknown reservation')
                amount = old[0] if actual_usd is None else micro_usd(actual_usd)
                state = 'uncertain' if actual_usd is None else 'settled'
                self.db.execute('UPDATE reservations SET amount=?, state=? WHERE id=?', (amount, state, rid))
                self.db.execute('COMMIT')
                with _CHANGED:
                    _ACTIVE.setdefault(self.identity,set()).discard(rid)
                    _CHANGED.notify_all()
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        if actual_usd is not None and amount > old[0]:
            raise BudgetExceeded('provider usage exceeded reserved upper bound; stop and reconcile billing')

    def summary(self) -> dict:
        with self.lock:
            return {scope: {state: {'usd': amount / 1e6, 'requests': count}
                            for state, amount, count in self.db.execute('''
                                SELECT r.state,SUM(r.amount),COUNT(*) FROM reservations r
                                JOIN entries e ON e.reservation=r.id WHERE e.scope=? GROUP BY r.state
                            ''', (scope,)).fetchall()} for scope in self.scopes}
