"""Run the actual worker dispatcher and WeChat4Source over generated SQLite only.

Only Context construction and two fixture-only control commands differ from
worker.serve. No account discovery, real connection, decryptor or user data.
"""
import sys
from pathlib import Path

project, root = map(Path, sys.argv[1:3])
budget_fixture = len(sys.argv) > 3 and sys.argv[3] == "long-messages"
assert root.is_dir()
sys.path.insert(0, str(project / "src/PythonWorker"))
sys.path.insert(0, str(project / "tests/PythonWorker.Tests"))
import worker
from synthetic_messages import SyntheticSourceFactory

original_context = worker.Context


class SyntheticContext(original_context):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        factory = SyntheticSourceFactory(root)
        for shard in range(3):
            factory.add_shard(f"shard-{shard}.db", {"c": [
                {"create_time": 1795579200000 + (i * 3 + shard) * 10,
                 "local_id": 1, "server_id": 7,
                 "content": (f"row-{i * 3 + shard}|".ljust(30_000, "x") if budget_fixture else f"row-{i * 3 + shard}")}
                for i in range(60 if budget_fixture else 401)
            ]}, indexed=True)
        self.wechat = factory.source()
        self.active_source = "wechat4"
        self.fixture_factory = factory


def mutate(payload, context):
    import sqlite3
    path = context.fixture_factory.paths["message\\shard-0.db"]
    with sqlite3.connect(path) as connection:
        table = next(iter(context.fixture_factory.locations.values()))[0][1]
        connection.execute(f'UPDATE "{table}" SET message_content = ? WHERE rowid = 1', ("fixture changed",))
    return {}


worker.Context = SyntheticContext
worker.HANDLERS["fixture_mutate"] = mutate
protocol_out = sys.stdout.buffer
sys.stdout = sys.stderr
raise SystemExit(worker.serve(sys.stdin.buffer, protocol_out, str(root / "unused-demo.json"),
                             str(root / "jobs"), str(root / "scenes.json"), str(root / "models")))
