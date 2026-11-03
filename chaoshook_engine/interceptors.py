"""Deterministic SQL scheduling and crash injection for real SQLite cells."""

import re
import threading

from sqlalchemy import event


class SimulatedMidHandlerCrash(RuntimeError):
    pass


def _insert_table(statement):
    match = re.match(
        r'^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+["`\[]?(\w+)',
        statement, re.IGNORECASE,
    )
    return match.group(1).lower() if match else None


class DualThreadBarrierController:
    """Synchronize each worker's first idempotency query in one race cell.

    SELECT also rendezvous after execution, ensuring both workers observe the
    unclaimed event before either performs business writes. Later event inserts
    must not rendezvous: one worker can already hold SQLite's writer lock.
    """

    def __init__(self, engine):
        self.engine = engine
        self.enabled = False
        self.barrier = threading.Barrier(2, timeout=1.5)
        self.trace_log = []
        self._lock = threading.Lock()
        self._workers = set()
        self._select_key = f"chaoshook_barrier_select_{id(self)}"
        event.listen(engine, "before_cursor_execute", self._on_before_cursor)
        event.listen(engine, "after_cursor_execute", self._on_after_cursor)

    @property
    def sql_trace(self):
        return self.trace_log

    def _wait(self, message):
        name = threading.current_thread().name
        with self._lock:
            self.trace_log.append(f"[{name}] {message}")
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError as exc:
            with self._lock:
                self.trace_log.append(f"[{name}] SQL barrier timed out or broke")
            raise RuntimeError("SQL barrier requires two RaceWorker threads") from exc
        with self._lock:
            self.trace_log.append(f"[{name}] Released from SQL barrier")

    def _on_before_cursor(self, conn, cursor, statement, parameters, context, executemany):
        worker = threading.current_thread()
        sql = statement.strip().upper()
        if not self.enabled or not worker.name.startswith("RaceWorker-"):
            return
        if "WEBHOOK_EVENTS" not in sql or not sql.startswith(("SELECT", "INSERT")):
            return
        with self._lock:
            if worker in self._workers:
                return
            self._workers.add(worker)
        if sql.startswith("SELECT"):
            conn.info[self._select_key] = True
        self._wait(f"Reached SQL barrier before: {statement.strip()}")

    def _on_after_cursor(self, conn, cursor, statement, parameters, context, executemany):
        if conn.info.pop(self._select_key, False):
            self._wait("Idempotency SELECT executed; both reads must finish before writes")

    def close(self):
        """Detach listeners when the cell is finished."""
        self.enabled = False
        for name, listener in (
            ("before_cursor_execute", self._on_before_cursor),
            ("after_cursor_execute", self._on_after_cursor),
        ):
            if event.contains(self.engine, name, listener):
                event.remove(self.engine, name, listener)


class CrashAfterStatementController:
    """Crash once after a selected side effect, respecting real commit boundaries.

    A claim-first transaction crashes immediately after the side-effect INSERT.
    Without a prior claim, arm at INSERT and defer until the first SQL following
    its commit. Engine 'commit' fires *before* DBAPI commit, so raising there
    would incorrectly roll back v1 as well. No interceptor commits application
    transactions or modifies their data.
    """

    def __init__(self, engine, target_table: str = "credit_ledger"):
        if target_table not in {"credit_ledger", "outbox_emails"}:
            raise ValueError("target_table must be credit_ledger or outbox_emails")
        self.engine = engine
        self.target_table = target_table
        self.enabled = False
        self.attempt = 1
        self.crashed = False
        self.trace_log = []
        self._lock = threading.Lock()
        self._state_key = f"chaoshook_crash_{id(self)}"
        self._listeners = (
            ("before_cursor_execute", self._on_before_cursor),
            ("after_cursor_execute", self._on_after_cursor),
            ("commit", self._on_commit),
            ("rollback", self._on_rollback),
        )
        for name, listener in self._listeners:
            event.listen(engine, name, listener)

    @property
    def sql_trace(self):
        return self.trace_log

    def _armed(self):
        return self.enabled and self.attempt == 1 and not self.crashed

    def _crash(self, conn, step):
        with self._lock:
            if not self._armed():
                return
            self.crashed = True
            self.attempt = 2
            self.trace_log.append(f"Attempt 1: injecting crash {step}")
        conn.info.pop(self._state_key, None)
        raise SimulatedMidHandlerCrash(
            "Injected fault after side-effect statement before handler completion"
        )

    def _on_after_cursor(self, conn, cursor, statement, parameters, context, executemany):
        if not self._armed():
            return
        state = conn.info.setdefault(self._state_key, {})
        table = _insert_table(statement)
        if table == "webhook_events":
            state["claimed"] = True
        elif table == self.target_table:
            self.trace_log.append(f"Attempt 1: executed side-effect SQL: {statement.strip()}")
            if state.get("claimed"):
                self._crash(conn, f"after {self.target_table} INSERT inside claim-first transaction; rollback expected")
            else:
                state["pending"] = True
                self.trace_log.append("Attempt 1: waiting for intermediate commit before injecting crash")

    def _on_commit(self, conn):
        state = conn.info.get(self._state_key)
        if state is not None:
            state["claimed"] = False
            if self._armed() and state.get("pending"):
                state["committing"] = True

    def _on_before_cursor(self, conn, cursor, statement, parameters, context, executemany):
        state = conn.info.get(self._state_key, {})
        if self._armed() and state.get("committing"):
            # sqlite3 reports False only after the intermediate commit completed.
            if not conn.connection.driver_connection.in_transaction:
                self._crash(conn, f"after committed {self.target_table} side effect, before SQL: {statement.strip()}")

    def _on_rollback(self, conn):
        conn.info.pop(self._state_key, None)

    def close(self):
        """Detach all instrumentation; safe to call more than once."""
        self.enabled = False
        for name, listener in self._listeners:
            if event.contains(self.engine, name, listener):
                event.remove(self.engine, name, listener)
