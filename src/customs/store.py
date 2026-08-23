import json
import sqlite3
import time
import uuid
from pathlib import Path

from customs.schema import ChangeRecord, Finding, Observation, RunRecord

class Store:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
        """)
        self._conn.commit()

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

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT data FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return RunRecord.from_json(json.loads(row[0])) if row else None

    def _write_run(self, run: RunRecord) -> None:
        self._conn.execute(
            "UPDATE runs SET data = ? WHERE id = ?",
            (json.dumps(run.to_json()), run.id),
        )
        self._conn.commit()

    def set_run_status(self, run_id: str, status: str) -> None:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        run.status = status
        self._write_run(run)

    def set_run_t0(self, run_id: str, t0: float) -> None:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        run.t0 = t0
        self._write_run(run)

    def add_observations(self, run_id: str, observations: list[Observation]) -> None:
        rows = [(o.id, run_id, json.dumps(o.to_json())) for o in observations]
        self._conn.executemany(
            "INSERT INTO observations (id, run_id, data) VALUES (?, ?, ?)", rows
        )
        self._conn.commit()

    def observations(self, run_id: str) -> list[Observation]:
        rows = self._conn.execute(
            "SELECT data FROM observations WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [Observation.from_json(json.loads(r[0])) for r in rows]

    def add_findings(self, findings: list[Finding]) -> None:
        rows = [
            (f.id, f.run_id, f.market, json.dumps(f.to_json())) for f in findings
        ]
        self._conn.executemany(
            "INSERT INTO findings (id, run_id, market, data) VALUES (?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

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

    def add_change(self, change: ChangeRecord) -> None:
        self._conn.execute(
            "INSERT INTO changes (id, run_id, data) VALUES (?, ?, ?)",
            (change.id, change.run_id, json.dumps(change.to_json())),
        )
        self._conn.commit()

    def changes(self, run_id: str) -> list[ChangeRecord]:
        rows = self._conn.execute(
            "SELECT data FROM changes WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [ChangeRecord.from_json(json.loads(r[0])) for r in rows]

    def emit(self, run_id: str, agent: str, message: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO events (run_id, ts, agent, message) VALUES (?, ?, ?, ?)",
            (run_id, time.time(), agent, message),
        )
        self._conn.commit()
        return cur.lastrowid

    def events_since(self, run_id: str, after_id: int) -> list[tuple]:
        return self._conn.execute(
            "SELECT id, ts, agent, message FROM events "
            "WHERE run_id = ? AND id > ? ORDER BY id",
            (run_id, after_id),
        ).fetchall()
