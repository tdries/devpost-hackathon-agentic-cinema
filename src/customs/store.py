import functools
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from customs.schema import ChangeRecord, Finding, Observation, RunRecord

def _locked(method):
    """Serialize one Store method on this store's connection lock.

    The connection is opened with check_same_thread=False and is genuinely
    used from several threads at once: the crew's ParallelAgent judges every
    market on its own thread, and Starlette runs each alert webhook's
    background remediation in a threadpool worker. CPython's sqlite3 reports
    threadsafety 3, but that is about the C library being serialized, not
    about two threads interleaving statements on one Python Connection: doing
    that raises "sqlite3.InterfaceError: bad parameter or other API misuse"
    and, worse, sometimes just returns None for a row that exists. Both were
    observed here, from two concurrent remediations of one run.

    An RLock rather than a Lock because these methods compose
    (set_run_status calls get_run and _write_run), and every one of them is a
    short statement plus a commit, so a single lock costs nothing at this
    scale. Ceiling: this serializes one process. Two processes on one sqlite
    file still rely on WAL and sqlite's own locking, which is the point at
    which this store should become a real database.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper

class Store:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        """Create the tables if they are not there.

        observations and findings are keyed (run_id, id), not id alone.
        Neither id is globally unique and neither was ever meant to be:
        analyst.observe_shot mints obs_{shot_id}_{n} from a per-video shot
        index, so every video's first shot is shot_0 and its first
        observation is obs_shot_0_000, and adjudicate.judge builds
        fnd_{market}_{rule}_{observation} on top of that. With id alone as
        the primary key the *second* run of anything into a given database
        died with "UNIQUE constraint failed", which is exactly what happened
        on this project's first repeated live run.

        This is a plain schema change with no migration: CREATE TABLE IF NOT
        EXISTS leaves an older file on its old schema, silently, so a
        database created before this change must be deleted rather than
        reused. Demo-grade on purpose; there is nothing in a run store worth
        migrating.
        """
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (run_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_observations_run
                ON observations (run_id);
            CREATE TABLE IF NOT EXISTS findings (
                id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                market TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (run_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_findings_run
                ON findings (run_id);
            CREATE INDEX IF NOT EXISTS idx_findings_run_market
                ON findings (run_id, market);
            CREATE TABLE IF NOT EXISTS changes (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_changes_run
                ON changes (run_id);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts REAL NOT NULL,
                agent TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_run
                ON events (run_id, id);
            CREATE TABLE IF NOT EXISTS spend (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                ts REAL NOT NULL,
                method TEXT NOT NULL,
                eur REAL NOT NULL,
                run_id TEXT NOT NULL,
                finding_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_spend_day ON spend (day);
        """)
        self._conn.commit()

    # --- the generation budget ---
    #
    # Veo is the only thing here that costs real money per use, so every
    # bridge is written down before it runs and the day's total is what the
    # console checks before offering the option. Keyed by UTC date because a
    # budget that resets on local midnight resets twice a year.

    @_locked
    def record_spend(self, method: str, eur: float, run_id: str,
                     finding_id: str, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        day = time.strftime("%Y-%m-%d", time.gmtime(stamp))
        self._conn.execute(
            "INSERT INTO spend (day, ts, method, eur, run_id, finding_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (day, stamp, method, float(eur), run_id, finding_id))
        self._conn.commit()

    @_locked
    def spent_today(self, now: float | None = None) -> float:
        stamp = time.time() if now is None else now
        day = time.strftime("%Y-%m-%d", time.gmtime(stamp))
        row = self._conn.execute(
            "SELECT COALESCE(SUM(eur), 0) FROM spend WHERE day = ?", (day,)).fetchone()
        return float(row[0] or 0.0)

    @_locked
    def create_run(self, asset_path: str, markets: list[str]) -> RunRecord:
        run = RunRecord(
            id=f"run_{uuid.uuid4().hex[:12]}",
            asset_path=asset_path,
            t0=None,
            status="created",
            markets=list(markets),
        )
        self._conn.execute(
            "INSERT INTO runs (id, data) VALUES (?, ?)",
            (run.id, json.dumps(run.to_json())),
        )
        self._conn.commit()
        return run

    @_locked
    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT data FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return RunRecord.from_json(json.loads(row[0])) if row else None

    @_locked
    def recent_runs(self, limit: int = 20) -> list[RunRecord]:
        """The newest runs first, for the console's front door.

        Ordered by rowid rather than by t0: a run that was created but never
        started has no t0 at all, and it is exactly the run someone is most
        likely to be looking for.
        """
        rows = self._conn.execute(
            "SELECT data FROM runs ORDER BY rowid DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [RunRecord.from_json(json.loads(r[0])) for r in rows]

    @_locked
    def _write_run(self, run: RunRecord) -> None:
        self._conn.execute(
            "UPDATE runs SET data = ? WHERE id = ?",
            (json.dumps(run.to_json()), run.id),
        )
        self._conn.commit()

    @_locked
    def set_run_status(self, run_id: str, status: str) -> None:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        run.status = status
        self._write_run(run)

    @_locked
    def add_run_markets(self, run_id: str, markets: list[str]) -> list[str]:
        """Add markets to an existing run; return only the ones that are new.

        A second clearance against the same asset is a judging pass over
        observations that already exist, so the run record grows rather than
        a new run being created. Returning the difference is what lets the
        caller judge only what has not been judged, and re-adding a market
        already on the run is a no-op rather than a duplicate row.
        """
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        fresh = [m for m in markets if m not in run.markets]
        if fresh:
            run.markets = list(run.markets) + fresh
            self._write_run(run)
        return fresh

    @_locked
    def set_run_t0(self, run_id: str, t0: float) -> None:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        run.t0 = t0
        self._write_run(run)

    @_locked
    def add_observations(self, run_id: str, observations: list[Observation]) -> None:
        rows = [(o.id, run_id, json.dumps(o.to_json())) for o in observations]
        self._conn.executemany(
            "INSERT INTO observations (id, run_id, data) VALUES (?, ?, ?)", rows
        )
        self._conn.commit()

    @_locked
    def observations(self, run_id: str) -> list[Observation]:
        rows = self._conn.execute(
            "SELECT data FROM observations WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [Observation.from_json(json.loads(r[0])) for r in rows]

    @_locked
    def add_findings(self, findings: list[Finding]) -> None:
        rows = [
            (f.id, f.run_id, f.market, json.dumps(f.to_json())) for f in findings
        ]
        self._conn.executemany(
            "INSERT INTO findings (id, run_id, market, data) VALUES (?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    @_locked
    def findings(self, run_id: str, market: str | None = None) -> list[Finding]:
        if market is None:
            rows = self._conn.execute(
                "SELECT data FROM findings WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data FROM findings WHERE run_id = ? AND market = ? "
                "ORDER BY rowid",
                (run_id, market),
            ).fetchall()
        return [Finding.from_json(json.loads(r[0])) for r in rows]

    @_locked
    def update_finding_status(self, finding_id: str, status: str,
                              run_id: str | None = None) -> None:
        """Set one finding's status.

        A finding id is unique within a run, not across runs (see
        _init_schema), so `run_id` says which run's copy to update. Left out,
        an id that exists in exactly one run still resolves -- which keeps
        every existing two-argument call working -- and an id that exists in
        several raises instead of updating all of them, because a bare
        "UPDATE ... WHERE id = ?" would write one run's finding over
        another's.
        """
        if run_id is None:
            rows = self._conn.execute(
                "SELECT run_id, data FROM findings WHERE id = ?", (finding_id,)
            ).fetchall()
            if not rows:
                raise ValueError(f"unknown finding: {finding_id}")
            if len(rows) > 1:
                raise ValueError(
                    f"finding {finding_id!r} exists in {len(rows)} runs; "
                    "pass run_id to say which one"
                )
            run_id, raw = rows[0]
        else:
            row = self._conn.execute(
                "SELECT data FROM findings WHERE id = ? AND run_id = ?",
                (finding_id, run_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown finding: {finding_id} in run {run_id}")
            raw = row[0]
        data = json.loads(raw)
        data["status"] = status
        finding = Finding.from_json(data)
        self._conn.execute(
            "UPDATE findings SET data = ? WHERE id = ? AND run_id = ?",
            (json.dumps(finding.to_json()), finding_id, run_id),
        )
        self._conn.commit()

    @_locked
    def open_finding_by_labels(self, asset: str, market: str,
                               rule_id: str) -> tuple[RunRecord, Finding] | None:
        """The newest still-open finding matching one alert's labels.

        A Grafana alert carries {asset, market, rule_id} and nothing else this
        system is willing to trust (design spec section 9: "The webhook looks
        that finding up in the run store rather than trusting anything in the
        payload body"), so this is the lookup that turns those three label
        values back into a real finding.

        `asset` is the label telemetry pushes, which is the asset path's file
        stem ("docs/samples/test_ad.mp4" -> "test_ad"), so the comparison is
        made against the stem here rather than the whole path. Newest first:
        the same asset can be cleared many times, and an alert firing now is
        about the most recent run that produced it. Returns None when nothing
        matches, which is the answer a forged or stale label must get.
        """
        rows = self._conn.execute(
            "SELECT f.data, f.run_id FROM findings f WHERE f.market = ? "
            "ORDER BY f.rowid DESC",
            (market,),
        ).fetchall()
        for raw, run_id in rows:
            finding = Finding.from_json(json.loads(raw))
            if finding.rule_id != rule_id or finding.status != "open":
                continue
            run = self.get_run(run_id)
            if run is None or Path(run.asset_path).stem != asset:
                continue
            return run, finding
        return None

    @_locked
    def add_change(self, change: ChangeRecord) -> None:
        self._conn.execute(
            "INSERT INTO changes (id, run_id, data) VALUES (?, ?, ?)",
            (change.id, change.run_id, json.dumps(change.to_json())),
        )
        self._conn.commit()

    @_locked
    def changes(self, run_id: str) -> list[ChangeRecord]:
        rows = self._conn.execute(
            "SELECT data FROM changes WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [ChangeRecord.from_json(json.loads(r[0])) for r in rows]

    @_locked
    def emit(self, run_id: str, agent: str, message: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO events (run_id, ts, agent, message) VALUES (?, ?, ?, ?)",
            (run_id, time.time(), agent, message),
        )
        self._conn.commit()
        return cur.lastrowid

    @_locked
    def events_since(self, run_id: str, after_id: int) -> list[tuple]:
        return self._conn.execute(
            "SELECT id, ts, agent, message FROM events "
            "WHERE run_id = ? AND id > ? ORDER BY id",
            (run_id, after_id),
        ).fetchall()
